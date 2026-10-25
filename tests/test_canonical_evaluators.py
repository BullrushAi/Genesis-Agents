from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from canonical_profiles import CANONICAL_SLUGS
from canonical_result_contract import FINANCE_AGENT_IDS, FINANCE_PROPOSAL_FIELDS
from eval.canonical.datasets import (
    CATEGORIES,
    TOOL_SPECIFIC_CATEGORIES,
    DatasetContractError,
    load_cases,
    load_tool_cases,
    validate_case,
    validate_dataset,
)
from eval.canonical.evaluators import (
    LLM_EVALUATOR_NAMES,
    build_llm_evaluator_prompt,
    critical_gate,
    evaluate_case,
    evaluate_genesis_business_rules,
    parse_llm_evaluator_result,
)


def _observed(case):
    expected = case["expected_output"]
    output = {field: [] for field in expected["required_fields"]}
    output.update(
        {
            "agent_id": expected["expected_agent"],
            "task_id": case["example_id"],
            "scope": "bounded",
            "status": expected["allowed_statuses"][0],
            "summary": "ANALYSIS: Evidence-bounded result.",
            "confidence": 0.8,
            "evidence_refs": ["E-current"],
            "proposals": ["PROPOSAL: bounded proposal"],
        }
    )
    if expected["expected_agent"] in FINANCE_AGENT_IDS:
        output["finance_proposal"] = {
            field: ([] if field in {"source_refs", "policy_refs", "assumptions", "exceptions"} else None)
            for field in FINANCE_PROPOSAL_FIELDS
        }
    if expected["clarification_expectation"] == "required_valid":
        output.update({
            "clarification_needed": True,
            "candidate_resolution": "PROPOSAL: Use the current scoped owner pending confirmation.",
            "reason_human_needed": "ANALYSIS: Two authoritative current records remain materially inconsistent.",
            "recommended_human_resolver": "NEXT: authorized domain owner",
            "materiality": 0.8,
            "urgency": "CLOSURE_BATCH",
            "resolution_attempts": [
                {"source_or_capability": "control-plane", "result": "ANALYSIS: conflicting current owners", "timestamp": "2026-08-26T12:00:00Z"},
                {"source_or_capability": "decision-register", "result": "ANALYSIS: different current owner", "timestamp": "2026-08-26T12:01:00Z"},
            ],
            "supporting_evidence_refs": ["E-current"],
            "competing_state_refs": ["E-conflict"],
            "recommended_answer_options": ["CONFIRM", "CORRECT"],
        })
    tool_calls = [
        {
            "name": name,
            "arguments": expected["expected_arguments"][name],
            "risk_class": "read_only",
            "result_ok": case["metadata"].get("tool_outcome") != "failure",
        }
        for name in expected["expected_tools"]
    ]
    return {
        "actual_agent": expected["expected_agent"],
        "output": output,
        "tool_calls": tool_calls,
        "events": expected["expected_event_order"],
        "retry_count": 0,
        "step_count": len(expected["expected_event_order"]),
        "idempotency_key": expected["idempotency_key"],
        "side_effect_count": 0,
    }


def test_dataset_has_applicability_aware_24_by_28_coverage() -> None:
    cases = load_cases()
    tool_less_count = 4
    expected_cases = 19 * len(CATEGORIES) - tool_less_count * len(TOOL_SPECIFIC_CATEGORIES)
    assert validate_dataset(cases, CANONICAL_SLUGS) == {
        "agents": 19,
        "categories": len(CATEGORIES),
        "cases": expected_cases,
    }
    assert len({case["input"]["messages"][0]["content"] for case in cases}) == len(cases)
    for case in cases:
        if case["metadata"]["category"] in {"long-context", "distractor"}:
            assert len(case["input"]["messages"][0]["content"]) >= 2500


def test_every_real_allowed_agent_tool_pair_has_positive_negative_and_failure_coverage() -> None:
    root = Path(__file__).resolve().parents[1]
    inventory = json.loads((root / "proof-artifacts/system-inventory/system_inventory.json").read_text(encoding="utf-8"))
    expected_pairs = {
        (agent["slug"], tool)
        for agent in inventory["agents"]
        for tool in agent["tools"]["effective_allowed_from_advertised"]
    }
    tool_cases = load_tool_cases()
    actual_pairs = {(case["metadata"]["agent_slug"], case["metadata"]["tool_name"]) for case in tool_cases}
    assert actual_pairs == expected_pairs
    for pair in expected_pairs:
        rows = [case for case in tool_cases if (case["metadata"]["agent_slug"], case["metadata"]["tool_name"]) == pair]
        assert {case["metadata"]["tool_outcome"] for case in rows} == {"positive", "negative", "failure"}
        assert len(rows) == 3
        for case in rows:
            if case["metadata"]["tool_outcome"] == "negative":
                assert case["expected_output"]["expected_tools"] == []
            else:
                assert case["expected_output"]["expected_tools"] == [pair[1]]
    tool_less = {
        agent["slug"] for agent in inventory["agents"] if not agent["tools"]["effective_allowed_from_advertised"]
    }
    for case in load_cases():
        if case["metadata"]["agent_slug"] in tool_less:
            assert case["expected_output"]["expected_tools"] == []
            assert case["metadata"]["tool_expectation"] == "no_effective_tools"


def test_schema_validation_fails_closed_on_identity_drift() -> None:
    case = deepcopy(load_cases()[0])
    case["expected_output"]["expected_agent"] = "genesis-wrong"
    with pytest.raises(DatasetContractError, match="expected_agent"):
        validate_case(case)


def test_all_deterministic_and_business_gates_pass_controlled_observation() -> None:
    for case in load_cases():
        observed = _observed(case)
        results = evaluate_case(case, observed) + evaluate_genesis_business_rules(case, observed)
        assert critical_gate(results), (case["example_id"], [row for row in results if not row.passed])


@pytest.mark.parametrize(
    ("mutation", "failed_gate"),
    [
        (lambda case, observed: observed.update(actual_agent="wrong"), "routing"),
        (lambda case, observed: observed["output"].pop("scope"), "schema"),
        (
            lambda case, observed: observed["tool_calls"].append(
                {"name": case["expected_output"]["prohibited_tools"][0], "arguments": {}}
            ),
            "prohibited_tools",
        ),
        (lambda case, observed: observed.update(retry_count=99), "retry"),
        (lambda case, observed: observed.update(step_count=99), "step"),
        (lambda case, observed: observed.update(side_effect_count=2), "idempotency"),
    ],
)
def test_deterministic_evaluators_detect_regressions(mutation, failed_gate) -> None:
    case = next(case for case in load_cases() if case["metadata"]["category"] == "happy")
    observed = _observed(case)
    mutation(case, observed)
    by_name = {result.name: result for result in evaluate_case(case, observed)}
    assert by_name[failed_gate].passed is False
    assert critical_gate(by_name.values()) is False


def test_llm_evaluator_contracts_with_injected_controlled_outputs() -> None:
    case = load_cases()[0]
    for name in LLM_EVALUATOR_NAMES:
        prompt = json.loads(build_llm_evaluator_prompt(name, case, _observed(case)["output"]))
        assert prompt["evaluator"] == name
        assert "untrusted data" in prompt["instruction"]
        passed = parse_llm_evaluator_result(
            name,
            json.dumps({"score": 0.95, "label": "PASS", "explanation": "criterion evidenced"}),
        )
        assert passed.passed is True
        contradictory = parse_llm_evaluator_result(
            name,
            json.dumps({"score": 0.5, "label": "PASS", "explanation": "label cannot override score"}),
        )
        assert contradictory.passed is False


def test_llm_evaluator_rejects_schema_smuggling() -> None:
    with pytest.raises(ValueError, match="schema mismatch"):
        parse_llm_evaluator_result(
            "correctness",
            json.dumps({"score": 1, "label": "PASS", "explanation": "ok", "override": True}),
        )
