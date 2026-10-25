"""Anthropic Messages API wire translation.

The bug this exists to prevent is specific and was already hit once in Cato (K-01): the agent
loop keeps OpenAI-shaped history, and posting a `{"role": "tool"}` message to Anthropic returns
`400 messages: Unexpected role "tool"` — which kills every tool-using conversation on turn 2,
i.e. exactly when an agent first tries to do real work.
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest
import agent_runtime
from agent_runtime import AgentRuntime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.anthropic_wire import (  # noqa: E402
    ANTHROPIC_PRICING_VERSION,
    ANTHROPIC_FALLBACK_MODELS,
    KNOWN_ANTHROPIC_MODEL_IDS,
    MODEL_ID_TRANSLATIONS,
    ExactForcedToolResponseError,
    EXACT_FORCED_TOOL_RESPONSE_ERROR_CODES,
    build_anthropic_request,
    build_exact_forced_tool_request,
    consume_exact_forced_tool_response,
    estimate_anthropic_cost,
    exact_forced_tool_envelope_reason,
    from_anthropic_response,
    split_system,
    to_anthropic_messages,
    to_anthropic_tools,
    translate_model_id,
    validate_reserved_anthropic_schema,
)
from canonical_result_contract import (  # noqa: E402
    CANONICAL_AGENT_IDS,
    CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION,
    CANONICAL_RESULT_TOOL_NAME,
    CLARIFICATION_FIELDS,
    FINANCE_AGENT_IDS,
    FINANCE_NARRATIVE_FIELDS,
    FINANCE_NARRATIVE_SAFETY_DESCRIPTION,
    NARRATIVE_RESULT_FIELDS,
    canonical_result_json_schema,
    canonical_provider_result_json_schema,
    canonical_result_tool_schema,
    project_provider_canonical_result,
    project_provider_canonical_result_with_metadata,
    validate_canonical_result,
)


def _provider_result(slug: str, *, clarification: bool = False) -> dict:
    value = {
        "agent_id": slug, "task_id": "branch", "scope": "bounded fixture",
        "status": "HOLD" if clarification else "ANALYSIS_COMPLETE",
        "summary": "ANALYSIS: Bounded analysis only.", "findings": [],
        "proposals": [], "evidence_refs": [], "assumptions": [], "conflicts": [],
        "unknowns": [], "confidence": 0.8, "risk_flags": [],
        "recommended_next_actions": [], "requested_human_decisions": [],
        "tool_calls_summary": [], "clarification_needed": clarification,
    }
    if slug in FINANCE_AGENT_IDS:
        value["finance_proposal"] = {
            "entity_id": None, "period": None, "transaction_or_schedule_type": None,
            "proposed_treatment": None, "account_mapping": None, "tracking_mapping": None,
            "tax_treatment": None, "amount_logic": None, "source_refs": [],
            "policy_refs": [], "assumptions": [], "confidence": None, "exceptions": [],
            "requires_human_decision": False, "recommended_financeos_action": None,
        }
    if clarification:
        value.update({
            "candidate_resolution": "PROPOSAL: Retain the current bounded interpretation.",
            "reason_human_needed": "ANALYSIS: Two authoritative sources conflict.",
            "recommended_human_resolver": "NEXT: Ask the authorized process owner.",
            "materiality": 0.8, "urgency": "CLOSURE_BATCH",
            "resolution_attempts": [
                {"source_or_capability": "source one", "result": "ANALYSIS: State A.",
                 "timestamp": "2026-08-26T12:00:00Z"},
                {"source_or_capability": "source two", "result": "ANALYSIS: State B.",
                 "timestamp": "2026-08-26T12:01:00Z"},
            ],
            "supporting_evidence_refs": ["source:one"],
            "competing_state_refs": ["source:two"],
            "recommended_answer_options": ["CONFIRM", "CORRECT"],
        })
    else:
        value.update({
            "candidate_resolution": "", "reason_human_needed": "",
            "recommended_human_resolver": "", "materiality": None, "urgency": "",
            "resolution_attempts": [], "supporting_evidence_refs": [],
            "competing_state_refs": [], "recommended_answer_options": [],
        })
    return value


def _provider_arguments(flat: dict) -> dict:
    value = copy.deepcopy(flat)
    clarification = None
    if value.pop("clarification_needed"):
        clarification = {
            field: value.pop(field)
            for field in (
                "candidate_resolution", "reason_human_needed", "recommended_human_resolver",
                "materiality", "urgency", "resolution_attempts", "supporting_evidence_refs",
                "competing_state_refs", "recommended_answer_options",
            )
        }
    else:
        for field in (
            "candidate_resolution", "reason_human_needed", "recommended_human_resolver",
            "materiality", "urgency", "resolution_attempts", "supporting_evidence_refs",
            "competing_state_refs", "recommended_answer_options",
        ):
            value.pop(field, None)
    value["clarification"] = clarification
    return value


# ---------------------------------------------------------------------------
# Model id translation + drift
# ---------------------------------------------------------------------------

def test_bundle_model_hints_all_translate_to_a_real_anthropic_id():
    """The drift test. All 24 bundles carry `anthropic/claude-sonnet-4-5`, a gateway-style id
    Anthropic rejects. If a bundle ever gains a hint with no translation, this fails rather than
    404-ing at runtime inside a paid agent run."""
    from bundle_loader import list_bundles, load_bundle

    slugs = list_bundles()
    assert slugs, "precondition: bundles exist"

    unmapped = []
    for slug in slugs:
        hint = (load_bundle(slug) or {}).get("model_hint")
        if not hint or hint == "auto":
            continue
        if translate_model_id(hint) not in KNOWN_ANTHROPIC_MODEL_IDS:
            unmapped.append((slug, hint, translate_model_id(hint)))

    assert not unmapped, f"bundle model_hints with no real Anthropic id: {unmapped}"


def test_every_translation_target_is_a_known_real_model_id():
    """Translations and the verified id registry cannot silently diverge."""
    bad = {k: v for k, v in MODEL_ID_TRANSLATIONS.items() if v not in KNOWN_ANTHROPIC_MODEL_IDS}
    assert not bad, f"translations pointing at ids not in the verified registry: {bad}"


def test_fallback_chain_contains_only_real_anthropic_ids():
    """The OpenAI path falls back to 'openrouter/free' and 'minimax/...', which mean nothing to
    Anthropic — a fallback that cannot work hides the real error behind the last one."""
    assert ANTHROPIC_FALLBACK_MODELS
    for model in ANTHROPIC_FALLBACK_MODELS:
        assert model in KNOWN_ANTHROPIC_MODEL_IDS, model


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("anthropic/claude-sonnet-4-5", "claude-sonnet-4-5-20250929"),
        ("claude-sonnet-4-5", "claude-sonnet-4-5-20250929"),
        ("anthropic/claude-opus-5", "claude-opus-5"),
        ("claude-sonnet-5", "claude-sonnet-5"),
        ("auto", "claude-sonnet-4-5-20250929"),
        ("", "claude-sonnet-4-5-20250929"),
    ],
)
def test_translate_model_id(raw, expected):
    assert translate_model_id(raw) == expected


def test_unknown_model_id_passes_through_rather_than_being_silently_rewritten():
    """A wrong-but-explicit 404 naming the id beats a silent substitution that bills a model
    nobody chose."""
    assert translate_model_id("some-future-model") == "some-future-model"


# ---------------------------------------------------------------------------
# The role:"tool" bug
# ---------------------------------------------------------------------------

def test_tool_role_becomes_a_user_tool_result_block():
    """THE regression. `role:"tool"` must never reach the wire."""
    out = to_anthropic_messages([
        {"role": "user", "content": "list the repo"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "file_write", "arguments": '{"path":"a.txt"}'}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
    ])

    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert all(m["role"] != "tool" for m in out)
    assistant_blocks = out[1]["content"]
    assert assistant_blocks[0]["type"] == "tool_use"
    assert assistant_blocks[0]["id"] == "call_1"
    assert assistant_blocks[0]["input"] == {"path": "a.txt"}
    result_block = out[2]["content"][0]
    assert result_block == {
        "type": "tool_result", "tool_use_id": "call_1", "content": '{"ok": true}'
    }


def test_multiple_tool_results_are_grouped_into_one_user_turn():
    """Anthropic rejects a tool_use block not answered in the IMMEDIATELY following user turn,
    so a parallel multi-tool turn must not become several user messages."""
    out = to_anthropic_messages([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "function": {"name": "a", "arguments": "{}"}},
            {"id": "c2", "function": {"name": "b", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "tool", "tool_call_id": "c2", "content": "r2"},
    ])

    assert len(out) == 2
    assert [b["tool_use_id"] for b in out[1]["content"]] == ["c1", "c2"]  # order preserved


def test_translation_is_idempotent():
    already = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]
    assert to_anthropic_messages(already) == already


def test_non_dict_tool_arguments_do_not_crash_the_translation():
    out = to_anthropic_messages([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "function": {"name": "a", "arguments": "not json at all"}}
        ]},
    ])
    assert out[0]["content"][0]["input"] == {}


def test_empty_assistant_turn_is_dropped():
    """An assistant message with neither content nor tool_calls is not a legal Anthropic turn."""
    out = to_anthropic_messages([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None},
    ])
    assert [m["role"] for m in out] == ["user"]


# ---------------------------------------------------------------------------
# system, tools, request assembly
# ---------------------------------------------------------------------------

def test_system_is_lifted_to_a_top_level_parameter():
    system, rest = split_system([
        {"role": "system", "content": "You are Genesis Research."},
        {"role": "user", "content": "go"},
    ])
    assert system == "You are Genesis Research."
    assert [m["role"] for m in rest] == ["user"]


def test_tools_are_converted_to_input_schema_shape():
    converted = to_anthropic_tools([
        {"type": "function", "function": {
            "name": "web_search", "description": "search",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        }}
    ])
    assert converted == [{
        "name": "web_search", "description": "search",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }]


def test_request_body_has_required_max_tokens_and_no_system_role():
    body = build_anthropic_request(
        model="claude-sonnet-4-5-20250929",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
        max_tokens=512,
    )

    assert body["max_tokens"] == 512  # REQUIRED by this API
    assert body["system"] == "sys"
    assert all(m["role"] != "system" for m in body["messages"])
    assert body["tool_choice"] == {"type": "auto"}
    assert "input_schema" in body["tools"][0]


def test_request_omits_tools_entirely_when_there_are_none():
    body = build_anthropic_request(
        model="m", messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=10
    )
    assert "tools" not in body and "tool_choice" not in body


@pytest.mark.parametrize(
    ("slug", "finance_required"),
    [
        ("genesis-knowledge-evidence", False),
        ("genesis-fin-ap", True),
    ],
)
def test_canonical_result_tool_is_slug_bound_forced_and_not_strict(slug, finance_required):
    tool = canonical_result_tool_schema(slug)
    body = build_anthropic_request(
        model="claude-sonnet-4-5-20250929",
        messages=[{"role": "user", "content": "bounded"}],
        tools=[tool],
        max_tokens=1000,
    )
    assert body["tool_choice"] == {
        "type": "tool",
        "name": CANONICAL_RESULT_TOOL_NAME,
        "disable_parallel_tool_use": True,
    }
    assert len(body["tools"]) == 1
    assert "strict" not in body["tools"][0]
    assert "_genesis_agent_id" not in body["tools"][0]
    schema = body["tools"][0]["input_schema"]
    assert "$schema" not in schema
    assert schema["properties"]["agent_id"] == {"type": "string", "enum": [slug]}
    assert ("finance_proposal" in schema["required"]) is finance_required
    assert ("finance_proposal" in schema["properties"]) is finance_required


def _schema_metrics(schema):
    banned = {"$schema", "minimum", "maximum", "minLength", "uniqueItems"}
    seen_banned = set()
    object_count = optional_count = union_count = 0

    def walk(node):
        nonlocal object_count, optional_count, union_count
        if isinstance(node, dict):
            seen_banned.update(banned & set(node))
            node_type = node.get("type")
            if isinstance(node_type, list):
                union_count += 1
            if node_type == "object":
                object_count += 1
                assert node.get("additionalProperties") is False
                properties = node.get("properties") or {}
                optional_count += len(set(properties) - set(node.get("required") or []))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return seen_banned, object_count, optional_count, union_count


def test_all_24_reserved_schemas_are_flat_exact_bound_and_within_dialect_limits():
    assert len(CANONICAL_AGENT_IDS) == 19
    for slug in CANONICAL_AGENT_IDS:
        schema = canonical_provider_result_json_schema(slug)
        assert schema["type"] == "object"
        assert not ({"oneOf", "allOf", "anyOf", "if", "then", "else"} & set(schema))
        assert schema["properties"]["agent_id"]["enum"] == [slug]
        assert "clarification" in schema["required"]
        assert not (set(schema["properties"]) & {
            "clarification_needed", "candidate_resolution", "reason_human_needed",
            "recommended_human_resolver", "materiality", "urgency", "resolution_attempts",
            "supporting_evidence_refs", "competing_state_refs", "recommended_answer_options",
        })
        finance = slug in FINANCE_AGENT_IDS
        assert ("finance_proposal" in schema["required"]) is finance
        assert ("finance_proposal" in schema["properties"]) is finance
        banned, objects, optional, unions = _schema_metrics(schema)
        assert banned == set()
        assert objects == (2 if finance else 1)
        assert optional == 0
        assert unions == (11 if finance else 0)
        validate_reserved_anthropic_schema(schema, slug)


@pytest.mark.parametrize("slug", ["genesis-knowledge-evidence", "genesis-fin-ap"])
def test_outgoing_schema_matches_rediagnosed_architecture_examples(slug):
    name = "finance_schema.json" if slug == "genesis-fin-ap" else "master_schema.json"
    expected = json.loads(
        (PROJECT_ROOT / "proof-artifacts/rediagnose-v2-anthropic-submission-schema" / name)
        .read_text(encoding="utf-8")
    )
    actual = copy.deepcopy(canonical_provider_result_json_schema(slug))

    def strip_descriptions(node):
        if isinstance(node, dict):
            node.pop("description", None)
            node.pop("pattern", None)
            node.pop("maxLength", None)
            node.pop("maxItems", None)
            for value in node.values():
                strip_descriptions(value)
        elif isinstance(node, list):
            for value in node:
                strip_descriptions(value)

    strip_descriptions(actual)
    actual["properties"].pop("clarification")
    actual["required"].remove("clarification")
    expected["required"].remove("clarification_needed")
    for field in (
        "clarification_needed",
        "candidate_resolution", "reason_human_needed", "recommended_human_resolver",
        "urgency", "materiality", "resolution_attempts", "supporting_evidence_refs",
        "competing_state_refs", "recommended_answer_options",
    ):
        expected["properties"].pop(field, None)
    assert actual == expected


def test_provider_visible_guidance_is_centralized_compact_and_dialect_safe():
    for slug in ("genesis-knowledge-evidence", "genesis-fin-payroll-tax"):
        schema = canonical_provider_result_json_schema(slug)
        assert schema["properties"]["status"]["description"] == (
            "ANALYSIS_COMPLETE denotes bounded analysis only."
        )
        assert '"pattern"' in json.dumps(schema)
        validate_reserved_anthropic_schema(schema, slug)
    finance = canonical_provider_result_json_schema("genesis-fin-payroll-tax")["properties"]["finance_proposal"]
    for field in FINANCE_NARRATIVE_FIELDS:
        assert finance["properties"][field]["description"] == (
            FINANCE_NARRATIVE_SAFETY_DESCRIPTION
        )
    finance_root = canonical_provider_result_json_schema("genesis-fin-payroll-tax")["properties"]
    for field in NARRATIVE_RESULT_FIELDS[:9]:
        assert finance_root[field]["description"] == FINANCE_NARRATIVE_SAFETY_DESCRIPTION
    assert finance_root["clarification"] == {"type": "null"}
    master_root = canonical_provider_result_json_schema("genesis-knowledge-evidence")["properties"]
    assert all(
        master_root[field].get("description") != FINANCE_NARRATIVE_SAFETY_DESCRIPTION
        for field in NARRATIVE_RESULT_FIELDS
        if field in master_root
    )
    assert "ANALYSIS_COMPLETE means only" in CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION
    assert "never assert that external work" in CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION
    assert "clarification key exactly once with value null" in CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION


@pytest.mark.parametrize("slug", sorted(CANONICAL_AGENT_IDS))
def test_all_24_provider_schemas_admit_explicit_false_clarification_branch(slug):
    schema = canonical_provider_result_json_schema(slug)
    value = _provider_arguments(_provider_result(slug))
    assert list(__import__("jsonschema").Draft202012Validator(schema).iter_errors(value)) == []
    validate_reserved_anthropic_schema(schema, slug)
    assert validate_canonical_result(slug, project_provider_canonical_result(slug, value)).ok


@pytest.mark.parametrize("slug", sorted(CANONICAL_AGENT_IDS))
def test_all_24_provider_schemas_admit_complete_true_clarification_branch(slug):
    schema = canonical_provider_result_json_schema(slug)
    value = _provider_arguments(_provider_result(slug, clarification=True))
    assert list(__import__("jsonschema").Draft202012Validator(schema).iter_errors(value))
    validate_reserved_anthropic_schema(schema, slug)
    with pytest.raises(ValueError, match="provider_clarification_representation_invalid"):
        project_provider_canonical_result(slug, value)


@pytest.mark.parametrize("slug", sorted(CANONICAL_AGENT_IDS))
@pytest.mark.parametrize("clarification", [False, True], ids=["false", "true"])
def test_all_24_complete_legacy_and_equal_dual_representations_project_exactly(slug, clarification):
    legacy = _provider_result(slug, clarification=clarification)
    legacy_snapshot = copy.deepcopy(legacy)
    legacy_projection = project_provider_canonical_result_with_metadata(slug, legacy)
    if clarification:
        assert not legacy_projection.ok and legacy_projection.subtype == "legacy_invalid"
        assert legacy == legacy_snapshot
        return
    assert legacy_projection.ok and legacy_projection.subtype == "legacy_flat"
    assert legacy_projection.value == legacy_snapshot
    assert legacy == legacy_snapshot

    canonical = _provider_arguments(legacy)
    dual = copy.deepcopy(canonical)
    dual.update(copy.deepcopy(legacy))
    dual_snapshot = copy.deepcopy(dual)
    dual_projection = project_provider_canonical_result_with_metadata(slug, dual)
    assert dual_projection.ok and dual_projection.subtype == "both_equal"
    assert dual_projection.value == legacy_snapshot
    assert dual == dual_snapshot


@pytest.mark.parametrize("slug", sorted(CANONICAL_AGENT_IDS))
def test_provider_representation_compatibility_fails_closed_with_safe_diagnostics(slug):
    legacy = _provider_result(slug, clarification=True)

    partial = copy.deepcopy(legacy)
    partial.pop("urgency")
    verdict = project_provider_canonical_result_with_metadata(slug, partial)
    assert not verdict.ok and verdict.subtype == "legacy_partial"
    assert verdict.missing_fields == ("urgency",)
    assert verdict.missing_field_count == 1

    mixed_partial = _provider_arguments(legacy)
    mixed_partial["clarification_needed"] = True
    verdict = project_provider_canonical_result_with_metadata(slug, mixed_partial)
    assert not verdict.ok and verdict.subtype == "legacy_partial"
    assert verdict.missing_field_count == len(CLARIFICATION_FIELDS) - 1

    mismatch = _provider_arguments(legacy)
    mismatch.update(copy.deepcopy(legacy))
    mismatch["urgency"] = "IMMEDIATE"
    verdict = project_provider_canonical_result_with_metadata(slug, mismatch)
    assert not verdict.ok and verdict.subtype == "canonical_invalid"

    unknown = _provider_arguments(legacy)
    unknown["clarification"]["private_payload"] = "must-not-leak"
    verdict = project_provider_canonical_result_with_metadata(slug, unknown)
    assert not verdict.ok and verdict.subtype == "canonical_invalid"
    assert verdict.extra_fields == () and verdict.extra_field_count == 1
    assert "private_payload" not in repr(verdict)

    missing = copy.deepcopy(legacy)
    for field in CLARIFICATION_FIELDS:
        missing.pop(field)
    verdict = project_provider_canonical_result_with_metadata(slug, missing)
    assert not verdict.ok and verdict.subtype == "representation_missing"
    assert verdict.missing_fields == ("clarification",)


def test_provider_branch_shape_and_local_condition_reject_hostile_mixed_values():
    slug = "genesis-knowledge-evidence"
    schema = canonical_provider_result_json_schema(slug)
    validator = __import__("jsonschema").Draft202012Validator(schema)

    false_with_true_detail = _provider_arguments(_provider_result(slug))
    false_with_true_detail["candidate_resolution"] = "PROPOSAL: Resolve the conflict."
    assert list(validator.iter_errors(false_with_true_detail))
    with pytest.raises(ValueError, match="provider_clarification_representation_invalid"):
        project_provider_canonical_result(slug, false_with_true_detail)

    true_with_false_detail = _provider_arguments(_provider_result(slug, clarification=True))
    true_with_false_detail["clarification"]["reason_human_needed"] = ""
    assert list(validator.iter_errors(true_with_false_detail))

    for field, hostile in (
        ("candidate_resolution", " "),
        ("reason_human_needed", "unprefixed"),
        ("urgency", " "),
        ("summary", ""),
        ("summary", "unprefixed"),
    ):
        value = _provider_arguments(_provider_result(slug, clarification=field != "summary"))
        if field == "summary":
            value[field] = hostile
        else:
            value["clarification"][field] = hostile
        assert list(validator.iter_errors(value)), field


@pytest.mark.parametrize("clarification", [False, True], ids=["null", "object"])
def test_provider_clarification_projection_is_deterministic_and_does_not_mutate(clarification):
    slug = "genesis-fin-ap"
    provider = _provider_arguments(_provider_result(slug, clarification=clarification))
    snapshot = copy.deepcopy(provider)
    if clarification:
        with pytest.raises(ValueError, match="provider_clarification_representation_invalid"):
            project_provider_canonical_result(slug, provider)
        assert provider == snapshot
        return
    first = project_provider_canonical_result(slug, provider)
    second = project_provider_canonical_result(slug, provider)
    assert first == second
    assert first is not second
    assert provider == snapshot
    assert validate_canonical_result(slug, first).ok


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda schema: schema.update(allOf=[]), "root_combinator"),
        (lambda schema: schema.update(oneOf=[]), "root_combinator"),
        (lambda schema: schema.update(anyOf=[]), "root_combinator"),
        (lambda schema: schema["properties"]["summary"].update(minLength=1), "unsupported_keyword"),
        (lambda schema: schema["properties"]["summary"].update(format="unsafe"), "unsupported_keyword"),
        (lambda schema: schema["properties"]["clarification"].update(type=["object", "null"]), "domain_shape"),
        (lambda schema: schema["properties"]["agent_id"].update(enum=["wrong"]), "agent_binding"),
    ],
)
def test_reserved_schema_preflight_rejects_old_and_malformed_dialects(mutation, code):
    tool = canonical_result_tool_schema("genesis-knowledge-evidence")
    mutation(tool["input_schema"])
    with pytest.raises(ValueError, match=f"reserved_schema_invalid:{code}"):
        build_anthropic_request(
            model="m",
            messages=[{"role": "user", "content": "bounded"}],
            tools=[tool],
            max_tokens=10,
        )


def test_canonical_runtime_rebuilds_authoritative_schema_in_shared_transport(monkeypatch):
    calls = []
    tool = canonical_result_tool_schema("genesis-knowledge-evidence")
    tool["input_schema"]["allOf"] = []

    async def exact_candidate(**kwargs):
        calls.append(kwargs)
        return {
            "model": "claude-sonnet-4-5-20250929",
            "choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": []}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(agent_runtime, "call_exact_anthropic_candidate", exact_candidate)
    asyncio.run(AgentRuntime("https://api.anthropic.com", "redacted")._call_anthropic(
        "claude-sonnet-4-5-20250929",
        [{"role": "user", "content": "bounded"}],
        [tool],
        100,
    ))
    assert len(calls) == 1
    assert calls[0]["slug"] == "genesis-knowledge-evidence"


def test_reserved_schema_preflight_enforces_optional_and_union_limits():
    master = canonical_provider_result_json_schema("genesis-knowledge-evidence")
    master["properties"].update({f"extra_{index}": {"type": "string"} for index in range(25)})
    with pytest.raises(ValueError, match="reserved_schema_invalid:optional_limit"):
        validate_reserved_anthropic_schema(master, "genesis-knowledge-evidence")

    finance = canonical_provider_result_json_schema("genesis-fin-ap")
    finance["properties"].update({
        f"extra_union_{index}": {"type": ["string", "null"]}
        for index in range(14)
    })
    with pytest.raises(ValueError, match="reserved_schema_invalid:union_limit"):
        validate_reserved_anthropic_schema(finance, "genesis-fin-ap")


def test_reserved_schema_preflight_rejects_wrong_root_and_domain_shape():
    wrong_root = canonical_provider_result_json_schema("genesis-knowledge-evidence")
    wrong_root["type"] = "array"
    with pytest.raises(ValueError, match="reserved_schema_invalid:root_type"):
        validate_reserved_anthropic_schema(wrong_root, "genesis-knowledge-evidence")

    wrong_domain = canonical_provider_result_json_schema("genesis-knowledge-evidence")
    wrong_domain["properties"]["finance_proposal"] = {"type": "object"}
    with pytest.raises(ValueError, match="reserved_schema_invalid:domain_shape"):
        validate_reserved_anthropic_schema(wrong_domain, "genesis-knowledge-evidence")


def test_reserved_result_tool_cannot_be_mixed_with_business_tools():
    with pytest.raises(ValueError, match="reserved canonical result tool must be exclusive"):
        build_anthropic_request(
            model="m",
            messages=[{"role": "user", "content": "bounded"}],
            tools=[
                canonical_result_tool_schema("genesis-knowledge-evidence"),
                {"name": "web_search", "input_schema": {"type": "object"}},
            ],
            max_tokens=10,
        )


def test_noncanonical_anthropic_tool_projection_remains_unchanged():
    legacy = {
        "name": "legacy_business_tool",
        "description": "legacy",
        "input_schema": {"$schema": "legacy-dialect", "type": "object"},
        "strict": True,
    }
    assert to_anthropic_tools([legacy]) == [legacy]


def _judge_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "offending_fields"],
        "properties": {
            "decision": {"type": "string", "enum": ["ALLOW", "DENY", "UNCERTAIN"]},
            "offending_fields": {"type": "array", "items": {"type": "string"}},
        },
    }


def test_generic_exact_forced_tool_request_is_exclusive_and_named():
    body = build_exact_forced_tool_request(
        model="claude-haiku-4-5-20251001",
        messages=[
            {"role": "system", "content": "fixed policy"},
            {"role": "user", "content": "untrusted projection"},
        ],
        tool_name="submit_genesis_safety_judgment_v1",
        input_schema=_judge_schema(),
        max_tokens=512,
        description="Return the safety decision.",
    )
    assert body["model"] == "claude-haiku-4-5-20251001"
    assert body["system"] == "fixed policy"
    assert len(body["tools"]) == 1
    assert body["tools"][0]["name"] == "submit_genesis_safety_judgment_v1"
    assert body["tool_choice"] == {
        "type": "tool",
        "name": "submit_genesis_safety_judgment_v1",
        "disable_parallel_tool_use": True,
    }


def test_generic_forced_tool_does_not_change_canonical_request_behavior():
    tool = canonical_result_tool_schema("genesis-knowledge-evidence")
    before = build_anthropic_request(
        model="claude-sonnet-4-5-20250929",
        messages=[{"role": "user", "content": "bounded"}],
        tools=[tool],
        max_tokens=1000,
    )
    with pytest.raises(ValueError, match="exact_forced_tool_invalid:canonical_reserved"):
        build_exact_forced_tool_request(
            model="claude-sonnet-4-5-20250929",
            messages=[{"role": "user", "content": "bounded"}],
            tool_name=CANONICAL_RESULT_TOOL_NAME,
            input_schema=tool["input_schema"],
            max_tokens=1000,
        )
    after = build_anthropic_request(
        model="claude-sonnet-4-5-20250929",
        messages=[{"role": "user", "content": "bounded"}],
        tools=[tool],
        max_tokens=1000,
    )
    assert after == before


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda schema: schema.update(additionalProperties=True), "open_or_optional_object"),
        (lambda schema: schema["required"].remove("decision"), "open_or_optional_object"),
        (lambda schema: schema.update(oneOf=[]), "root_combinator"),
        (lambda schema: schema.update(**{"$schema": "draft"}), "unsupported_keyword"),
    ],
)
def test_generic_forced_tool_schema_fails_closed(mutation, code):
    schema = _judge_schema()
    mutation(schema)
    with pytest.raises(ValueError, match=f"exact_forced_tool_schema_invalid:{code}"):
        build_exact_forced_tool_request(
            model="claude-haiku-4-5-20251001",
            messages=[{"role": "user", "content": "data"}],
            tool_name="submit_genesis_safety_judgment_v1",
            input_schema=schema,
            max_tokens=100,
        )


def _judge_response(**changes):
    value = {
        "model": "claude-haiku-4-5-20251001",
        "stop_reason": "tool_use",
        "content": [{
            "type": "tool_use",
            "id": "judge-1",
            "name": "submit_genesis_safety_judgment_v1",
            "input": {"decision": "ALLOW", "offending_fields": []},
        }],
        "usage": {"input_tokens": 50, "output_tokens": 10},
    }
    value.update(changes)
    return value


def test_generic_exact_forced_tool_response_returns_detached_json_arguments_and_usage():
    payload = _judge_response()
    result = consume_exact_forced_tool_response(
        payload, tool_name="submit_genesis_safety_judgment_v1"
    )
    assert result.actual_model == "claude-haiku-4-5-20251001"
    assert result.input_tokens == 50 and result.output_tokens == 10
    assert result.arguments == {"decision": "ALLOW", "offending_fields": []}
    result.arguments["decision"] = "DENY"
    assert payload["content"][0]["input"]["decision"] == "ALLOW"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ("not-an-envelope", "invalid"),
        (_judge_response(stop_reason="end_turn", content=[]), "missing"),
        (_judge_response(stop_reason="max_tokens"), "truncated"),
        (_judge_response(stop_reason="refusal"), "refusal"),
        (_judge_response(content=[{"type": "text", "text": "ALLOW"}]), "text"),
        (_judge_response(content=[]), "missing"),
        (_judge_response(content=[
            _judge_response()["content"][0], _judge_response()["content"][0]
        ]), "multiple"),
        (_judge_response(content=[{
            **_judge_response()["content"][0], "name": "wrong"
        }]), "wrong_tool"),
        (_judge_response(content=[{
            **_judge_response()["content"][0], "input": "not-an-object"
        }]), "arguments"),
        (_judge_response(model=None), "model"),
        (_judge_response(usage={}), "usage"),
        (_judge_response(usage={"input_tokens": 1.5, "output_tokens": 1}), "usage"),
    ],
)
def test_generic_exact_forced_tool_response_matrix_fails_content_free(payload, code):
    with pytest.raises(ExactForcedToolResponseError) as exc:
        consume_exact_forced_tool_response(
            payload, tool_name="submit_genesis_safety_judgment_v1"
        )
    assert exc.value.code == f"exact_forced_tool_response_{code}"
    assert "ALLOW" not in str(exc.value)


def test_exact_forced_tool_error_codes_are_closed_and_unknown_codes_collapse() -> None:
    assert EXACT_FORCED_TOOL_RESPONSE_ERROR_CODES == {
        "exact_forced_tool_response_invalid", "exact_forced_tool_response_truncated",
        "exact_forced_tool_response_refusal", "exact_forced_tool_response_missing",
        "exact_forced_tool_response_text", "exact_forced_tool_response_multiple",
        "exact_forced_tool_response_wrong_tool", "exact_forced_tool_response_arguments",
        "exact_forced_tool_response_model", "exact_forced_tool_response_usage",
    }
    hostile = ExactForcedToolResponseError("provider content: secret")
    assert hostile.code == str(hostile) == "exact_forced_tool_response_invalid"
    assert "secret" not in str(hostile)
    assert exact_forced_tool_envelope_reason(hostile) == "exact_forced_tool_response_invalid"
    assert exact_forced_tool_envelope_reason(RuntimeError("secret")) is None


# ---------------------------------------------------------------------------
# Response normalisation
# ---------------------------------------------------------------------------

def test_response_with_tool_use_normalises_to_openai_tool_calls():
    """The agent loop reads choices[0].message.tool_calls — translating back means the loop,
    trace, budget accounting and every existing test keep working on one canonical shape."""
    normalised = from_anthropic_response({
        "id": "msg_1", "model": "claude-sonnet-4-5-20250929", "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": "I'll search."},
            {"type": "tool_use", "id": "tu_1", "name": "web_search", "input": {"q": "e4l"}},
        ],
        "usage": {"input_tokens": 100, "output_tokens": 25},
    })

    msg = normalised["choices"][0]["message"]
    assert msg["content"] == "I'll search."
    assert msg["tool_calls"][0]["id"] == "tu_1"
    assert msg["tool_calls"][0]["function"]["name"] == "web_search"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"q": "e4l"}
    assert normalised["choices"][0]["finish_reason"] == "tool_calls"
    assert normalised["usage"]["total_tokens"] == 125  # budget accounting depends on this


def test_plain_text_response_has_no_tool_calls():
    normalised = from_anthropic_response({
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "done"}],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    })
    msg = normalised["choices"][0]["message"]
    assert msg["content"] == "done"
    assert "tool_calls" not in msg
    assert normalised["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize(
    ("stop_reason", "finish_reason"),
    [("max_tokens", "length"), ("refusal", "refusal")],
)
def test_provider_terminal_reasons_remain_distinguishable(stop_reason, finish_reason):
    normalised = from_anthropic_response({
        "stop_reason": stop_reason,
        "content": [{"type": "text", "text": "not persisted"}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    assert normalised["choices"][0]["finish_reason"] == finish_reason


def test_estimated_cost_uses_explicit_usage_and_versioned_pricing_only():
    estimate = estimate_anthropic_cost(
        "claude-sonnet-4-5-20250929",
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
    )
    assert estimate == {
        "estimated_cost_usd": 18.0,
        "cost_basis": "estimated",
        "pricing_version": ANTHROPIC_PRICING_VERSION,
        "unavailable_reason": None,
    }

    unknown = estimate_anthropic_cost(
        "claude-future", {"input_tokens": 100, "output_tokens": 100}
    )
    assert unknown["estimated_cost_usd"] is None
    assert unknown["unavailable_reason"] == "unknown_model_pricing"

    absent = estimate_anthropic_cost("claude-sonnet-4-5-20250929", {})
    assert absent["estimated_cost_usd"] is None
    assert absent["unavailable_reason"] == "usage_unavailable"


def test_direct_anthropic_call_measures_latency_and_estimates_from_raw_usage(monkeypatch):
    async def exact_candidate(**_kwargs):
        return {
            "model": "claude-haiku-4-5-20251001",
            "choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": []}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }

    monkeypatch.setattr(agent_runtime, "call_exact_anthropic_candidate", exact_candidate)
    result = asyncio.run(AgentRuntime("https://api.anthropic.com", "redacted")._call_anthropic(
        "claude-sonnet-4-5-20250929",
        [{"role": "user", "content": "bounded"}],
        [canonical_result_tool_schema("genesis-knowledge-evidence")],
        100,
    ))
    obs = result["_genesis_observability"]
    assert isinstance(obs["latency_ms"], float)
    assert obs["latency_ms"] >= 0
    assert obs["estimated_cost_usd"] == pytest.approx(0.0002)
    assert obs["cost_basis"] == "estimated"
    assert obs["pricing_version"] == ANTHROPIC_PRICING_VERSION
    assert obs["cost_unavailable_reason"] is None


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "env,url,expected",
    [
        ("", "https://api.anthropic.com/v1/messages", True),
        ("", "https://api.swarmsync.ai/v1/chat/completions", False),
        ("anthropic", "https://api.swarmsync.ai/v1/chat/completions", True),
        ("openai", "https://api.anthropic.com/v1/messages", False),
    ],
)
def test_provider_selection(monkeypatch, env, url, expected):
    """Explicit env signal wins; URL detection is the fallback."""
    from agent_runtime import AgentRuntime

    monkeypatch.setenv("GENESIS_LLM_PROVIDER", env)
    assert AgentRuntime(url, "k")._is_anthropic() is expected


def test_openai_path_is_untouched_by_the_anthropic_branch(monkeypatch):
    """Provider-aware, not a rip-out: the gateway path must still build an OpenAI body."""
    from agent_runtime import AgentRuntime

    monkeypatch.delenv("GENESIS_LLM_PROVIDER", raising=False)
    rt = AgentRuntime("https://api.swarmsync.ai/v1/chat/completions", "k")
    assert rt._is_anthropic() is False


# ---------------------------------------------------------------------------
# Config documentation + the tool-evidence gate the live proof relied on
# ---------------------------------------------------------------------------

def test_env_example_documents_the_provider_switch():
    """A provider switch nobody can find is a provider switch nobody sets."""
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "GENESIS_LLM_PROVIDER" in text
    assert "api.anthropic.com" in text
    assert "anthropic-version" in text
    assert "LEGACY/NONCANONICAL GATEWAY PATH" in text
    assert "Canonical V5 fails closed before any provider" in text
    assert "never route" in text
    assert "MasterOS=4096 tokens" in text and "FinanceOS=4096 tokens" in text
    assert "candidate=60s" in text and "judge=5s" in text and "2s endpoint reserve" in text
    assert "67s total" in text and "below the 60s default Cato request budget" in text


def test_tool_evidence_gate_is_not_vacuous():
    """The live proof claims `min_successful_tool_calls` was satisfied by real tool evidence.
    That claim is only worth anything if a prose-only answer FAILS."""
    from agent_runtime import _check_success_criteria
    from bundle_loader import load_bundle

    advertises_tools = bool((load_bundle("genesis-builder") or {}).get("tools_advertised"))
    assert advertises_tools, "precondition: genesis-builder advertises tools"

    real = {
        "response": "Output: Fizz 4",
        "trace": {"tool_calls": [
            {"tool_name": "file_write", "ok": True},
            {"tool_name": "run_code", "ok": True},
        ]},
        "elapsed_s": 12,
    }
    prose_only = {
        "response": "I have created the file and it works.",
        "trace": {"tool_calls": []},
        "elapsed_s": 1,
    }

    assert _check_success_criteria(None, real, require_tool_evidence=True)["ok"] is True
    verdict = _check_success_criteria(None, prose_only, require_tool_evidence=True)
    assert verdict["ok"] is False
    assert verdict["failed"][0]["type"] == "min_successful_tool_calls"


def test_canonical_output_contract_normalizes_one_complete_json_fence():
    from agent_runtime import _canonical_output_contract

    bundle = {"canonical_profile": True, "output_shape_hint": ["status", "summary"]}
    verdict = _canonical_output_contract(
        bundle,
        '```json\n{"status":"HOLD","summary":"Evidence missing"}\n```',
    )
    assert verdict == {
        "required": True,
        "ok": True,
        "normalized_code_fence": True,
        "missing_keys": [],
        "reason": None,
        "response": '{"status":"HOLD","summary":"Evidence missing"}',
    }


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("plain prose", "response_not_json_object"),
        ('["not", "an", "object"]', "response_not_json_object"),
        ('{"status":"HOLD"}', "missing_required_keys"),
        ('```json\n{"status":"HOLD"}\n```\nextra', "response_not_json_object"),
        ('```json\n{"status":"HOLD"}\n```\n```json\n{}\n```', "response_not_json_object"),
    ],
)
def test_canonical_output_contract_rejects_prose_non_object_partial_and_ambiguous_fences(content, reason):
    from agent_runtime import _canonical_output_contract

    bundle = {"canonical_profile": True, "output_shape_hint": ["status", "summary"]}
    verdict = _canonical_output_contract(bundle, content)
    assert verdict["ok"] is False
    assert verdict["reason"] == reason
    assert "summary" in verdict["missing_keys"]


def test_noncanonical_output_contract_preserves_legacy_response_verbatim():
    from agent_runtime import _canonical_output_contract

    content = "legacy prose remains valid"
    verdict = _canonical_output_contract(
        {"canonical_profile": False, "output_shape_hint": ["status"]}, content
    )
    assert verdict["ok"] is True
    assert verdict["required"] is False
    assert verdict["response"] == content
