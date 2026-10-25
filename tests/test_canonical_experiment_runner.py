from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from eval.canonical.datasets import REQUIRED_OUTPUT_FIELDS, load_cases, load_tool_cases
from eval.canonical.contracts import (
    GENESIS_OUTPUT_CONTRACT,
    JUDGE_RESULTS_CONTRACT,
    SEMANTIC_JUDGE_CONTRACT,
    genesis_output_contract,
)
from canonical_result_contract import (
    CLARIFICATION_FIELDS,
    FINANCE_AGENT_IDS,
    FINANCE_COMPACT_RESULT_INSTRUCTION,
    FINANCE_PROPOSAL_FIELDS,
    project_provider_canonical_result,
)
from eval.canonical.experiments import (
    ExperimentConfig,
    ExperimentContractError,
    aggregate_results,
    candidate_prompt,
    execute_case,
    failed_record,
    projected_cost_usd,
    repeatability_summary,
    select_baseline_cases,
    select_repeatability_cases,
    select_tool_matrix_cases,
)
from eval.canonical.model_catalog import CANDIDATE_LOGICAL_MODEL, JUDGE_LOGICAL_MODEL
from runtime.anthropic_wire import ANTHROPIC_PRICING_VERSION
from runtime.semantic_safety_judge import POLICY_VERSION, project_candidate_narrative

from scripts.run_canonical_experiments import (
    ExperimentRunAbort,
    run_records,
)




def test_empty_target_lane_does_not_rewrite_shared_checkpoint(tmp_path) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text('{"sentinel":true}\n', encoding="utf-8")
    rows = asyncio.run(
        run_records([], 1, None, _config(), checkpoint_path=checkpoint, cohort_name="empty")
    )
    assert rows == []
    assert json.loads(checkpoint.read_text(encoding="utf-8")) == {"sentinel": True}


def _candidate(case, *, omit: str | None = None):
    output = {field: [] for field in REQUIRED_OUTPUT_FIELDS}
    output.update(
        {
            "agent_id": case["metadata"]["agent_slug"],
            "task_id": case["example_id"],
            "scope": "bounded synthetic evaluation",
            "status": case["expected_output"]["allowed_statuses"][0],
            "summary": "ANALYSIS: Evidence-bounded result.",
            "evidence_refs": ["E-current"],
            "proposals": ["PROPOSAL: bounded proposal"],
            "confidence": 0.8,
            "clarification_needed": False,
        }
    )
    if case["metadata"]["agent_slug"] in FINANCE_AGENT_IDS:
        output["finance_proposal"] = {
            field: ([] if field in {"source_refs", "policy_refs", "assumptions", "exceptions"} else None)
            for field in FINANCE_PROPOSAL_FIELDS
        }
    if case["expected_output"]["clarification_expectation"] == "required_valid":
        output.update({
            "clarification_needed": True,
            "candidate_resolution": "PROPOSAL: Use the scoped owner pending confirmation.",
            "reason_human_needed": "ANALYSIS: Current authoritative records materially conflict.",
            "recommended_human_resolver": "NEXT: authorized domain owner",
            "materiality": 0.8,
            "urgency": "CLOSURE_BATCH",
            "resolution_attempts": [{"source_or_capability": "control-plane", "result": "ANALYSIS: current records conflict", "timestamp": "2026-08-26T12:00:00Z"}],
            "supporting_evidence_refs": ["E-current"],
            "competing_state_refs": ["E-conflict"],
            "recommended_answer_options": ["CONFIRM", "CORRECT"],
        })
    if omit:
        output.pop(omit)
    return output


def _provider_candidate(candidate: dict) -> dict:
    provider = deepcopy(candidate)
    needed = provider.pop("clarification_needed")
    provider["clarification"] = (
        {field: provider.pop(field) for field in CLARIFICATION_FIELDS[1:]}
        if needed else None
    )
    return provider


def _judge_payload():
    names = (
        "correctness",
        "faithfulness",
        "hallucination",
        "relevance",
        "tool_use",
        "conciseness",
        "refusal",
        "instruction_adherence",
    )
    return {"results": [{"name": name, "score": 0.95, "label": "PASS", "explanation": "evidenced"} for name in names]}


TEST_JUDGE_MODEL = "openai/test-judge"


def _config(**kwargs):
    return ExperimentConfig(
        dataset_name="generic-test",
        judge_model=TEST_JUDGE_MODEL,
        input_usd_per_million=3.0,
        output_usd_per_million=15.0,
        **kwargs,
    )


def _safety_judge_payload(candidate):
    projection = project_candidate_narrative(candidate)
    return {
        "agent_id": candidate["agent_id"],
        "policy_version": POLICY_VERSION,
        "candidate_sha256": projection.candidate_sha256,
        "projection_sha256": projection.projection_sha256,
        "decision": "ALLOW",
        "reason_code": "NONE",
        "offending_fields": [],
    }


def _submit_response(payload, model, contract, *, usage=None):
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "submit-1",
                            "type": "function",
                            "function": {
                                "name": contract.tool_name,
                                "arguments": json.dumps(payload),
                            },
                        }
                    ],
                },
            }
        ],
        "model": model,
        "usage": usage or {"total_tokens": 1},
    }


def test_real_subset_selection_is_transparent_and_covers_tools() -> None:
    cases = load_cases()
    baseline = select_baseline_cases(cases)
    repeats = select_repeatability_cases(cases)
    tools = select_tool_matrix_cases(load_tool_cases())
    assert len(baseline) == 19
    assert len({case["metadata"]["agent_slug"] for case in baseline}) == 19
    assert len(repeats) == 4
    assert tools
    assert all(case["metadata"]["tool_outcome"] in {"positive", "failure"} for case in tools)
    assert len(tools) * 3 == len(load_tool_cases()) * 2
    platform = next(case for case in baseline if case["metadata"]["agent_slug"] == "genesis-platform-engineer")
    assert platform["metadata"]["category"] == "happy"
    assert platform["expected_output"]["expected_tools"] == []


def test_candidate_and_judge_configuration_must_be_independent() -> None:
    with pytest.raises(ExperimentContractError, match="must_differ"):
        ExperimentConfig(candidate_model="same", judge_model="same").validate()


def test_default_anthropic_roles_are_exact_dual_haiku_with_independent_contracts() -> None:
    config = ExperimentConfig()
    assert config.candidate_model == CANDIDATE_LOGICAL_MODEL == "anthropic/claude-haiku-4.5"
    assert config.judge_model == JUDGE_LOGICAL_MODEL == "anthropic/claude-haiku-4.5"
    config.validate()


@pytest.mark.parametrize(
    "override",
    [
        {"provider": "openai"},
        {"judge_model": TEST_JUDGE_MODEL},
        {"max_contract_retries": 1},
    ],
)
def test_canonical_dataset_cannot_enter_generic_provider_model_or_retry_path(override) -> None:
    with pytest.raises(ExperimentContractError):
        ExperimentConfig(**override).validate()


def test_automation_bundle_has_a_mechanical_output_bound() -> None:
    from bundle_loader import load_bundle

    prompt = load_bundle("genesis-automation-architect")["system_prompt"]
    assert "within 800 output tokens" in prompt
    assert "each array has at most 2" in prompt
    assert "empty array when no evidenced item exists" in prompt
    assert "if evidence_refs is empty" in prompt
    assert "it MUST NOT use the superseded completion status" in prompt
    assert "atomic idempotency state machine" in prompt


def test_automation_happy_prompt_cannot_offer_completion_without_evidence() -> None:
    case = next(
        case
        for case in load_cases()
        if case["example_id"] == "genesis-automation-architect::happy"
    )
    prompt = json.loads(candidate_prompt(case))
    assert prompt["allowed_statuses"] == ["PARTIAL"]
    assert any("evidence_refs MUST be empty" in instruction for instruction in prompt["instructions"])


def test_projected_cost_aborts_above_authorized_cap() -> None:
    config = ExperimentConfig(spend_cap_usd=10.0)
    assert projected_cost_usd(46, config) == 1.403
    assert projected_cost_usd(1000, config) > config.spend_cap_usd


def test_execute_case_scores_actual_provider_record_not_expected_fixture(monkeypatch) -> None:
    case = next(case for case in load_cases() if case["example_id"] == "genesis-fin-ap::happy")
    calls = []

    async def caller(model, messages, max_tokens, tools):
        calls.append({"model": model, "tools": tools, "messages": messages})
        payload = _candidate(case) if model != TEST_JUDGE_MODEL else _judge_payload()
        contract = GENESIS_OUTPUT_CONTRACT if model != TEST_JUDGE_MODEL else JUDGE_RESULTS_CONTRACT
        return _submit_response(payload, model, contract, usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})

    result = asyncio.run(execute_case(case, repetition=1, call_model=caller, config=_config()))
    by_name = {row["name"]: row for row in result["deterministic_scores"]}
    assert by_name["schema"]["passed"] is True
    assert calls[0]["model"] != calls[1]["model"]
    assert calls[0]["tools"][0]["function"]["name"] == "submit_genesis_output_v5"
    assert calls[1]["tools"][0]["function"]["name"] == "submit_genesis_judgement_v1"
    assert result["candidate_role_binding_sha256"] != result["judge_role_binding_sha256"]
    assert result["candidate_usage"]["total_tokens"] == 15
    assert result["candidate_cost_usd"] == 0.000035
    assert result["candidate_pricing_version"] == ANTHROPIC_PRICING_VERSION
    # The custom test judge is deliberately priced through the configurable
    # fallback rates; canonical dual-Haiku pricing is covered separately.
    assert result["judge_cost_usd"] == 0.000105
    assert result["judge_pricing_version"] == "configured-rates"
    assert result["total_cost_usd"] == 0.00014


def test_default_dual_haiku_no_tool_case_makes_one_call_per_distinct_role_contract(monkeypatch) -> None:
    case = next(case for case in load_cases() if case["example_id"] == "genesis-fin-ap::happy")
    calls = []

    async def caller(model, messages, max_tokens, tools):
        tool_name = tools[0]["function"]["name"]
        calls.append({"model": model, "tool_name": tool_name, "messages": messages})
        if tool_name == genesis_output_contract(case["metadata"]["agent_slug"]).tool_name:
            contract = genesis_output_contract(case["metadata"]["agent_slug"])
            return _submit_response(_provider_candidate(_candidate(case)), model, contract)
        projected = project_provider_canonical_result(
            case["metadata"]["agent_slug"], _provider_candidate(_candidate(case)),
        )
        return _submit_response(_safety_judge_payload(projected), model, SEMANTIC_JUDGE_CONTRACT)

    result = asyncio.run(execute_case(case, repetition=1, call_model=caller, config=ExperimentConfig()))
    assert [(row["model"], row["tool_name"]) for row in calls] == [
        (CANDIDATE_LOGICAL_MODEL, "submit_genesis_result_v5"),
        (JUDGE_LOGICAL_MODEL, "submit_genesis_safety_judgment_v1"),
    ]
    candidate_system = "\n".join(
        str(row.get("content") or "")
        for row in calls[0]["messages"] if row.get("role") == "system"
    )
    assert candidate_system.count(FINANCE_COMPACT_RESULT_INSTRUCTION) == 1
    assert result["candidate_model_actual"] == result["judge_model_actual"] == CANDIDATE_LOGICAL_MODEL
    assert result["candidate_contract_sha256"] != result["judge_contract_sha256"]
    assert result["candidate_role_binding_sha256"] != result["judge_role_binding_sha256"]
    assert result["clarification_representation_subtype"] == "canonical_object"


def test_canonical_experiment_accepts_complete_legacy_flat_shape_with_safe_subtype(monkeypatch) -> None:
    case = next(case for case in load_cases() if case["example_id"] == "genesis-fin-ap::happy")
    slug = case["metadata"]["agent_slug"]
    flat = project_provider_canonical_result(slug, _provider_candidate(_candidate(case)))

    async def caller(model, messages, max_tokens, tools):
        contract = genesis_output_contract(slug)
        if tools[0]["function"]["name"] == contract.tool_name:
            return _submit_response(flat, model, contract)
        return _submit_response(_safety_judge_payload(flat), model, SEMANTIC_JUDGE_CONTRACT)

    result = asyncio.run(execute_case(
        case, repetition=1, call_model=caller, config=ExperimentConfig(),
    ))
    assert result["clarification_representation_subtype"] == "legacy_flat"
    assert result["clarification_representation_missing_fields"] == []
    assert result["clarification_representation_extra_fields"] == []
    assert "candidate_resolution" not in result


def test_canonical_experiment_failure_records_only_safe_representation_identifiers(monkeypatch) -> None:
    case = next(case for case in load_cases() if case["example_id"] == "genesis-fin-ap::happy")
    slug = case["metadata"]["agent_slug"]
    hostile = _provider_candidate(_candidate(case))
    hostile["clarification"] = {"clarification_needed": "private-value-must-not-escape"}

    async def caller(model, messages, max_tokens, tools):
        return _submit_response(hostile, model, genesis_output_contract(slug))

    with pytest.raises(ExperimentContractError) as caught:
        asyncio.run(execute_case(
            case, repetition=1, call_model=caller, config=ExperimentConfig(),
        ))
    record = failed_record(case, 1, caught.value)
    assert record["clarification_representation_subtype"] == "canonical_invalid"
    assert record["clarification_representation_extra_fields"] == ["clarification_needed"]
    assert record["clarification_representation_extra_field_count"] == 1
    assert record["clarification_representation_missing_field_count"] == 9
    assert "private-value-must-not-escape" not in json.dumps(record)


def test_execute_case_rejects_candidate_actual_model_mismatch_before_judge(monkeypatch) -> None:
    case = next(case for case in load_cases() if case["example_id"] == "genesis-fin-ap::happy")
    calls = 0

    async def caller(model, messages, max_tokens, tools):
        nonlocal calls
        calls += 1
        return _submit_response(_candidate(case), "same-actual-model", GENESIS_OUTPUT_CONTRACT)

    with pytest.raises(ExperimentContractError, match="actual_model_mismatch"):
        asyncio.run(execute_case(case, repetition=1, call_model=caller, config=_config()))
    assert calls == 1


def test_deterministic_candidate_contract_failure_has_zero_retries_and_redacted_evidence(monkeypatch) -> None:
    case = next(case for case in load_cases() if case["example_id"] == "genesis-fin-ap::happy")
    calls = 0

    async def caller(model, messages, max_tokens, tools):
        nonlocal calls
        calls += 1
        return {
            "model": model,
            "provider_detail": "api_key=must-not-persist",
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }

    config = _config(max_contract_retries=1, contract_retry_backoff_seconds=0)
    result = asyncio.run(run_records([case], 1, caller, config))[0]
    assert calls == 1
    assert result["error"] == "ExperimentContractError"
    assert result["trace_id"]
    assert result["total_latency_ms"] > 0
    assert result["candidate_usage"]["total_tokens"] == 3
    assert result["retry_count"] == 0
    assert [row["error"] for row in result["contract_attempts"]] == ["provider_choices_missing"]
    assert "must-not-persist" not in json.dumps(result)


def test_transient_judge_transport_failure_retries_once_without_losing_attempt(monkeypatch) -> None:
    case = next(case for case in load_cases() if case["example_id"] == "genesis-fin-ap::happy")
    calls = 0

    async def caller(model, messages, max_tokens, tools):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _submit_response(_candidate(case), model, GENESIS_OUTPUT_CONTRACT, usage={"total_tokens": 5})
        elif calls == 2:
            raise ExperimentContractError("provider_timeout", transient=True)
        return _submit_response(_judge_payload(), model, JUDGE_RESULTS_CONTRACT, usage={"total_tokens": 5})

    result = asyncio.run(
        execute_case(
            case,
            repetition=1,
            call_model=caller,
            config=_config(max_contract_retries=1, contract_retry_backoff_seconds=0),
        )
    )
    assert calls == 3
    assert result["retry_count"] == 1
    judge_attempts = [row for row in result["contract_attempts"] if row["phase"] == "judge"]
    assert [row["error"] for row in judge_attempts] == ["provider_timeout", None]
    assert result["judge_usage"]["total_tokens"] == 5


def test_known_provider_data_envelope_recovers_for_forced_tool_without_recall(monkeypatch) -> None:
    case = next(case for case in load_cases() if case["example_id"] == "genesis-fin-ap::happy")
    calls = 0

    async def caller(model, messages, max_tokens, tools):
        nonlocal calls
        calls += 1
        payload = _candidate(case) if calls == 1 else _judge_payload()
        contract = GENESIS_OUTPUT_CONTRACT if calls == 1 else JUDGE_RESULTS_CONTRACT
        response = _submit_response(payload, model, contract, usage={"total_tokens": 2})
        if calls == 1:
            return {"data": response}
        return response

    result = asyncio.run(execute_case(case, repetition=1, call_model=caller, config=_config()))
    assert calls == 2
    assert result["error"] is None
    assert result["retry_count"] == 0
    assert result["candidate_usage"]["total_tokens"] == 2


def test_default_retry_budget_fails_closed_after_one_transient_call(monkeypatch) -> None:
    case = next(case for case in load_cases() if case["example_id"] == "genesis-fin-ap::happy")

    candidate_calls = 0

    async def recovery_caller(model, messages, max_tokens, tools):
        nonlocal candidate_calls
        if model != TEST_JUDGE_MODEL:
            candidate_calls += 1
            if candidate_calls == 1:
                raise ExperimentContractError("provider_http_503", transient=True)
            return _submit_response(_candidate(case), model, GENESIS_OUTPUT_CONTRACT)
        return _submit_response(_judge_payload(), model, JUDGE_RESULTS_CONTRACT)

    failed = asyncio.run(run_records([case], 1, recovery_caller, _config(concurrency=1)))
    assert ExperimentConfig().max_contract_retries == 0
    assert failed[0]["error"] == "ExperimentContractError"
    assert candidate_calls == 1
    assert failed[0]["retry_count"] == 0


@pytest.mark.parametrize("abort_reason", ["rolling_spend_projection_exceeded", "fingerprint_changed"])
def test_abort_guard_runs_after_atomic_redacted_row_checkpoint(monkeypatch, tmp_path, abort_reason) -> None:
    case = next(case for case in load_cases() if case["example_id"] == "genesis-fin-ap::happy")
    checkpoint = tmp_path / f"{abort_reason}.json"

    async def caller(model, messages, max_tokens, tools):
        return {
            "provider_payload": "api_key=must-not-persist",
            "usage": {"total_tokens": 3, "cost": 0.01},
        }

    with pytest.raises(ExperimentRunAbort, match=abort_reason):
        asyncio.run(
            run_records(
                [case],
                2,
                caller,
                _config(concurrency=1, max_contract_retries=0),
                checkpoint_path=checkpoint,
                cohort_name="abort-proof",
                post_checkpoint_guard=lambda rows: abort_reason if rows else None,
            )
        )
    raw = checkpoint.read_text(encoding="utf-8")
    saved = json.loads(raw)
    assert saved["status"] == "aborted"
    assert saved["reason"] == abort_reason
    assert saved["completed_records"] >= 1
    assert saved["rows"][0]["critical_pass"] is False
    assert saved["rows"][0]["error"] == "ExperimentContractError"
    assert saved["rows"][0]["trace_id"]
    assert saved["rows"][0]["total_latency_ms"] > 0
    assert saved["rows"][0]["total_cost_usd"] == 0.01
    assert saved["rows"][0]["failed_deterministic_names"] == []
    assert saved["rows"][0]["failed_business_names"] == []
    assert saved["rows"][0]["failed_llm_scores"] == []
    assert "must-not-persist" not in raw


def test_deterministic_judge_and_evaluator_failures_are_not_retried(monkeypatch) -> None:
    case = next(case for case in load_cases() if case["example_id"] == "genesis-fin-ap::happy")
    calls = 0

    async def judge_tool_caller(model, messages, max_tokens, tools):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _submit_response(_candidate(case), model, GENESIS_OUTPUT_CONTRACT)
        return {
            "choices": [{"message": {"content": None, "tool_calls": [{"id": "j1", "function": {"name": "forbidden", "arguments": "{}"}}]}}],
            "model": model,
            "usage": {"total_tokens": 1},
        }

    with pytest.raises(ExperimentContractError, match="unexpected_submit_tool") as caught:
        asyncio.run(
            execute_case(
                case,
                repetition=1,
                call_model=judge_tool_caller,
                config=_config(max_contract_retries=1, contract_retry_backoff_seconds=0),
            )
        )
    assert calls == 2
    assert caught.value.transient is False
    assert len([row for row in caught.value.attempt_evidence if row["phase"] == "judge"]) == 1

    evaluator_calls = 0

    async def evaluator_failure_caller(model, messages, max_tokens, tools):
        nonlocal evaluator_calls
        evaluator_calls += 1
        if evaluator_calls == 1:
            payload = _candidate(case)
            payload["status"] = "HOLD"
            return _submit_response(payload, model, GENESIS_OUTPUT_CONTRACT)
        return _submit_response(_judge_payload(), model, JUDGE_RESULTS_CONTRACT)

    result = asyncio.run(
        execute_case(
            case,
            repetition=1,
            call_model=evaluator_failure_caller,
            config=_config(max_contract_retries=1, contract_retry_backoff_seconds=0),
        )
    )
    assert evaluator_calls == 2
    assert result["critical_pass"] is False
    assert result["retry_count"] == 0


def test_tool_positive_case_uses_actual_tool_call_and_arguments(monkeypatch) -> None:
    case = next(case for case in load_cases() if case["example_id"] == "genesis-platform-engineer::correct-tool")
    count = 0

    async def successful_formatter(**kwargs):
        return {"ok": True, "content": "x = 1\n"}

    import tools

    real_get_tool = tools.get_tool
    monkeypatch.setattr(tools, "get_tool", lambda name: successful_formatter if name == "code_format" else real_get_tool(name))

    async def caller(model, messages, max_tokens, tools):
        nonlocal count
        count += 1
        if count == 1:
            return {
                "choices": [{"message": {"content": None, "tool_calls": [{"id": "t1", "function": {"name": "code_format", "arguments": json.dumps(case["expected_output"]["expected_arguments"]["code_format"])}}]}}],
                "model": model,
                "usage": {"total_tokens": 10},
            }
        payload = _candidate(case) if count == 2 else _judge_payload()
        contract = GENESIS_OUTPUT_CONTRACT if count == 2 else JUDGE_RESULTS_CONTRACT
        return _submit_response(payload, model, contract, usage={"total_tokens": 10})

    result = asyncio.run(execute_case(case, repetition=1, call_model=caller, config=_config()))
    deterministic = {row["name"]: row for row in result["deterministic_scores"]}
    assert deterministic["arguments"]["passed"] is True
    assert deterministic["order"]["passed"] is True
    assert result["actual_tool_calls"] == [
        {
            "name": "code_format",
            "arguments": case["expected_output"]["expected_arguments"]["code_format"],
            "result_ok": True,
            "error_type": None,
            "fault_injection": False,
        }
    ]


def test_submit_contract_tool_can_never_reach_runtime_tool_dispatch(monkeypatch) -> None:
    case = next(case for case in load_cases() if case["example_id"] == "genesis-platform-engineer::happy")
    import tools

    monkeypatch.setattr(
        tools,
        "get_tool",
        lambda _name: pytest.fail("submit extraction contract reached runtime tool dispatch"),
    )

    async def caller(model, messages, max_tokens, tool_schemas):
        return _submit_response(_candidate(case), model, GENESIS_OUTPUT_CONTRACT)

    with pytest.raises(ExperimentContractError, match="unexpected_submit_tool"):
        asyncio.run(execute_case(case, repetition=1, call_model=caller, config=_config()))


def test_expected_tool_failure_is_measured_and_never_retried(monkeypatch) -> None:
    case = next(
        case
        for case in load_tool_cases()
        if case["example_id"] == "genesis-fin-ap::tool::file_write::failure"
    )
    calls = 0

    async def caller(model, messages, max_tokens, tools):
        nonlocal calls
        calls += 1
        if calls == 1:
            message = {
                "content": None,
                "tool_calls": [
                    {
                        "id": "t1",
                        "function": {
                            "name": "file_write",
                            "arguments": json.dumps(case["expected_output"]["expected_arguments"]["file_write"]),
                        },
                    }
                ],
            }
        else:
            payload = _candidate(case) if calls == 2 else _judge_payload()
            contract = GENESIS_OUTPUT_CONTRACT if calls == 2 else JUDGE_RESULTS_CONTRACT
            return _submit_response(payload, model, contract)
        return {"choices": [{"message": message}], "model": model, "usage": {"total_tokens": 1}}

    result = asyncio.run(
        execute_case(
            case,
            repetition=1,
            call_model=caller,
            config=_config(max_contract_retries=1, contract_retry_backoff_seconds=0),
        )
    )
    assert calls == 3
    assert result["retry_count"] == 0
    assert result["actual_tool_calls"][0]["result_ok"] is False
    assert result["actual_tool_calls"][0]["error_type"] == "injected_timeout"


def test_aggregates_and_repeatability_use_measured_rows() -> None:
    rows = [
        {"example_id": "a", "critical_pass": True, "total_latency_ms": 10.0, "total_cost_usd": 0.1, "step_count": 4, "retry_count": 0, "candidate_usage": {"prompt_tokens": 2, "completion_tokens": 3}, "judge_usage": {"prompt_tokens": 5, "completion_tokens": 7}, "output_status": "HOLD", "candidate_model_actual": "m", "candidate_output_sha256": "x", "error": None},
        {"example_id": "a", "critical_pass": False, "total_latency_ms": 30.0, "total_cost_usd": 0.2, "step_count": 6, "retry_count": 1, "candidate_usage": {"prompt_tokens": 2, "completion_tokens": 3}, "judge_usage": {"prompt_tokens": 5, "completion_tokens": 7}, "output_status": "FAILED", "candidate_model_actual": "m2", "candidate_output_sha256": "y", "error": None},
    ]
    summary = aggregate_results(rows)
    assert summary["critical_pass_rate"] == 0.5
    assert summary["p95_latency_ms"] == 30.0
    assert summary["total_tokens"] == 34
    variance = repeatability_summary(rows)["scenarios"][0]
    assert variance["intermittent_critical_failure"] is True
    assert variance["output_digest_unique_count"] == 2


def test_failure_record_never_persists_provider_message() -> None:
    case = load_cases()[0]
    record = failed_record(case, 1, RuntimeError("api_key=must-not-persist"))
    assert record["error"] == "RuntimeError"
    assert "must-not-persist" not in json.dumps(record)


def test_cli_dry_run_loads_all_637_applicability_aware_cases_and_needs_no_credentials(tmp_path) -> None:
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    for name in ("LLM_API_URL", "LLM_API_KEY"):
        env.pop(name, None)
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "run_canonical_experiments.py"),
            "--dry-run",
            "--output-dir",
            str(tmp_path),
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert payload["cases"] == 504
    assert payload["agents"] == 19
    assert payload["runs"] == 19 + (4 * 5) + len(select_tool_matrix_cases(load_tool_cases()))
    config = json.loads((tmp_path / "experiment_config.json").read_text(encoding="utf-8"))
    assert config["candidate_model"] == CANDIDATE_LOGICAL_MODEL
    assert config["judge_model"] == JUDGE_LOGICAL_MODEL
    assert len(config["tool_matrix_case_ids"]) == len(select_tool_matrix_cases(load_tool_cases()))


def test_cli_dry_run_can_isolate_exact_automation_cohort(tmp_path) -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "run_canonical_experiments.py"),
            "--dry-run",
            "--provider",
            "anthropic",
            "--judge-model",
            JUDGE_LOGICAL_MODEL,
            "--target-agent",
            "genesis-automation-architect",
            "--target-category",
            "happy",
            "--repeat-count",
            "1",
            "--output-dir",
            str(tmp_path),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["runs"] == 1
    config = json.loads((tmp_path / "experiment_config.json").read_text(encoding="utf-8"))
    assert config["provider"] == "anthropic"
    assert config["candidate_model"] == CANDIDATE_LOGICAL_MODEL
    assert config["judge_model"] == JUDGE_LOGICAL_MODEL
    assert config["baseline_case_ids"] == []
    assert config["repeatability_case_ids"] == ["genesis-automation-architect::happy"]
    assert config["tool_matrix_case_ids"] == []
    assert config["target_agent"] == "genesis-automation-architect"
    assert config["execution_repeat_count"] == 1
