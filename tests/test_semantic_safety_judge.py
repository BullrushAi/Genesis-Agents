from __future__ import annotations

from copy import deepcopy

import pytest

from runtime.semantic_safety_judge import (
    CANDIDATE_LOGICAL_MODEL,
    CANDIDATE_MODEL,
    ExactAnthropicTransportError,
    FINANCE_NARRATIVE_FIELDS,
    JUDGE_MODEL,
    JUDGE_LOGICAL_MODEL,
    JUDGE_TOOL_NAME,
    MAX_OUTPUT_TOKENS,
    MAX_PROJECTION_BYTES,
    POLICY_VERSION,
    PROJECTION_FIELD_NAMES,
    REASON_CONTRACT_FAILED,
    REASON_EXTERNAL_STATE,
    REASON_MODEL_MISMATCH,
    REASON_PROJECTION_TOO_LARGE,
    REASON_TRANSPORT_FAILED,
    REASON_UNCERTAIN,
    build_judge_request,
    canonical_sha256,
    consume_judge_response,
    judge_candidate_semantics,
    judge_tool_schema,
    project_candidate_narrative,
    require_exact_model_role_binding,
    role_contract_metadata,
)


def _candidate() -> dict:
    return {
        "agent_id": "genesis-fin-commissions",
        "task_id": "secret-task-id",
        "scope": "secret-tenant",
        "status": "ANALYSIS_COMPLETE",
        "summary": "ANALYSIS: The bounded commission analysis is complete.",
        "findings": ["ANALYSIS: A proposed allocation needs FinanceOS review."],
        "proposals": ["PROPOSAL: Recommend preparing a posting proposal."],
        "evidence_refs": ["secret-evidence-ref"],
        "assumptions": [],
        "conflicts": [],
        "unknowns": [],
        "confidence": 0.9,
        "risk_flags": [],
        "recommended_next_actions": ["NEXT: If approved, the Worker may post the journal."],
        "requested_human_decisions": [],
        "tool_calls_summary": ["secret tool output"],
        "finance_proposal": {
            "entity_id": "secret-entity",
            "period": "secret-period",
            "transaction_or_schedule_type": "ANALYSIS: commission accrual",
            "proposed_treatment": "PROPOSAL: Proposed accrual treatment.",
            "account_mapping": ["PROPOSAL: Proposed commission expense mapping."],
            "tracking_mapping": [],
            "tax_treatment": "PROPOSAL: No tax treatment asserted.",
            "amount_logic": "PROPOSAL: Use the approved schedule if supplied.",
            "source_refs": ["secret-source-ref"],
            "policy_refs": ["secret-policy-ref"],
            "assumptions": [],
            "confidence": 0.8,
            "exceptions": [],
            "requires_human_decision": False,
            "recommended_financeos_action": "NEXT: Submit the proposal for review.",
        },
    }


def _arguments(projection, *, decision="ALLOW", reason_code="NONE", offending_fields=None):
    return {
        "agent_id": projection.payload["agent_id"],
        "policy_version": POLICY_VERSION,
        "candidate_sha256": projection.candidate_sha256,
        "projection_sha256": projection.projection_sha256,
        "decision": decision,
        "reason_code": reason_code,
        "offending_fields": [] if offending_fields is None else offending_fields,
    }


def _response(projection, **argument_overrides):
    arguments = _arguments(projection)
    arguments.update(argument_overrides)
    return {
        "id": "provider-response-id",
        "model": JUDGE_MODEL,
        "stop_reason": "tool_use",
        "content": [
            {
                "type": "tool_use",
                "id": "provider-tool-id",
                "name": JUDGE_TOOL_NAME,
                "input": arguments,
            }
        ],
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def test_projection_is_pure_complete_and_privacy_minimal():
    candidate = _candidate()
    original = deepcopy(candidate)
    projection = project_candidate_narrative(candidate)

    assert candidate == original
    assert projection.payload["agent_id"] == candidate["agent_id"]
    assert projection.payload["policy_version"] == POLICY_VERSION
    assert set(projection.payload["fields"]) == {
        "summary", "findings", "proposals", "assumptions", "conflicts", "unknowns",
        "risk_flags", "recommended_next_actions", "requested_human_decisions",
        *{f"finance_proposal.{field}" for field in FINANCE_NARRATIVE_FIELDS if field != "confidence"},
    }
    serialized = str(projection.payload)
    for excluded in (
        "secret-task-id", "secret-tenant", "secret-evidence-ref", "secret tool output",
        "secret-entity", "secret-period", "secret-source-ref", "secret-policy-ref",
    ):
        assert excluded not in serialized
    assert projection.candidate_sha256 == canonical_sha256(candidate)
    assert projection.projection_sha256 == canonical_sha256(projection.payload)
    assert projection.projection_bytes <= MAX_PROJECTION_BYTES


def test_hashes_are_order_invariant_but_bind_every_candidate_value():
    candidate = _candidate()
    reordered = dict(reversed(list(candidate.items())))
    assert canonical_sha256(candidate) == canonical_sha256(reordered)
    changed = deepcopy(candidate)
    changed["task_id"] = "different-secret-id"
    assert canonical_sha256(candidate) != canonical_sha256(changed)


def test_request_is_exact_haiku_one_forced_nonexecuting_tool_and_untrusted_projection_only():
    projection = project_candidate_narrative(_candidate())
    request = build_judge_request(projection)
    assert request["model"] == JUDGE_MODEL
    assert request["max_tokens"] == MAX_OUTPUT_TOKENS == 256
    assert len(request["messages"]) == len(request["tools"]) == 1
    assert request["tool_choice"] == {
        "type": "tool", "name": JUDGE_TOOL_NAME, "disable_parallel_tool_use": True
    }
    assert request["tools"][0]["name"] == JUDGE_TOOL_NAME
    assert request["tools"][0]["input_schema"] == judge_tool_schema()
    assert "untrusted" in request["system"].lower()
    assert "secret-task-id" not in request["messages"][0]["content"]
    assert "secret-evidence-ref" not in request["messages"][0]["content"]


def test_judge_policy_explicitly_covers_finance_execution_paraphrase_family():
    request = build_judge_request(project_candidate_narrative(_candidate()))
    policy = request["system"].lower()
    for required_term in (
        "journal was posted or booked",
        "payment, payout, refund, or payroll",
        "filing was submitted",
        "approval was granted",
        "reconciliation or close was completed",
        "action was executed",
        "passive voice",
        "idioms",
        "euphemisms",
        "hedging",
        "attribution",
        "authoritative execution proof",
        "evidence_refs, provider submission, intent prefixes, and confident phrasing never suffice",
        "no bound trusted runtime observation exists in the projection",
        "return uncertain rather than allow",
    ):
        assert required_term in policy


def test_model_roles_share_exact_haiku_but_contracts_are_distinct_and_immutable():
    assert CANDIDATE_MODEL == "claude-haiku-4-5-20251001"
    assert CANDIDATE_LOGICAL_MODEL == "anthropic/claude-haiku-4.5"
    assert JUDGE_MODEL == "claude-haiku-4-5-20251001"
    assert JUDGE_LOGICAL_MODEL == "anthropic/claude-haiku-4.5"
    assert CANDIDATE_MODEL == JUDGE_MODEL
    assert CANDIDATE_LOGICAL_MODEL == JUDGE_LOGICAL_MODEL
    assert JUDGE_TOOL_NAME != "submit_genesis_result_v5"


@pytest.mark.parametrize("shared_binding", ["tool", "policy", "schema"])
def test_model_role_binding_fails_closed_when_role_contracts_are_shared(
    monkeypatch, shared_binding,
):
    import runtime.semantic_safety_judge as module

    if shared_binding == "tool":
        monkeypatch.setattr(module, "JUDGE_TOOL_NAME", "submit_genesis_result_v5")
    elif shared_binding == "policy":
        monkeypatch.setattr(module, "_SYSTEM_PROMPT", module.CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION)
    else:
        monkeypatch.setattr(
            module,
            "judge_tool_schema",
            lambda: module.canonical_result_tool_schema("genesis-knowledge-evidence")["input_schema"],
        )
    with pytest.raises(RuntimeError, match="^model_role_binding_invalid$"):
        require_exact_model_role_binding()
    with pytest.raises(RuntimeError, match="^model_role_binding_invalid$"):
        build_judge_request(project_candidate_narrative(_candidate()))


def test_role_contract_metadata_binds_distinct_tools_policies_and_schemas():
    metadata = role_contract_metadata("genesis-knowledge-evidence")
    assert metadata["candidate_tool"] != metadata["judge_tool"]
    assert metadata["candidate_policy_sha256"] != metadata["judge_policy_sha256"]
    assert metadata["candidate_schema_sha256"] != metadata["judge_schema_sha256"]
    assert all(len(str(value)) == 64 for key, value in metadata.items() if key.endswith("sha256"))


def test_judge_schema_is_closed_and_exact():
    schema = judge_tool_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["policy_version"]["enum"] == [POLICY_VERSION]
    assert schema["properties"]["offending_fields"]["items"]["enum"] == list(PROJECTION_FIELD_NAMES)


def test_exact_allow_is_accepted_with_content_free_metadata():
    projection = project_candidate_narrative(_candidate())
    verdict = consume_judge_response(_response(projection), projection)
    assert verdict.ok
    assert verdict.reason is None
    assert verdict.decision == "ALLOW"
    assert verdict.judge_reason_code == "NONE"
    assert verdict.offending_fields == ()
    assert verdict.actual_model == JUDGE_MODEL
    assert verdict.input_tokens == 100
    assert verdict.output_tokens == 20
    assert verdict.estimated_cost_usd == pytest.approx(0.0002)
    assert "bounded commission" not in repr(verdict)
    assert "secret" not in repr(verdict)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda response: response.update(stop_reason="max_tokens"),
        lambda response: response.update(stop_reason="refusal"),
        lambda response: response.update(content=[]),
        lambda response: response["content"].append({"type": "text", "text": "ALLOW"}),
        lambda response: response["content"].append(deepcopy(response["content"][0])),
        lambda response: response["content"][0].update(type="text"),
        lambda response: response["content"][0].update(name="wrong_tool"),
        lambda response: response["content"][0].update(input="malformed-json"),
        lambda response: response["content"][0]["input"].pop("policy_version"),
        lambda response: response["content"][0]["input"].update(extra="not-allowed"),
        lambda response: response["content"][0]["input"].update(decision="MAYBE"),
        lambda response: response["content"][0]["input"].update(reason_code="FREE_TEXT"),
        lambda response: response["content"][0]["input"].update(offending_fields=["task_id"]),
        lambda response: response["content"][0]["input"].update(offending_fields=["summary", "summary"]),
    ],
)
def test_malformed_text_fallback_multiple_refusal_and_schema_fail_closed(mutator):
    projection = project_candidate_narrative(_candidate())
    response = _response(projection)
    mutator(response)
    verdict = consume_judge_response(response, projection)
    assert not verdict.ok
    assert verdict.reason == REASON_CONTRACT_FAILED
    assert "ALLOW" not in repr(verdict) or verdict.decision == "ALLOW"


@pytest.mark.parametrize(
    "field,value",
    [
        ("agent_id", "genesis-fin-ap"),
        ("policy_version", "wrong-policy"),
        ("candidate_sha256", "0" * 64),
        ("projection_sha256", "f" * 64),
    ],
)
def test_binding_mismatch_fails_closed(field, value):
    projection = project_candidate_narrative(_candidate())
    verdict = consume_judge_response(_response(projection, **{field: value}), projection)
    assert not verdict.ok
    assert verdict.reason == REASON_CONTRACT_FAILED


def test_actual_provider_model_must_match_exactly():
    projection = project_candidate_narrative(_candidate())
    response = _response(projection)
    response["model"] = "claude-haiku-4-5"
    verdict = consume_judge_response(response, projection)
    assert not verdict.ok
    assert verdict.reason == REASON_MODEL_MISMATCH


def test_deny_and_uncertain_map_to_content_free_reasons():
    projection = project_candidate_narrative(_candidate())
    denied = consume_judge_response(
        _response(
            projection,
            decision="DENY",
            reason_code="AFFIRMATIVE_EXTERNAL_STATE",
            offending_fields=["summary"],
        ),
        projection,
    )
    uncertain = consume_judge_response(
        _response(
            projection,
            decision="UNCERTAIN",
            reason_code="AMBIGUOUS_EXTERNAL_STATE",
            offending_fields=["summary"],
        ),
        projection,
    )
    assert (denied.ok, denied.reason) == (False, REASON_EXTERNAL_STATE)
    assert (uncertain.ok, uncertain.reason) == (False, REASON_UNCERTAIN)


@pytest.mark.parametrize(
    "decision,reason_code,offending_fields",
    [
        ("ALLOW", "AFFIRMATIVE_EXTERNAL_STATE", []),
        ("ALLOW", "NONE", ["summary"]),
        ("DENY", "NONE", ["summary"]),
        ("DENY", "AFFIRMATIVE_EXTERNAL_STATE", []),
        ("UNCERTAIN", "NONE", ["summary"]),
    ],
)
def test_incoherent_decision_contract_fails_closed(decision, reason_code, offending_fields):
    projection = project_candidate_narrative(_candidate())
    verdict = consume_judge_response(
        _response(
            projection,
            decision=decision,
            reason_code=reason_code,
            offending_fields=offending_fields,
        ),
        projection,
    )
    assert not verdict.ok
    assert verdict.reason == REASON_CONTRACT_FAILED


@pytest.mark.asyncio
async def test_async_transport_is_called_exactly_once_and_candidate_is_unchanged():
    candidate = _candidate()
    original = deepcopy(candidate)
    calls = []

    async def transport(request):
        calls.append(request)
        projection = project_candidate_narrative(candidate)
        return _response(projection)

    verdict = await judge_candidate_semantics(candidate, transport=transport)
    assert verdict.ok
    assert len(calls) == 1
    assert candidate == original


@pytest.mark.asyncio
async def test_transport_exception_fails_closed_without_retry_or_content():
    calls = 0

    async def transport(_request):
        nonlocal calls
        calls += 1
        raise RuntimeError("provider body with secret narrative")

    verdict = await judge_candidate_semantics(_candidate(), transport=transport)
    assert calls == 1
    assert (verdict.ok, verdict.reason) == (False, REASON_TRANSPORT_FAILED)
    assert "provider body" not in repr(verdict)
    assert "secret narrative" not in repr(verdict)


@pytest.mark.asyncio
async def test_sealed_transport_metadata_survives_judge_verdict_without_content():
    async def transport(_request):
        raise ExactAnthropicTransportError(
            REASON_TRANSPORT_FAILED,
            stage="read_timeout", elapsed_ms=5_001.25,
        )

    verdict = await judge_candidate_semantics(_candidate(), transport=transport)
    assert (verdict.ok, verdict.reason) == (False, REASON_TRANSPORT_FAILED)
    assert verdict.transport_stage == "read_timeout"
    assert verdict.transport_elapsed_ms == 5_001.25
    assert verdict.http_status_class is None
    assert "request" not in repr(verdict).lower()


@pytest.mark.asyncio
async def test_projection_above_24000_utf8_bytes_fails_without_transport_call():
    candidate = _candidate()
    candidate["summary"] = "ANALYSIS: " + "🔥" * 7_000
    calls = 0

    async def transport(_request):
        nonlocal calls
        calls += 1
        raise AssertionError("must not call transport")

    projection = project_candidate_narrative(candidate)
    assert projection.projection_bytes > MAX_PROJECTION_BYTES
    verdict = await judge_candidate_semantics(candidate, transport=transport)
    assert calls == 0
    assert (verdict.ok, verdict.reason) == (False, REASON_PROJECTION_TOO_LARGE)


@pytest.mark.asyncio
async def test_non_json_candidate_fails_closed_without_transport_call():
    candidate = _candidate()
    candidate["unexpected"] = object()
    calls = 0

    async def transport(_request):
        nonlocal calls
        calls += 1
        return {}

    verdict = await judge_candidate_semantics(candidate, transport=transport)
    assert calls == 0
    assert (verdict.ok, verdict.reason) == (False, REASON_CONTRACT_FAILED)
