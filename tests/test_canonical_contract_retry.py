"""Regression: a single stochastic candidate that trips a fail-closed
contract stage on phrasing alone must not surface as an unlogged HTTP 502.

Root cause (Oct 21, 2026): on the canonical live-test/persona path, any
contract-stage rejection (submission structure, semantic judge, or the
deterministic narrative firewall) raised a bare HTTP 502 with no retry and
no log line. A Finance Controller answer whose recommended_next_actions
joined a finance action verb to a finance noun -- ordinary proposal-tense
phrasing for that persona -- failed with reason finance_execution_claim,
and SkillProof scored the run 0.

The fix retries the pipeline exactly once with the validator's feedback
appended as a system instruction. The gate is unchanged: the retry must
pass submission, judge, deterministic, and immutability checks, and a
second rejection returns the identical 502 detail.
"""
from __future__ import annotations

import json

import pytest

import main
from canonical_result_contract import CANONICAL_RESULT_TOOL_NAME
from runtime.request_auth import Principal
from runtime.semantic_safety_judge import (
    CANDIDATE_MODEL,
    JUDGE_MODEL,
    SafetyJudgeVerdict,
    project_candidate_narrative,
)

SLUG = "genesis-fin-controller"
TRIPPING_ACTION = (
    "NEXT: Issue the invoice for the agreed fee once the extraction is complete."
)
COMPLIANT_ACTION = (
    "NEXT: Return the request to Cato for routing to a document-processing service."
)


def _candidate(next_action: str) -> dict:
    value = {
        "agent_id": SLUG, "task_id": "task-retry", "scope": "tenant:scope",
        "status": "ANALYSIS_COMPLETE",
        "summary": "ANALYSIS: Bounded analysis is complete.",
        "findings": [], "proposals": [], "evidence_refs": [], "assumptions": [],
        "conflicts": [], "unknowns": [], "confidence": 0.9, "risk_flags": [],
        "recommended_next_actions": [next_action],
        "requested_human_decisions": [], "tool_calls_summary": [],
        "clarification_needed": False,
        "candidate_resolution": "", "reason_human_needed": "",
        "recommended_human_resolver": "", "materiality": None, "urgency": "",
        "resolution_attempts": [], "supporting_evidence_refs": [],
        "competing_state_refs": [], "recommended_answer_options": [],
        "finance_proposal": {
            "entity_id": None, "period": None,
            "transaction_or_schedule_type": None,
            "proposed_treatment": "PROPOSAL: Draft treatment for review.",
            "account_mapping": None, "tracking_mapping": None,
            "tax_treatment": None, "amount_logic": None,
            "source_refs": [], "policy_refs": [], "assumptions": [],
            "confidence": 0.8, "exceptions": [],
            "requires_human_decision": False,
            "recommended_financeos_action": "NEXT: Review the proposal.",
        },
    }
    return value


def _provider_value(candidate: dict) -> dict:
    provider = json.loads(json.dumps(candidate))
    provider.pop("clarification_needed")
    for field in (
        "candidate_resolution", "reason_human_needed",
        "recommended_human_resolver", "materiality", "urgency",
        "resolution_attempts", "supporting_evidence_refs",
        "competing_state_refs", "recommended_answer_options",
    ):
        provider.pop(field, None)
    provider["clarification"] = None
    return provider


def _forced_response(candidate: dict) -> dict:
    return {
        "model": CANDIDATE_MODEL,
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {"content": None, "tool_calls": [{
                "id": "result", "type": "function",
                "function": {
                    "name": CANONICAL_RESULT_TOOL_NAME,
                    "arguments": json.dumps(_provider_value(candidate)),
                },
            }]},
        }],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def _principal() -> Principal:
    return Principal(
        "test", "tenant", "client", frozenset({"agent:run"}), "test", 9999999999,
    )


def _wire(monkeypatch, candidate_call) -> None:
    async def allow(value, **_kwargs):
        projection = project_candidate_narrative(value)
        return SafetyJudgeVerdict(
            True, None, "ALLOW", "NONE", (), projection.candidate_sha256,
            projection.projection_sha256, projection.projection_bytes,
            JUDGE_MODEL, 10, 2, 0.00002, "estimated", "test",
        )

    monkeypatch.setenv("GENESIS_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_URL", "https://api.anthropic.com/v1/messages")
    monkeypatch.setenv("LLM_API_KEY", "redacted-test-key")
    monkeypatch.setattr(main, "call_exact_anthropic_candidate", candidate_call)
    monkeypatch.setattr(main, "judge_candidate_semantics", allow)


@pytest.mark.asyncio
async def test_contract_rejection_retries_once_with_feedback_and_succeeds(monkeypatch):
    calls: list[dict] = []

    async def candidate_call(*_args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _forced_response(_candidate(TRIPPING_ACTION))
        return _forced_response(_candidate(COMPLIANT_ACTION))

    _wire(monkeypatch, candidate_call)
    response = await main.run_agent(
        SLUG,
        main.RunRequest(prompt="How much would you charge for this task?", mode="live_test"),
        _principal(),
    )
    envelope = json.loads(response.response)
    result = json.loads(envelope["response"])
    assert result["recommended_next_actions"] == [COMPLIANT_ACTION]
    assert len(calls) == 2
    retry_messages = calls[1]["messages"]
    correction = " ".join(
        str(m.get("content") or "") for m in retry_messages if m.get("role") == "system"
    )
    assert "finance_execution_claim" in correction
    assert "recommended_next_actions" in correction


@pytest.mark.asyncio
async def test_second_rejection_returns_identical_502(monkeypatch):
    calls: list[dict] = []

    async def candidate_call(*_args, **kwargs):
        calls.append(kwargs)
        return _forced_response(_candidate(TRIPPING_ACTION))

    _wire(monkeypatch, candidate_call)
    with pytest.raises(main.HTTPException) as raised:
        await main.run_agent(
            SLUG,
            main.RunRequest(prompt="How much would you charge for this task?", mode="live_test"),
            _principal(),
        )
    assert raised.value.status_code == 502
    assert raised.value.detail["error"] == "output_contract_failed"
    assert raised.value.detail["reason"] == "finance_execution_claim"
    assert raised.value.detail["failing_fields"] == ["recommended_next_actions"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_compliant_first_candidate_does_not_retry(monkeypatch):
    calls: list[dict] = []

    async def candidate_call(*_args, **kwargs):
        calls.append(kwargs)
        return _forced_response(_candidate(COMPLIANT_ACTION))

    _wire(monkeypatch, candidate_call)
    response = await main.run_agent(
        SLUG,
        main.RunRequest(prompt="How much would you charge for this task?", mode="live_test"),
        _principal(),
    )
    assert json.loads(response.response)["ok"] is True
    assert len(calls) == 1
