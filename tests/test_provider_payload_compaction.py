from __future__ import annotations

import json
import math

from bundle_loader import load_bundle
from canonical_profiles import CANONICAL_SLUGS, FINANCEOS_SLUGS, MASTER_OS_SLUGS
from canonical_result_contract import (
    CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION,
    FINANCE_COMPACT_RESULT_INSTRUCTION,
    MASTER_COMPACT_RESULT_INSTRUCTION,
    canonical_provider_result_json_schema,
    canonical_result_json_schema,
    validate_canonical_result,
)
from runtime.semantic_safety_judge import (
    FINANCE_CANDIDATE_OUTPUT_TOKENS,
    MASTER_CANDIDATE_OUTPUT_TOKENS,
    build_canonical_candidate_request,
)


def _estimate_tokens(body: dict) -> int:
    encoded = json.dumps(
        body, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    return math.ceil(len(encoded) / 4)


def test_all24_exact_candidate_wire_payloads_are_bounded() -> None:
    rows = {}
    for slug in sorted(CANONICAL_SLUGS):
        bundle = load_bundle(slug)
        body = build_canonical_candidate_request(
            slug=slug,
            messages=[
                {"role": "system", "content": bundle["system_prompt"]},
                {"role": "user", "content": "bounded provider payload diagnostic"},
            ],
            requested_max_tokens=8_000,
        )
        rows[slug] = _estimate_tokens(body)
        assert body['temperature'] == 0
        assert body["system"].count(CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION) == 1
        assert body["system"].count(FINANCE_COMPACT_RESULT_INSTRUCTION) == (
            1 if slug in FINANCEOS_SLUGS else 0
        )
        assert body["max_tokens"] == (
            FINANCE_CANDIDATE_OUTPUT_TOKENS
            if slug in FINANCEOS_SLUGS
            else MASTER_CANDIDATE_OUTPUT_TOKENS
        )
    assert set(rows) == CANONICAL_SLUGS
    assert max(rows.values()) <= 4_700
    assert max(rows[slug] for slug in FINANCEOS_SLUGS) <= 4_700
    assert max(rows[slug] for slug in MASTER_OS_SLUGS) <= 4_700


def test_finance_compact_instruction_is_exactly_12_of_24_and_complete() -> None:
    finance_occurrences = 0
    master_occurrences = 0
    for slug in sorted(CANONICAL_SLUGS):
        body = build_canonical_candidate_request(
            slug=slug,
            messages=[{"role": "user", "content": "bounded"}],
            requested_max_tokens=8_000,
        )
        count = body["system"].count(FINANCE_COMPACT_RESULT_INSTRUCTION)
        finance_occurrences += count if slug in FINANCEOS_SLUGS else 0
        master_occurrences += count if slug in MASTER_OS_SLUGS else 0
    assert finance_occurrences == 9
    assert master_occurrences == 0
    assert "Target <=2400 output tokens" in FINANCE_COMPACT_RESULT_INSTRUCTION
    assert "never omit any required field" in FINANCE_COMPACT_RESULT_INSTRUCTION
    assert "only for absent nullable structured non-narrative fields" in FINANCE_COMPACT_RESULT_INSTRUCTION
    assert "Every supplied narrative string must be non-empty" in FINANCE_COMPACT_RESULT_INSTRUCTION
    assert "prefix compliance overrides brevity" in FINANCE_COMPACT_RESULT_INSTRUCTION
    assert "source-bound" in FINANCE_COMPACT_RESULT_INSTRUCTION
    assert "even under a valid prefix" in FINANCE_COMPACT_RESULT_INSTRUCTION
    for term in (
        "posting", "payment", "filing", "invoice", "journal", "close", "execution",
        "approval", "verification", "completion",
    ):
        assert term in FINANCE_COMPACT_RESULT_INSTRUCTION
    assert "Genesis has no trusted runtime observation here" in FINANCE_COMPACT_RESULT_INSTRUCTION


def test_prefix_taxonomy_precedes_finance_brevity_clause_exactly_once() -> None:
    body = build_canonical_candidate_request(
        slug="genesis-fin-payroll-tax",
        messages=[{"role": "user", "content": "bounded"}],
        requested_max_tokens=8_000,
    )
    system = body["system"]
    assert system.count(CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION) == 1
    assert system.count(FINANCE_COMPACT_RESULT_INSTRUCTION) == 1
    assert system.index(CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION) < system.index(FINANCE_COMPACT_RESULT_INSTRUCTION)


def test_master_compact_instruction_is_exactly_12_of_24() -> None:
    master_occurrences = finance_occurrences = 0
    for slug in sorted(CANONICAL_SLUGS):
        body = build_canonical_candidate_request(
            slug=slug, messages=[{"role": "user", "content": "bounded"}],
            requested_max_tokens=8_000,
        )
        count = body["system"].count(MASTER_COMPACT_RESULT_INSTRUCTION)
        master_occurrences += count if slug in MASTER_OS_SLUGS else 0
        finance_occurrences += count if slug in FINANCEOS_SLUGS else 0
        if slug in MASTER_OS_SLUGS:
            assert body["system"].index(CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION) < body["system"].index(MASTER_COMPACT_RESULT_INSTRUCTION)
    assert master_occurrences == 10
    assert finance_occurrences == 0


def test_master_schema_does_not_publish_unenforced_compaction_limits():
    for slug in sorted(MASTER_OS_SLUGS):
        serialized = json.dumps(canonical_provider_result_json_schema(slug))
        assert '"maxItems"' not in serialized
        assert '"maxLength"' not in serialized


def test_master_schema_and_runtime_preserve_multiple_conflicts_and_long_narratives() -> None:
    for slug in (
        "genesis-automation-architect", "genesis-knowledge-evidence",
        "genesis-knowledge-evidence",
    ):
        value = {
            "agent_id": slug, "task_id": "bounded", "scope": "source-bound review",
            "status": "ANALYSIS_COMPLETE", "summary": "ANALYSIS: Bounded source review is complete.",
            "findings": ["ANALYSIS: One material finding."],
            "proposals": ["PROPOSAL: Review one bounded option."],
            "evidence_refs": ["source:1"], "assumptions": [], "conflicts": [],
            "unknowns": [], "confidence": 0.8, "risk_flags": [],
            "recommended_next_actions": ["NEXT: Review the bounded result."],
            "requested_human_decisions": [], "tool_calls_summary": [],
            "clarification_needed": False,
        }
        assert validate_canonical_result(slug, value).ok
        schema = canonical_result_json_schema(slug)
        assert not list(__import__("jsonschema").Draft202012Validator(schema).iter_errors(value))
        overflow = dict(value)
        overflow["summary"] = "ANALYSIS: " + ("x" * 200)
        overflow["conflicts"] = ["RISK: First material conflict.", "RISK: Independent second material conflict."]
        assert not list(__import__("jsonschema").Draft202012Validator(schema).iter_errors(overflow))
        assert validate_canonical_result(slug, overflow).ok
        overflow["conflicts"].append("")
        assert list(__import__("jsonschema").Draft202012Validator(schema).iter_errors(overflow))
        assert not validate_canonical_result(slug, overflow).ok


def test_provider_schema_uses_compact_prefix_patterns_without_repeated_guidance() -> None:
    for slug in sorted(CANONICAL_SLUGS):
        schema = canonical_provider_result_json_schema(slug)
        serialized = json.dumps(schema, separators=(",", ":"))
        assert serialized.count('"pattern"') == (18 if slug in FINANCEOS_SLUGS else 9)
        assert '"description":"Every supplied' not in serialized
        assert serialized.count("bounded Genesis analysis") <= 1
        assert len(serialized.encode("utf-8")) < (9_000 if slug in FINANCEOS_SLUGS else 6_000)


def test_candidate_instruction_defines_taxonomy_once() -> None:
    instruction = CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION
    for prefix in (
        "ANALYSIS:", "PROPOSAL:", "UNKNOWN:", "RISK:", "NEXT:",
        "DECISION_NEEDED:",
    ):
        assert instruction.count(prefix) == 1
    assert "prefix does not prove safety" in instruction.lower()
    assert "external work" in instruction.lower()
    assert instruction.count("emit the required clarification key exactly once") == 1
    assert instruction.count("clarification key exactly once with value null") == 1
    assert instruction.count("Never emit legacy flat clarification controls") == 1
