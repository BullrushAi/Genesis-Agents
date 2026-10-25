"""Phase 6 pins: the retry-correction hint for the clarification
representation rejection, and byte-exact preservation of every pre-existing
hint mapping.

Root cause (phase 5, instrumented captures): failing candidates emit a
16-key short-form result object that omits the `clarification` key entirely
(and `finance_proposal` with it), which the submission stage projects as
provider_clarification_representation_invalid. The retry-correction hint
maps in main.py and agent_runtime.py had NO entry for that reason, so the
single corrective retry fired with the generic fallback hint -- which never
names the omission -- and both attempts failed together.

The fix adds one per-reason hint entry to both mirrored maps. The validator
(canonical_result_contract.py) is untouched.

Phase 7 update: every correction now also ends with a standing
clarification-representation footer, because a retry triggered by an
unrelated deterministic reason can regenerate a candidate that newly omits
the clarification key (phase-6 runs 6/10). The byte-exact pins below
therefore assert pristine body + exactly one footer. The phase-6 pin that a
finance correction contains no clarification text is replaced by design:
the finance correction now carries the standing footer, but never the
clarification-specific hint body.
"""
from __future__ import annotations

import json

import pytest

import agent_runtime
import main
from canonical_result_contract import CANONICAL_RESULT_TOOL_NAME
from runtime.request_auth import Principal
from runtime.semantic_safety_judge import (
    CANDIDATE_MODEL,
    JUDGE_MODEL,
    SafetyJudgeVerdict,
    project_candidate_narrative,
)

REASON = "provider_clarification_representation_invalid"

# The phase-7 standing footer, rendered exactly as appended to every
# correction produced by main.py and agent_runtime._contract_correction.
FOOTER = (
    "Standing requirement for every resubmission: include the key "
    "clarification exactly once with value null and every other required "
    "field; never emit clarification_needed, candidate_resolution, or any "
    "other flat clarification field."
)

# Byte-exact correction strings produced by the PRE-CHANGE
# agent_runtime._contract_correction (captured from pristine main @ dacd4dad
# before the phase-6 hint was added). Any drift in an existing mapping --
# hint body, wrapper, or field rendering -- fails these pins.
EXPECTED_PRISTINE_CORRECTIONS = {'finance_execution_claim': "CORRECTION: your previous result submission was rejected by the deterministic Genesis V5 contract validator (reason: finance_execution_claim; field: a narrative field). The validator rejects narrative that asserts or directs finance execution (a finance action verb -- pay, post, issue, execute, run, file, submit, transfer, move, refund, approve -- joined to a finance object -- payment, invoice, journal, bill, funds, refund, payroll, tax filing, Xero) when it is not framed as a proposal, recommendation, or plan step, and it rejects every immediacy directive ('now', 'immediately', 'do not wait'). Frame each action step explicitly as a recommendation or as a step of a stated plan with no immediacy wording, or use nouns only with no action verb when an item must mention a finance object (safe pattern: 'UNKNOWN: Invoice and payment terms absent from the request.'). Never state that a finance action occurred. Submit exactly one complete, compliant V5 result tool call.", 'real_world_completion_claim': 'CORRECTION: your previous result submission was rejected by the deterministic Genesis V5 contract validator (reason: real_world_completion_claim; field: a narrative field). Do not state or imply that any posting, payment, filing, approval, verification, or other external action occurred or was completed. Submit exactly one complete, compliant V5 result tool call.', 'private_durable_memory': 'CORRECTION: your previous result submission was rejected by the deterministic Genesis V5 contract validator (reason: private_durable_memory; field: a narrative field). Never propose storing or retaining anything in private or durable memory, ledgers, or databases. Submit exactly one complete, compliant V5 result tool call.', 'direct_human_contact': "CORRECTION: your previous result submission was rejected by the deterministic Genesis V5 contract validator (reason: direct_human_contact; field: a narrative field). Never instruct anyone to contact, message, email, or notify a person directly; route clarification through Cato's Clarification Engine. Submit exactly one complete, compliant V5 result tool call.", 'unsupported_human_question': 'CORRECTION: your previous result submission was rejected by the deterministic Genesis V5 contract validator (reason: unsupported_human_question; field: a narrative field). State unknowns as concise declarative missing-input facts, not as broad questions directed at a human. Submit exactly one complete, compliant V5 result tool call.', 'some_unknown_reason': 'CORRECTION: your previous result submission was rejected by the deterministic Genesis V5 contract validator (reason: some_unknown_reason; field: a narrative field). Resubmit a fully compliant Genesis V5 result: prefix every narrative string and array item with its exact intent prefix, keep every required field, and keep all narrative analysis-only. Submit exactly one complete, compliant V5 result tool call.', '__with_field__': 'CORRECTION: your previous result submission was rejected by the deterministic Genesis V5 contract validator (reason: real_world_completion_claim; field: summary). Do not state or imply that any posting, payment, filing, approval, verification, or other external action occurred or was completed. Submit exactly one complete, compliant V5 result tool call.'}

SLUG = "genesis-fin-controller"
TRIPPING_ACTION = (
    "NEXT: Issue the invoice for the agreed fee once the extraction is complete."
)
COMPLIANT_ACTION = (
    "NEXT: Return the request to Cato for routing to a document-processing service."
)


def test_agent_runtime_clarification_hint_names_defect_and_remedy():
    text = agent_runtime._contract_correction(REASON, [])
    assert f"(reason: {REASON}; field: a narrative field)" in text
    # The hint must name the defect (omitted keys, clarification key) ...
    assert "omitted required keys" in text
    assert "clarification key" in text
    # ... and the remedy (complete object, clarification exactly once = null,
    # no flat clarification fields).
    assert "every required field present" in text
    assert "clarification exactly once with value null" in text
    assert "clarification_needed" in text
    assert "Submit exactly one complete, compliant V5 result tool call." in text
    # It must NOT be the generic fallback.
    assert "prefix every narrative string" not in text
    # Phase 7: the standing footer is appended exactly once, at the end.
    assert text.count(FOOTER) == 1
    assert text.endswith(FOOTER)
    assert agent_runtime._CLARIFICATION_REPRESENTATION_FOOTER == FOOTER


def test_agent_runtime_existing_hints_unchanged():
    # Phase 7: every pre-existing hint body stays byte-identical to the
    # pristine correction; the ONLY change is the standing footer appended
    # after the final sentence, exactly once.
    for reason, expected_text in EXPECTED_PRISTINE_CORRECTIONS.items():
        if reason == "__with_field__":
            continue
        produced = agent_runtime._contract_correction(reason, [])
        assert produced == expected_text + " " + FOOTER
        assert produced.count(FOOTER) == 1
    produced = agent_runtime._contract_correction(
        "real_world_completion_claim", ["summary"]
    )
    assert produced == EXPECTED_PRISTINE_CORRECTIONS["__with_field__"] + " " + FOOTER
    assert produced.count(FOOTER) == 1


def _candidate(next_action: str) -> dict:
    return {
        "agent_id": SLUG, "task_id": "task-clarification-retry",
        "scope": "tenant:scope", "status": "ANALYSIS_COMPLETE",
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


def _provider_value(candidate: dict, *, short_form: bool = False) -> dict:
    provider = json.loads(json.dumps(candidate))
    provider.pop("clarification_needed")
    for field in (
        "candidate_resolution", "reason_human_needed",
        "recommended_human_resolver", "materiality", "urgency",
        "resolution_attempts", "supporting_evidence_refs",
        "competing_state_refs", "recommended_answer_options",
    ):
        provider.pop(field, None)
    if short_form:
        # The phase-5 failure shape: a 16-key short-form object that omits
        # the clarification key entirely, and finance_proposal with it.
        provider.pop("finance_proposal", None)
        assert "clarification" not in provider
        assert len(provider) == 16
        return provider
    provider["clarification"] = None
    return provider


def _forced_response(candidate: dict, *, short_form: bool = False) -> dict:
    return {
        "model": CANDIDATE_MODEL,
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {"content": None, "tool_calls": [{
                "id": "result", "type": "function",
                "function": {
                    "name": CANONICAL_RESULT_TOOL_NAME,
                    "arguments": json.dumps(
                        _provider_value(candidate, short_form=short_form)
                    ),
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


def _retry_correction(calls) -> str:
    """The correction is the extra system message appended on the retry."""
    first_systems = [
        str(m.get("content") or "")
        for m in calls[0]["messages"] if m.get("role") == "system"
    ]
    retry_systems = [
        str(m.get("content") or "")
        for m in calls[1]["messages"] if m.get("role") == "system"
    ]
    assert len(retry_systems) == len(first_systems) + 1
    return retry_systems[-1]


@pytest.mark.asyncio
async def test_main_short_form_candidate_retries_with_clarification_hint(monkeypatch):
    calls: list[dict] = []

    async def candidate_call(*_args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _forced_response(_candidate(COMPLIANT_ACTION), short_form=True)
        return _forced_response(_candidate(COMPLIANT_ACTION))

    _wire(monkeypatch, candidate_call)
    response = await main.run_agent(
        SLUG,
        main.RunRequest(prompt="Review the proposed plan.", mode="live_test"),
        _principal(),
    )
    assert json.loads(response.response)["ok"] is True
    assert len(calls) == 2
    correction = _retry_correction(calls)
    assert f"(reason: {REASON};" in correction
    assert "omitted required keys" in correction
    assert "clarification exactly once with value null" in correction
    # Phase 7: the standing footer rides on the main.py path too.
    assert correction.count(FOOTER) == 1
    assert correction.endswith(FOOTER)


@pytest.mark.asyncio
async def test_main_finance_correction_carries_footer_not_clarification_hint(monkeypatch):
    calls: list[dict] = []

    async def candidate_call(*_args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _forced_response(_candidate(TRIPPING_ACTION))
        return _forced_response(_candidate(COMPLIANT_ACTION))

    _wire(monkeypatch, candidate_call)
    response = await main.run_agent(
        SLUG,
        main.RunRequest(prompt="Review the proposed plan.", mode="live_test"),
        _principal(),
    )
    assert json.loads(response.response)["ok"] is True
    assert len(calls) == 2
    correction = _retry_correction(calls)
    assert "(reason: finance_execution_claim;" in correction
    # Phase-6 pin intent, preserved: the clarification-specific hint body
    # must NOT leak into a finance correction ...
    assert "omitted required keys" not in correction
    assert "invalid clarification representation" not in correction
    # ... but the phase-7 standing footer IS present exactly once, at the
    # end -- this is the runs-6/10 fix: a finance-triggered retry now
    # restates the clarification requirement.
    assert correction.count(FOOTER) == 1
    assert correction.endswith(FOOTER)
