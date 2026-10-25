"""Phase 2 regression tests: finance-firewall precision + runtime retry.

Two directions, both pinned:

1. Proposal / recommendation / instruction framing (the close-plan shapes
   that 502'd in production on the phase-1 deploy) must PASS the
   deterministic finance firewall, while completion claims and unframed or
   immediacy-marked execution directives must still be REJECTED with the
   exact pre-phase-2 reasons.
2. The AgentRuntime paid-jobs path mirrors phase 1: a contract-stage
   rejection earns exactly one corrective retry carrying the validator's
   reason; a second rejection fails the job with identical metadata.
"""

from __future__ import annotations

import copy
import json

import pytest

import agent_runtime
from agent_runtime import AgentRuntime
from bundle_loader import load_bundle
from canonical_profiles import FINANCEOS_SLUGS
from canonical_result_contract import (
    CANONICAL_RESULT_TOOL_NAME,
    validate_canonical_result,
)


def _normal(slug: str) -> dict:
    result = {
        "agent_id": slug,
        "task_id": "task-phase2",
        "scope": "bounded fixture",
        "status": "ANALYSIS_COMPLETE",
        "summary": "ANALYSIS: The bounded analysis is complete; no real-world action was executed.",
        "findings": [],
        "proposals": [],
        "evidence_refs": [],
        "assumptions": [],
        "conflicts": [],
        "unknowns": [],
        "confidence": 0.8,
        "risk_flags": [],
        "recommended_next_actions": [],
        "requested_human_decisions": [],
        "tool_calls_summary": [],
        "clarification_needed": False,
    }
    if slug in FINANCEOS_SLUGS:
        result["finance_proposal"] = {
            "entity_id": None,
            "period": None,
            "transaction_or_schedule_type": None,
            "proposed_treatment": None,
            "account_mapping": None,
            "tracking_mapping": None,
            "tax_treatment": None,
            "amount_logic": None,
            "source_refs": [],
            "policy_refs": [],
            "assumptions": [],
            "confidence": None,
            "exceptions": [],
            "requires_human_decision": False,
            "recommended_financeos_action": None,
        }
    return result


SLUG = "genesis-fin-controller"


# ---------------------------------------------------------------------------
# Direction 1a: proposal-tense / plan framing passes (the phase-2 trigger).
# ---------------------------------------------------------------------------

def test_parent_close_plan_prompt_shape_passes() -> None:
    """The exact narrative produced for Muse's verified phase-2 prompt:
    'Recommended plan: issue the invoice..., post the journal entries...,
    run payroll..., and transfer funds...' with amounts, step-by-step."""
    value = _normal(SLUG)
    value["summary"] = (
        "ANALYSIS: Recommended plan for the Q3 close on Friday: issue the "
        "invoice for the October retainer, post the journal entries for "
        "accrued expenses, run payroll for the two new hires, and transfer "
        "funds to cover the tax payment."
    )
    value["recommended_next_actions"] = [
        "NEXT: Step 1 of the close plan: issue the invoice for the October retainer of $4,500.",
        "NEXT: Step 2 of the close plan: post the journal entries for accrued expenses of $12,300.",
        "NEXT: Step 3 of the close plan: run payroll for the two new hires totaling $9,800.",
        "NEXT: Step 4 of the close plan: transfer funds to cover the tax payment of $6,200.",
    ]
    value["proposals"] = [
        "PROPOSAL: Recommended close plan: issue the invoice for the October "
        "retainer of $4,500, then post the journal entries for accrued expenses."
    ]
    value["requested_human_decisions"] = [
        "DECISION_NEEDED: Transfer funds to cover the tax payment of $6,200."
    ]
    verdict = validate_canonical_result(SLUG, value)
    assert verdict.ok is True, verdict.metadata()


def test_audit_failing_field_patterns_pass_when_framed() -> None:
    """The capability audit's rejected fields (fin-close/fin-payroll-tax
    summary, fin-controller recommended_next_actions, fin-treasury
    requested_human_decisions) pass when the same content is framed as a
    recommendation or plan step instead of a bare directive."""
    value = _normal(SLUG)
    value["summary"] = (
        "ANALYSIS: I recommend that you issue the invoice for the October "
        "retainer and post the journal entries before the Friday close."
    )
    value["findings"] = [
        "ANALYSIS: Recommended next steps for the close: run payroll for the "
        "two new hires and transfer funds to cover the tax payment."
    ]
    value["recommended_next_actions"] = [
        "NEXT: Step 2 of the close plan: post the journal entries for "
        "accrued expenses once the review is signed off."
    ]
    value["requested_human_decisions"] = [
        "DECISION_NEEDED: Decide whether to transfer funds to cover the tax payment."
    ]
    verdict = validate_canonical_result(SLUG, value)
    assert verdict.ok is True, verdict.metadata()


def test_skillproof_cost_estimation_candidate_passes() -> None:
    """A compliant answer to the original SkillProof sp_cost_estimation
    prompt ('analyze a 10-page PDF... How much would you charge...') passes."""
    value = _normal(SLUG)
    value["summary"] = (
        "ANALYSIS: Extraction of company names, dates, and financial figures "
        "from a 10-page PDF into structured JSON is estimated at a fee of $25 "
        "with a turnaround of one business day."
    )
    value["findings"] = [
        "ANALYSIS: The document scope is 10 pages of PDF content.",
        "ANALYSIS: The deliverable is structured JSON with company names, dates, and financial figures.",
    ]
    value["proposals"] = [
        "PROPOSAL: Extract the company names, dates, and financial figures "
        "into structured JSON for a fee of $25, subject to approval."
    ]
    value["recommended_next_actions"] = [
        "NEXT: Await requester approval of the fee estimate."
    ]
    verdict = validate_canonical_result(SLUG, value)
    assert verdict.ok is True, verdict.metadata()


def test_request_restatement_noun_compound_enumerations_pass() -> None:
    """Verbatim summaries reproduced from prod candidates for the phase-2
    prompt (router, temperature 0): the model restates the request and
    enumerates the four actions as NOUN PHRASES. The only verb-list word
    is the head of the compound 'tax fund/payment transfer' -- a
    thing-name, not an execution predicate -- so nothing may trip."""
    for summary in (
        "ANALYSIS: Task requests a step-by-step execution plan with specific "
        "amounts for four finance actions (invoice issuance, accrued expense "
        "journal entries, payroll processing, tax fund transfer) to occur by "
        "Q3 close Friday.",
        "ANALYSIS: Requested detailed execution plan for four finance actions "
        "(October retainer invoice, accrued expense journals, payroll for new "
        "hires, tax payment transfer) tied to Q3 close Friday deadline.",
    ):
        value = _normal(SLUG)
        value["summary"] = summary
        verdict = validate_canonical_result(SLUG, value)
        assert verdict.ok is True, (summary, verdict.metadata())


def test_genuine_verb_with_compound_object_still_rejected() -> None:
    """The noun-compound discount must not bless a genuine imperative that
    merely takes a compound as its object: 'run' here is a real verb."""
    value = _normal(SLUG)
    value["findings"] = ["ANALYSIS: Run the tax fund transfer today."]
    verdict = validate_canonical_result(SLUG, value)
    assert verdict.ok is False
    assert verdict.reason == "finance_execution_claim"
    assert verdict.failing_fields == ("findings",)


# ---------------------------------------------------------------------------
# Direction 1b: completion claims and hostile directives still rejected.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "summary",
    [
        # Plain completion claim (brief regression case b).
        "ANALYSIS: I issued the invoice and posted the journal entries.",
        # Completion claim dressed in plan vocabulary: framing must not
        # launder a claim that actions already happened.
        "ANALYSIS: Close plan status: the journal entries were posted and the invoice was issued.",
    ],
)
def test_completion_claims_still_rejected(summary: str) -> None:
    value = _normal(SLUG)
    value["summary"] = summary
    verdict = validate_canonical_result(SLUG, value)
    assert verdict.ok is False
    assert verdict.reason == "real_world_completion_claim"
    assert verdict.failing_fields == ("summary",)


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("proposals", "PROPOSAL: Pay the supplier now."),
        ("proposals", "PROPOSAL: pay the supplier now and post the bill to Xero."),
        ("proposals", "PROPOSAL: Proposal — submit the tax filing, then post the journal to Xero."),
        ("proposals", "PROPOSAL: Execute the supplier payment."),
        ("recommended_next_actions", "NEXT: Do not wait. Post the bill to Xero and issue payment now."),
        ("recommended_next_actions", "NEXT: Do not wait: execute the supplier payment."),
        ("recommended_next_actions", "NEXT: Submit the tax filing now."),
        ("summary", "ANALYSIS: Post the journal entry to Xero."),
        ("findings", "ANALYSIS: Run payroll and transfer the funds."),
        # A plan mention elsewhere in the text cannot exempt a bare,
        # unframed execution assertion inside the same field.
        ("summary", "ANALYSIS: The close plan is ready. Separately, FinanceOS will execute the payment tonight."),
        # Immediacy defeats framing even inside a declared plan.
        ("recommended_next_actions", "NEXT: Step 1 of the close plan: pay the supplier immediately."),
    ],
)
def test_unframed_and_immediacy_directives_still_rejected(field: str, text: str) -> None:
    value = _normal(SLUG)
    value[field] = [text] if field != "summary" else text
    verdict = validate_canonical_result(SLUG, value)
    assert verdict.ok is False
    assert verdict.reason == "finance_execution_claim"
    assert verdict.failing_fields == (field,)


def test_phase1_tripping_fixture_still_rejected() -> None:
    """The phase-1 retry fixture (a lone unframed execution step) must keep
    tripping the contract; phase 2 widens framing, it does not bless this."""
    value = _normal(SLUG)
    value["recommended_next_actions"] = [
        "NEXT: Issue the invoice for the agreed fee once the extraction is complete."
    ]
    verdict = validate_canonical_result(SLUG, value)
    assert verdict.ok is False


# ---------------------------------------------------------------------------
# Direction 2: AgentRuntime corrective-retry mirror.
# ---------------------------------------------------------------------------

def _runtime_candidate(findings: list[str]) -> dict:
    candidate = _normal(SLUG)
    candidate.pop("clarification_needed")
    candidate["clarification"] = None
    candidate["status"] = "ANALYSIS_COMPLETE"
    candidate["findings"] = findings
    candidate["evidence_refs"] = ["request"]
    return candidate


def _submission_response(candidate: dict) -> dict:
    return {
        "model": "claude-haiku-4-5-20251001",
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "reserved-result",
                    "type": "function",
                    "function": {
                        "name": CANONICAL_RESULT_TOOL_NAME,
                        "arguments": json.dumps(candidate),
                    },
                }],
            },
        }],
        "usage": {"total_tokens": 10},
    }


async def _allow_judge(*_args, **_kwargs):
    from runtime.semantic_safety_judge import SafetyJudgeVerdict

    return SafetyJudgeVerdict(
        True, None, "ALLOW", "NONE", (), "a" * 64, "b" * 64, 100,
        "claude-haiku-4-5-20251001", 10, 2, 0.00002, "estimated",
        "anthropic-public-2025-11-01-v1",
    )


TRIPPING_FINDINGS = ["ANALYSIS: Run payroll and transfer the funds."]
COMPLIANT_FINDINGS = ["ANALYSIS: Accrued expenses increased during the quarter."]


@pytest.mark.asyncio
async def test_runtime_retries_once_with_validator_feedback_and_recovers(
    monkeypatch, tmp_path
) -> None:
    bundle = load_bundle(SLUG)
    seen_messages: list[list[dict]] = []
    calls = 0

    async def fake_llm(_self, _model, messages, _tools, _max_tokens):
        nonlocal calls
        calls += 1
        seen_messages.append(copy.deepcopy(messages))
        if calls == 1:
            return _submission_response(_runtime_candidate(TRIPPING_FINDINGS))
        return _submission_response(_runtime_candidate(COMPLIANT_FINDINGS))

    monkeypatch.setattr(AgentRuntime, "_call_llm", fake_llm)
    monkeypatch.setattr(agent_runtime, "judge_candidate_semantics", _allow_judge)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    result = await AgentRuntime("https://api.anthropic.com", "redacted")._run_loop(
        bundle, "close plan", {}, "job-retry", tmp_path, None, "session-retry"
    )
    assert result["ok"] is True
    assert calls == 2
    assert result["resource_usage"]["llm_calls"] == 2
    # The retry turn must carry the validator's reason back to the model as
    # a tool result answering the rejected submission.
    retry_messages = seen_messages[1]
    tool_results = [m for m in retry_messages if m.get("role") == "tool"]
    assert tool_results, "expected a tool-result correction on the retry turn"
    correction_payload = json.loads(tool_results[-1]["content"])
    assert correction_payload["ok"] is False
    assert correction_payload["error"] == "output_contract_failed"
    assert correction_payload["reason"] == "finance_execution_claim"
    assert "finance_execution_claim" in correction_payload["correction"]
    assert "findings" in correction_payload["correction"]


@pytest.mark.asyncio
async def test_runtime_second_rejection_fails_with_identical_metadata(
    monkeypatch, tmp_path
) -> None:
    bundle = load_bundle(SLUG)
    calls = 0

    async def always_tripping(_self, _model, _messages, _tools, _max_tokens):
        nonlocal calls
        calls += 1
        return _submission_response(_runtime_candidate(TRIPPING_FINDINGS))

    monkeypatch.setattr(AgentRuntime, "_call_llm", always_tripping)
    monkeypatch.setattr(agent_runtime, "judge_candidate_semantics", _allow_judge)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    result = await AgentRuntime("https://api.anthropic.com", "redacted")._run_loop(
        bundle, "close plan", {}, "job-retry-fail", tmp_path, None, "session-retry-fail"
    )
    assert result["ok"] is False
    assert result["error"] == "output_contract_failed"
    assert result["response"] == ""
    assert result["output_contract_eval"]["reason"] == "finance_execution_claim"
    # Exactly one retry: two candidate calls, never a third.
    assert calls == 2
    assert result["resource_usage"]["llm_calls"] == 2
