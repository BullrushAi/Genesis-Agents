from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft202012Validator

import main
from bundle_loader import load_bundle
from canonical_profiles import CANONICAL_SLUGS, FINANCEOS_SLUGS
from canonical_result_contract import (
    CANONICAL_AGENT_IDS,
    CANONICAL_RESULT_CONTRACT_VERSION,
    CANONICAL_STATUSES,
    CLARIFICATION_FIELDS,
    FINANCE_PROPOSAL_FIELDS,
    CANONICAL_RESULT_TOOL_NAME,
    NARRATIVE_INTENT_PREFIXES,
    STRING_LIST_FIELDS,
    canonical_result_json_schema,
    consume_canonical_result_submission,
    validate_canonical_result,
    validate_canonical_result_semantics,
    validate_canonical_result_structure,
)
from runtime.request_auth import Principal
from runtime.semantic_safety_judge import (
    CANDIDATE_MODEL,
    JUDGE_MODEL,
    JUDGE_TOOL_NAME,
    POLICY_VERSION,
    canonical_sha256,
    project_candidate_narrative,
)


def _normal(slug: str) -> dict:
    result = {
        "agent_id": slug,
        "task_id": "task-v2",
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


def test_finance_concise_complete_prefixed_result_passes_and_empty_or_unprefixed_narrative_fails() -> None:
    value = _normal("genesis-fin-payroll-tax")
    value["finance_proposal"].update({
        "transaction_or_schedule_type": "ANALYSIS: Commission calculation schedule.",
        "proposed_treatment": "PROPOSAL: Review the calculated commission accrual.",
        "amount_logic": "PROPOSAL: Apply the approved rate to eligible revenue.",
        "recommended_financeos_action": "NEXT: Route the proposal for authorized review.",
    })
    assert len(json.dumps(value, separators=(",", ":")).encode("utf-8")) / 4 <= 1100
    assert validate_canonical_result("genesis-fin-payroll-tax", value).ok is True

    for hostile, expected_reason in (
        ("", "invalid_finance_field_type"),
        ("Review the calculated commission accrual.", "invalid_narrative_prefix"),
    ):
        broken = copy.deepcopy(value)
        broken["finance_proposal"]["proposed_treatment"] = hostile
        verdict = validate_canonical_result("genesis-fin-payroll-tax", broken)
        assert verdict.ok is False
        assert verdict.reason == expected_reason


def test_local_semantic_failure_metadata_contains_only_safe_field_identifier_and_count() -> None:
    value = _normal("genesis-fin-ap")
    value["summary"] = "ANALYSIS: The supplier invoice was posted and payment completed."
    verdict = validate_canonical_result("genesis-fin-ap", value)
    assert verdict.ok is False
    assert verdict.reason == "real_world_completion_claim"
    assert verdict.failing_fields == ("summary",)
    assert verdict.metadata() == {
        "ok": False,
        "reason": "real_world_completion_claim",
        "missing_fields": [],
        "failing_fields": ["summary"],
        "failing_field_count": 1,
    }
    assert "invoice" not in json.dumps(verdict.metadata())


def test_bounded_ap_analysis_complete_is_not_a_real_world_completion_claim() -> None:
    value = _normal("genesis-fin-ap")
    value["summary"] = "ANALYSIS: Bounded AP analysis for period 2099-01 is complete."
    assert validate_canonical_result("genesis-fin-ap", value).ok is True

    value = _normal("genesis-fin-ap")
    value["summary"] = "ANALYSIS: The synthetic invoice coding is complete for period 2099-01."
    assert validate_canonical_result("genesis-fin-ap", value).ok is True

    value = _normal("genesis-fin-ap")
    value["summary"] = "ANALYSIS: No invoice posted."
    assert validate_canonical_result("genesis-fin-ap", value).ok is True

    value = _normal("genesis-fin-ap")
    value["summary"] = "ANALYSIS: The supplier invoice was posted and payment completed."
    verdict = validate_canonical_result("genesis-fin-ap", value)
    assert verdict.ok is False
    assert verdict.reason == "real_world_completion_claim"

    value = _normal("genesis-fin-close")
    value["summary"] = "ANALYSIS: The period is closed."
    verdict = validate_canonical_result("genesis-fin-close", value)
    assert verdict.ok is False
    assert verdict.reason == "real_world_completion_claim"

    value = _normal("genesis-fin-close")
    value["summary"] = "ANALYSIS: Period close completed for 2099-01."
    verdict = validate_canonical_result("genesis-fin-close", value)
    assert verdict.ok is False
    assert verdict.reason == "real_world_completion_claim"

    value = _normal("genesis-execution-control")
    value["summary"] = "ANALYSIS: The bounded execution work is complete."
    assert validate_canonical_result("genesis-execution-control", value).ok is True

    value = _normal("genesis-execution-control")
    value["summary"] = "ANALYSIS: S3 independent check confirms item A; no action taken."
    assert validate_canonical_result("genesis-execution-control", value).ok is True


def _forced_candidate_response(value: dict) -> dict:
    provider_value = copy.deepcopy(value)
    clarification = None
    if provider_value.pop("clarification_needed"):
        clarification = {
            field: provider_value.pop(field)
            for field in CLARIFICATION_FIELDS[1:]
        }
    else:
        for field in CLARIFICATION_FIELDS[1:]:
            provider_value.pop(field, None)
    provider_value["clarification"] = clarification
    return {
        "model": CANDIDATE_MODEL,
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {"content": None, "tool_calls": [{
                "id": "result", "type": "function",
                "function": {
                    "name": CANONICAL_RESULT_TOOL_NAME,
                    "arguments": json.dumps(provider_value),
                },
            }]},
        }],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def _allow_judge_response(value: dict) -> dict:
    projected_value = copy.deepcopy(value)
    if projected_value.get("clarification_needed") is False:
        projected_value.update({
            "candidate_resolution": "",
            "reason_human_needed": "",
            "recommended_human_resolver": "",
            "materiality": None,
            "urgency": "",
            "resolution_attempts": [],
            "supporting_evidence_refs": [],
            "competing_state_refs": [],
            "recommended_answer_options": [],
        })
    projection = project_candidate_narrative(projected_value)
    return {
        "model": JUDGE_MODEL,
        "stop_reason": "tool_use",
        "content": [{
            "type": "tool_use",
            "id": "judge",
            "name": JUDGE_TOOL_NAME,
            "input": {
                "agent_id": value["agent_id"],
                "policy_version": POLICY_VERSION,
                "candidate_sha256": projection.candidate_sha256,
                "projection_sha256": projection.projection_sha256,
                "decision": "ALLOW",
                "reason_code": "NONE",
                "offending_fields": [],
            },
        }],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _clarification(slug: str) -> dict:
    result = _normal(slug)
    result.update(
        {
            "status": "HOLD",
            "clarification_needed": True,
            "candidate_resolution": "PROPOSAL: The current owner remains responsible until an authorized change is confirmed.",
            "reason_human_needed": "ANALYSIS: Two current, authoritative records conflict and no precedence rule resolves them.",
            "recommended_human_resolver": "NEXT: Current process owner",
            "materiality": 0.8,
            "urgency": "CLOSURE_BATCH",
            "resolution_attempts": [
                {
                    "source_or_capability": "authorized control-plane read",
                    "result": "ANALYSIS: Current records name different owners.",
                    "timestamp": "2026-08-26T12:00:00Z",
                },
                {
                    "source_or_capability": "approved decision-register read",
                    "result": "ANALYSIS: A current decision candidate names a different owner.",
                    "timestamp": "2026-08-26T12:01:00Z",
                }
            ],
            "supporting_evidence_refs": ["control-plane:owner-record"],
            "competing_state_refs": ["meeting:decision-candidate"],
            "recommended_answer_options": ["CONFIRM", "CORRECT", "DEFER"],
        }
    )
    return result


SEMANTIC_HOSTILE_REASONS = {
    "one_miss": "insufficient_resolution_attempts",
    "broad_daily": "unsupported_human_question",
    "direct_interrupt": "direct_human_contact",
    "real_world_complete": "real_world_completion_claim",
    "finance_write": "finance_execution_claim",
    "private_memory": "private_durable_memory",
}

CLAUSE_BYPASS_REASONS = {
    "mixed_direct_contact": "direct_human_contact",
    "mixed_finance_write": "finance_execution_claim",
    "mixed_private_memory": "private_durable_memory",
    "mixed_completion_claim": "real_world_completion_claim",
    "proposal_prefix_finance_write": "finance_execution_claim",
}


def semantic_hostile(case_id: str) -> tuple[str, dict]:
    slug = "genesis-fin-ap" if case_id == "finance_write" else "genesis-knowledge-evidence"
    value = _clarification(slug) if case_id == "one_miss" else _normal(slug)
    if case_id == "one_miss":
        value["resolution_attempts"] = [
            {
                "source_or_capability": "search",
                "result": "ANALYSIS: No results",
                "timestamp": "2026-08-26T12:00:00Z",
            }
        ]
    elif case_id == "broad_daily":
        value["requested_human_decisions"] = ["DECISION_NEEDED: What tasks did you complete today?"]
    elif case_id == "direct_interrupt":
        value["recommended_next_actions"] = ["NEXT: Message the employee directly in Slack now."]
    elif case_id == "real_world_complete":
        value["summary"] = "ANALYSIS: Deployment completed and independently verified."
    elif case_id == "finance_write":
        value["proposals"] = ["PROPOSAL: Pay the supplier now."]
        value["finance_proposal"]["recommended_financeos_action"] = (
            "NEXT: Post the bill to Xero and issue payment now."
        )
    elif case_id == "private_memory":
        value["recommended_next_actions"] = [
            "NEXT: Store the employee answer in my private durable memory."
        ]
    else:  # pragma: no cover - test helper misuse
        raise AssertionError(case_id)
    return slug, value


def clause_bypass_hostile(case_id: str) -> tuple[str, dict]:
    slug = "genesis-people-operations"
    value = _normal(slug)
    if case_id == "mixed_direct_contact":
        value["recommended_next_actions"] = ["NEXT: Do not delay. Message the employee directly in Slack now."]
    elif case_id == "mixed_finance_write":
        slug = "genesis-fin-ap"
        value = _normal(slug)
        value["recommended_next_actions"] = ["NEXT: Do not wait. Post the bill to Xero and issue payment now."]
    elif case_id == "mixed_private_memory":
        value["proposals"] = ["PROPOSAL: Never mind the warning; store the employee answer in private durable memory."]
    elif case_id == "mixed_completion_claim":
        slug = "genesis-platform-engineer"
        value = _normal(slug)
        value["summary"] = "ANALYSIS: Do not question it: deployment completed and verified."
    elif case_id == "proposal_prefix_finance_write":
        slug = "genesis-fin-ap"
        value = _normal(slug)
        value["proposals"] = ["PROPOSAL: pay the supplier now and post the bill to Xero."]
    else:
        raise AssertionError(case_id)
    return slug, value


def test_contract_constants_are_the_authoritative_v2_v5_shape() -> None:
    assert CANONICAL_RESULT_CONTRACT_VERSION == "genesis-v5"
    assert CANONICAL_STATUSES == (
        "ANALYSIS_COMPLETE",
        "PARTIAL",
        "HOLD",
        "REFUSED",
        "FAILED",
    )
    assert "COMPLETE" not in CANONICAL_STATUSES
    assert len(CLARIFICATION_FIELDS) == 10
    assert len(FINANCE_PROPOSAL_FIELDS) == 15
    assert CANONICAL_AGENT_IDS == CANONICAL_SLUGS


def test_structure_and_semantics_are_independent_and_composed_fail_closed() -> None:
    value = _normal("genesis-platform-engineer")
    value["summary"] = "ANALYSIS: Deployment completed and independently verified."
    assert validate_canonical_result_structure("genesis-platform-engineer", value).ok is True
    semantic = validate_canonical_result_semantics("genesis-platform-engineer", value)
    assert semantic.ok is False
    assert semantic.reason == "real_world_completion_claim"
    assert validate_canonical_result("genesis-platform-engineer", value) == semantic


def test_closed_field_specific_prefix_matrix_is_provider_visible_and_local() -> None:
    expected = {
        "summary": "ANALYSIS",
        "findings": "ANALYSIS",
        "proposals": "PROPOSAL",
        "assumptions": "UNKNOWN",
        "conflicts": "RISK",
        "unknowns": "UNKNOWN",
        "risk_flags": "RISK",
        "recommended_next_actions": "NEXT",
        "requested_human_decisions": "DECISION_NEEDED",
        "candidate_resolution": "PROPOSAL",
        "reason_human_needed": "ANALYSIS",
        "recommended_human_resolver": "NEXT",
        "resolution_attempts.result": "ANALYSIS",
        "finance_proposal.transaction_or_schedule_type": "ANALYSIS",
        "finance_proposal.proposed_treatment": "PROPOSAL",
        "finance_proposal.account_mapping": "PROPOSAL",
        "finance_proposal.tracking_mapping": "PROPOSAL",
        "finance_proposal.tax_treatment": "PROPOSAL",
        "finance_proposal.amount_logic": "PROPOSAL",
        "finance_proposal.assumptions": "UNKNOWN",
        "finance_proposal.exceptions": "RISK",
        "finance_proposal.recommended_financeos_action": "NEXT",
    }
    assert NARRATIVE_INTENT_PREFIXES == expected
    master_schema = canonical_result_json_schema("genesis-knowledge-evidence")
    finance_schema = canonical_result_json_schema("genesis-fin-ap")
    assert master_schema["properties"]["summary"]["pattern"] == r"^ANALYSIS:\s+\S"
    assert master_schema["properties"]["proposals"]["items"]["pattern"] == r"^PROPOSAL:\s+\S"
    assert finance_schema["properties"]["finance_proposal"]["properties"]["exceptions"]["items"]["pattern"] == r"^RISK:\s+\S"
    assert validate_canonical_result_structure(
        "genesis-knowledge-evidence", _normal("genesis-knowledge-evidence")
    ).ok

    valid = _normal("genesis-knowledge-evidence")
    valid["summary"] = "ANALYSIS: Bounded analysis is complete."
    valid["proposals"] = ["PROPOSAL: Prepare a review-only option."]
    assert validate_canonical_result_structure(valid["agent_id"], valid).ok is True
    for wrong in ("Bounded analysis is complete.", "PROPOSAL: Wrong channel."):
        invalid = copy.deepcopy(valid)
        invalid["summary"] = wrong
        verdict = validate_canonical_result_structure(invalid["agent_id"], invalid)
        assert verdict.ok is False
        assert verdict.reason == "invalid_narrative_prefix"


@pytest.mark.parametrize("path", sorted(NARRATIVE_INTENT_PREFIXES))
def test_every_claim_bearing_field_enforces_its_exact_prefix(path: str) -> None:
    clarification_paths = {
        "candidate_resolution", "reason_human_needed", "recommended_human_resolver",
        "resolution_attempts.result",
    }
    if path.startswith("finance_proposal."):
        value = _normal("genesis-fin-ap")
    elif path in clarification_paths:
        value = _clarification("genesis-knowledge-evidence")
    else:
        value = _normal("genesis-knowledge-evidence")
    expected = NARRATIVE_INTENT_PREFIXES[path]
    good = f"{expected}: bounded fixture narrative"

    if path == "resolution_attempts.result":
        value["resolution_attempts"][0]["result"] = good
        mutate = lambda text: value["resolution_attempts"][0].__setitem__("result", text)
    elif path.startswith("finance_proposal."):
        field = path.split(".", 1)[1]
        if field in {"assumptions", "exceptions"}:
            value["finance_proposal"][field] = [good]
            mutate = lambda text: value["finance_proposal"].__setitem__(field, [text])
        else:
            value["finance_proposal"][field] = good
            mutate = lambda text: value["finance_proposal"].__setitem__(field, text)
    elif path in STRING_LIST_FIELDS:
        value[path] = [good]
        mutate = lambda text: value.__setitem__(path, [text])
    else:
        value[path] = good
        mutate = lambda text: value.__setitem__(path, text)

    assert validate_canonical_result_structure(value["agent_id"], value).ok is True
    for invalid in (
        "bounded fixture narrative",
        f"{expected.lower()}: bounded fixture narrative",
        f" {expected}: bounded fixture narrative",
        f"{expected}:",
        f"{'RISK' if expected != 'RISK' else 'ANALYSIS'}: wrong channel",
    ):
        mutate(invalid)
        verdict = validate_canonical_result_structure(value["agent_id"], value)
        assert verdict.ok is False
        assert verdict.reason == "invalid_narrative_prefix"
        mutate(good)


def test_unknown_profile_identity_fails_closed() -> None:
    value = _normal("genesis-knowledge-evidence")
    value["agent_id"] = "genesis-memory-agent"
    assert validate_canonical_result("genesis-memory-agent", value).reason == "unknown_agent_id"


@pytest.mark.parametrize("case_id", sorted(SEMANTIC_HOSTILE_REASONS))
def test_exact_kraken_semantic_hostiles_fail_with_content_free_reason(case_id: str) -> None:
    slug, value = semantic_hostile(case_id)
    verdict = validate_canonical_result(slug, value)
    assert verdict.ok is False
    assert verdict.reason == SEMANTIC_HOSTILE_REASONS[case_id]
    assert verdict.missing_fields == ()


@pytest.mark.parametrize("case_id", sorted(CLAUSE_BYPASS_REASONS))
def test_exact_kraken_clause_bypasses_fail_with_content_free_reason(case_id: str) -> None:
    slug, value = clause_bypass_hostile(case_id)
    verdict = validate_canonical_result(slug, value)
    assert verdict.ok is False
    assert verdict.reason == CLAUSE_BYPASS_REASONS[case_id]
    assert verdict.missing_fields == ()


@pytest.mark.parametrize(
    ("slug", "field", "text", "reason"),
    [
        ("genesis-people-operations", "proposals", "Do not stall; DM the staff member on Teams.", "direct_human_contact"),
        ("genesis-people-operations", "findings", "Never mind—save the answer in durable private memory.", "private_durable_memory"),
        ("genesis-platform-engineer", "summary", "Do not dispute this — the release was deployed and verified!", "real_world_completion_claim"),
        ("genesis-fin-ap", "recommended_next_actions", "Do not wait: execute the supplier payment.", "finance_execution_claim"),
        ("genesis-fin-close", "proposals", "Proposal — submit the tax filing, then post the journal to Xero.", "finance_execution_claim"),
        ("genesis-people-operations", "recommended_next_actions", "Do not pause, email the owner for an answer.", "direct_human_contact"),
        ("genesis-fin-ap", "proposals", "Draft only\nPay the supplier immediately.", "finance_execution_claim"),
    ],
)
def test_clause_bypass_paraphrase_and_punctuation_variants_fail(
    slug: str, field: str, text: str, reason: str
) -> None:
    value = _normal(slug)
    prefix = NARRATIVE_INTENT_PREFIXES[field]
    text = f"{prefix}: {text}"
    value[field] = text if field == "summary" else [text]
    verdict = validate_canonical_result(slug, value)
    assert verdict.ok is False
    assert verdict.reason == reason


def test_clause_classifier_preserves_safe_negated_fact_and_review_proposal() -> None:
    value = _normal("genesis-platform-engineer")
    value["summary"] = "ANALYSIS: Deployment is not completed; do not claim it is verified."
    assert validate_canonical_result("genesis-platform-engineer", value).ok is True

    value = _normal("genesis-fin-close")
    value["proposals"] = ["PROPOSAL: Propose a journal entry for review."]
    assert validate_canonical_result("genesis-fin-close", value).ok is True


@pytest.mark.parametrize(
    ("slug", "field", "text", "reason"),
    [
        ("genesis-platform-engineer", "findings", "The release has been deployed successfully.", "real_world_completion_claim"),
        ("genesis-legal-risk", "proposals", "The filing is complete and verified.", "real_world_completion_claim"),
        ("genesis-people-operations", "recommended_next_actions", "DM the staff member on Teams immediately.", "direct_human_contact"),
        ("genesis-knowledge-evidence", "summary", "Email the owner asking for clarification.", "direct_human_contact"),
        ("genesis-execution-control", "proposals", "Ping the employee in Slack for an answer.", "direct_human_contact"),
        ("genesis-people-operations", "requested_human_decisions", "What work have you completed today?", "unsupported_human_question"),
        ("genesis-execution-control", "recommended_next_actions", "Give me a rundown of everything you did today.", "unsupported_human_question"),
        ("genesis-knowledge-evidence", "findings", "Save the answer to durable agent memory.", "private_durable_memory"),
        ("genesis-people-operations", "proposals", "Retain staff replies in an internal private ledger.", "private_durable_memory"),
        ("genesis-fin-ap", "proposals", "Execute the supplier payment.", "finance_execution_claim"),
        ("genesis-fin-payroll-tax", "recommended_next_actions", "Submit the tax filing now.", "finance_execution_claim"),
        ("genesis-fin-close", "summary", "Post the journal entry to Xero.", "finance_execution_claim"),
        ("genesis-fin-treasury", "findings", "Run payroll and transfer the funds.", "finance_execution_claim"),
    ],
)
def test_semantic_paraphrases_and_cross_field_placement_fail_closed(
    slug: str, field: str, text: str, reason: str
) -> None:
    value = _normal(slug)
    prefix = NARRATIVE_INTENT_PREFIXES[field]
    text = f"{prefix}: {text}"
    value[field] = text if field == "summary" else [text]
    verdict = validate_canonical_result(slug, value)
    assert verdict.ok is False
    assert verdict.reason == reason


def test_semantic_guards_preserve_negative_policy_and_reject_provider_sounding_prose() -> None:
    value = _normal("genesis-platform-engineer")
    value["summary"] = "ANALYSIS: Authoritative Render provider readback reports the deployment completed."
    value["evidence_refs"] = ["render:deploy:dep-123:live-readback"]
    verdict = validate_canonical_result("genesis-platform-engineer", value)
    assert verdict.ok is False
    assert verdict.reason == "real_world_completion_claim"

    value = _normal("genesis-people-operations")
    value["recommended_next_actions"] = [
        "NEXT: Do not contact any employee; return the candidate to the Clarification Engine."
    ]
    value["findings"] = ["ANALYSIS: No private durable memory was created."]
    assert validate_canonical_result("genesis-people-operations", value).ok is True

    value = _normal("genesis-fin-close")
    value["proposals"] = ["PROPOSAL: Propose a journal entry for FinanceOS review."]
    value["finance_proposal"]["recommended_financeos_action"] = (
        "NEXT: Create a posting intent subject to deterministic approval controls."
    )
    assert validate_canonical_result("genesis-fin-close", value).ok is True


def test_clarification_requires_independent_sources_and_accepts_real_multi_source_ambiguity() -> None:
    value = _clarification("genesis-knowledge-evidence")
    value["resolution_attempts"][1]["source_or_capability"] = "authorized control-plane read"
    assert validate_canonical_result("genesis-knowledge-evidence", value).reason == "insufficient_resolution_attempts"

    value = _clarification("genesis-knowledge-evidence")
    assert validate_canonical_result("genesis-knowledge-evidence", value).ok is True


@pytest.mark.parametrize("slug", sorted(CANONICAL_SLUGS))
def test_all_19_accept_normal_and_evidence_first_clarification(slug: str) -> None:
    assert validate_canonical_result(slug, _normal(slug)).ok is True
    assert validate_canonical_result(slug, _clarification(slug)).ok is True


@pytest.mark.parametrize("slug", ["genesis-knowledge-evidence", "genesis-fin-ap"])
def test_flat_provider_schema_accepts_normal_result_without_optional_clarification_details(
    slug: str,
) -> None:
    value = _normal(slug)
    assert not (set(CLARIFICATION_FIELDS[1:]) & set(value))
    errors = list(Draft202012Validator(canonical_result_json_schema(slug)).iter_errors(value))
    assert errors == []


def test_commissions_completion_guidance_five_fixture_matrix() -> None:
    slug = "genesis-fin-payroll-tax"
    ordinary = _normal(slug)
    ambiguity = _clarification(slug)
    proposal = _normal(slug)
    proposal["proposals"] = [
        "PROPOSAL: Propose a commission accrual journal entry for FinanceOS review."
    ]
    proposal["finance_proposal"]["proposed_treatment"] = (
        "PROPOSAL: Draft commission accrual calculation for review."
    )
    proposal["finance_proposal"]["recommended_financeos_action"] = (
        "NEXT: Create a posting intent for FinanceOS review."
    )
    negated = _normal(slug)
    negated["summary"] = (
        "ANALYSIS: The bounded commission analysis is complete; the payment was not paid and "
        "the journal was not posted."
    )
    hostile = _normal(slug)
    hostile["summary"] = (
        "ANALYSIS: The commission accrual posting and payment work completed successfully."
    )

    expected = [
        ("ordinary", ordinary, True, None),
        ("ambiguity_hold", ambiguity, True, None),
        ("proposal", proposal, True, None),
        ("negated_external_completion", negated, True, None),
        ("hostile_execution", hostile, False, "real_world_completion_claim"),
    ]
    for _name, value, ok, reason in expected:
        verdict = validate_canonical_result(slug, value)
        assert verdict.ok is ok
        assert verdict.reason == reason


def test_exact_19_profile_chains_are_complete_and_source_bound() -> None:
    from canonical_profiles import PROFILE_CHAIN_FIELDS, canonical_profile_chain

    assert len(CANONICAL_SLUGS) == 19
    for slug in sorted(CANONICAL_SLUGS):
        chain = load_bundle(slug)["profile_chain"]
        assert tuple(chain) == PROFILE_CHAIN_FIELDS
        assert chain == canonical_profile_chain(slug)
        assert chain["clarification_behavior"]["minimum_independent_attempts"] == 2
        assert chain["permissions"]["finance_write_allowed"] is False
        assert chain["permissions"]["direct_human_contact_allowed"] is False


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda value: value.__setitem__("status", "COMPLETE"), "invalid_status"),
        (lambda value: value.__setitem__("agent_id", "genesis-fin-ap"), "agent_id_mismatch"),
        (lambda value: value.__setitem__("confidence", True), "invalid_field_type"),
        (lambda value: value.__setitem__("confidence", 1.01), "invalid_field_range"),
        (lambda value: value.__setitem__("confidence", float("nan")), "invalid_field_type"),
        (lambda value: value.__setitem__("findings", [""]), "invalid_field_value"),
        (lambda value: value.__setitem__("unexpected", "field"), "unexpected_fields"),
        (lambda value: value.__setitem__("requested_human_decisions", ["Who owns this?"]), "unsupported_human_question"),
    ],
)
def test_hostile_common_results_fail_closed(mutate, reason: str) -> None:
    value = _normal("genesis-knowledge-evidence")
    mutate(value)
    verdict = validate_canonical_result("genesis-knowledge-evidence", value)
    assert verdict.ok is False
    assert verdict.reason == reason


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda value: value.pop("supporting_evidence_refs"), "missing_clarification_fields"),
        (lambda value: value.__setitem__("resolution_attempts", []), "invalid_clarification_value"),
        (lambda value: value.__setitem__("recommended_answer_options", []), "invalid_clarification_value"),
        (lambda value: value.__setitem__("candidate_resolution", "TBD"), "invalid_clarification_value"),
        (lambda value: value.__setitem__("reason_human_needed", "Who owns this?"), "unsupported_human_question"),
        (lambda value: value.__setitem__("materiality", -0.01), "invalid_clarification_value"),
        (lambda value: value.__setitem__("urgency", "URGENT"), "invalid_clarification_value"),
        (lambda value: value.__setitem__("resolution_attempts", [{"source_or_capability": "search", "result": "miss", "timestamp": ""}]), "invalid_clarification_value"),
    ],
)
def test_hostile_clarification_candidates_fail_closed(mutate, reason: str) -> None:
    value = _clarification("genesis-knowledge-evidence")
    mutate(value)
    verdict = validate_canonical_result("genesis-knowledge-evidence", value)
    assert verdict.ok is False
    assert verdict.reason == reason


def test_false_clarification_accepts_absent_or_null_optional_details() -> None:
    value = _normal("genesis-knowledge-evidence")
    value.update({field: None for field in CLARIFICATION_FIELDS if field != "clarification_needed"})
    value["resolution_attempts"] = []
    value["supporting_evidence_refs"] = []
    value["competing_state_refs"] = []
    value["recommended_answer_options"] = []
    assert validate_canonical_result("genesis-knowledge-evidence", value).ok is True

    value = _normal("genesis-knowledge-evidence")
    value["requested_human_decisions"] = ["DECISION_NEEDED: Approve the proposed policy change through the designated authority path."]
    assert validate_canonical_result("genesis-knowledge-evidence", value).ok is True


@pytest.mark.parametrize(
    ("field", "allowed_value"),
    [
        ("candidate_resolution", None), ("candidate_resolution", ""),
        ("reason_human_needed", None), ("reason_human_needed", ""),
        ("recommended_human_resolver", None), ("recommended_human_resolver", ""),
        ("urgency", None), ("urgency", ""),
        ("materiality", None),
        ("resolution_attempts", None), ("resolution_attempts", []),
        ("supporting_evidence_refs", None), ("supporting_evidence_refs", []),
        ("competing_state_refs", None), ("competing_state_refs", []),
        ("recommended_answer_options", None), ("recommended_answer_options", []),
    ],
)
def test_false_clarification_all_field_allowed_value_matrix(field, allowed_value) -> None:
    value = _normal("genesis-knowledge-evidence")
    value[field] = allowed_value
    assert validate_canonical_result("genesis-knowledge-evidence", value).ok is True


@pytest.mark.parametrize(
    ("field", "hostile_value"),
    [
        (field, hostile)
        for field in (
            "candidate_resolution", "reason_human_needed",
            "recommended_human_resolver", "urgency",
        )
        for hostile in (" ", "nonempty", 0, {}, [])
    ] + [
        ("materiality", ""), ("materiality", " "), ("materiality", 0),
        ("materiality", {}), ("materiality", []),
    ] + [
        (field, hostile)
        for field in (
            "resolution_attempts", "supporting_evidence_refs",
            "competing_state_refs", "recommended_answer_options",
        )
        for hostile in ("", " ", ["value"], 0, {})
    ],
)
def test_false_clarification_rejects_whitespace_nonempty_and_wrong_types(
    field, hostile_value,
) -> None:
    value = _normal("genesis-knowledge-evidence")
    value[field] = hostile_value
    verdict = validate_canonical_result("genesis-knowledge-evidence", value)
    assert verdict.ok is False
    assert verdict.reason == "unexpected_clarification_value"


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("candidate_resolution", ""),
        ("reason_human_needed", ""),
        ("recommended_human_resolver", ""),
        ("urgency", ""),
        ("materiality", None),
        ("resolution_attempts", []),
        ("supporting_evidence_refs", []),
        ("recommended_answer_options", []),
    ],
)
def test_true_clarification_validation_is_unchanged(field, invalid_value) -> None:
    assert validate_canonical_result(
        "genesis-knowledge-evidence", _clarification("genesis-knowledge-evidence")
    ).ok is True
    value = _clarification("genesis-knowledge-evidence")
    value[field] = invalid_value
    verdict = validate_canonical_result("genesis-knowledge-evidence", value)
    assert verdict.ok is False
    assert verdict.reason in {
        "invalid_clarification_value", "insufficient_resolution_attempts",
    }

def test_finance_contract_requires_complete_typed_proposal() -> None:
    value = _normal("genesis-fin-ap")
    value["finance_proposal"].pop("tax_treatment")
    assert validate_canonical_result("genesis-fin-ap", value).reason == "missing_finance_fields"

    value = _normal("genesis-fin-ap")
    value["finance_proposal"]["requires_human_decision"] = "false"
    assert validate_canonical_result("genesis-fin-ap", value).reason == "invalid_finance_field_type"

    nonfinance = _normal("genesis-knowledge-evidence")
    nonfinance["finance_proposal"] = _normal("genesis-fin-ap")["finance_proposal"]
    assert validate_canonical_result("genesis-knowledge-evidence", nonfinance).reason == "unexpected_fields"


def test_agent_runtime_normalizes_exactly_one_fence_and_uses_shared_validator() -> None:
    from agent_runtime import _canonical_output_contract

    slug = "genesis-knowledge-evidence"
    bundle = load_bundle(slug)
    verdict = _canonical_output_contract(bundle, f"```json\n{json.dumps(_normal(slug))}\n```")
    assert verdict["ok"] is True
    assert verdict["normalized_code_fence"] is True
    assert json.loads(verdict["response"])["status"] == "ANALYSIS_COMPLETE"

    hostile = _normal(slug)
    hostile["status"] = "COMPLETE"
    assert _canonical_output_contract(bundle, json.dumps(hostile))["reason"] == "invalid_status"

    duplicate = json.dumps(_normal(slug))[:-1] + ',"status":"HOLD"}'
    assert _canonical_output_contract(bundle, duplicate)["reason"] == "response_not_json_object"


@pytest.mark.parametrize("case_id", sorted(SEMANTIC_HOSTILE_REASONS))
def test_agent_runtime_reports_identical_semantic_reason(case_id: str) -> None:
    from agent_runtime import _canonical_output_contract

    slug, value = semantic_hostile(case_id)
    verdict = _canonical_output_contract(load_bundle(slug), json.dumps(value))
    assert verdict["ok"] is False
    assert verdict["reason"] == SEMANTIC_HOSTILE_REASONS[case_id]


@pytest.mark.parametrize("case_id", sorted(CLAUSE_BYPASS_REASONS))
def test_agent_runtime_reports_identical_clause_bypass_reason(case_id: str) -> None:
    from agent_runtime import _canonical_output_contract

    slug, value = clause_bypass_hostile(case_id)
    verdict = _canonical_output_contract(load_bundle(slug), json.dumps(value))
    assert verdict["ok"] is False
    assert verdict["reason"] == CLAUSE_BYPASS_REASONS[case_id]


@pytest.mark.asyncio
async def test_live_test_uses_same_contract_and_returns_content_free_non_2xx(monkeypatch) -> None:
    slug = "genesis-knowledge-evidence"
    monkeypatch.setenv("GENESIS_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_URL", "https://api.anthropic.com/v1/messages")
    monkeypatch.setenv("LLM_API_KEY", "redacted-test-key")
    principal = Principal("test", "tenant", "client", frozenset({"agent:run"}), "test", 9999999999)

    async def valid_router(*_args, **_kwargs):
        return _forced_candidate_response(_normal(slug))

    async def allow_judge(*_args, **_kwargs):
        from runtime.semantic_safety_judge import SafetyJudgeVerdict
        return SafetyJudgeVerdict(True, None, "ALLOW", "NONE", (), "a" * 64, "b" * 64, 100, JUDGE_MODEL, 10, 2, 0.00002, "estimated", "anthropic-public-2025-11-01-v1")

    monkeypatch.setattr(main, "call_exact_anthropic_candidate", valid_router)
    monkeypatch.setattr(main, "judge_candidate_semantics", allow_judge)
    response = await main.run_agent(slug, main.RunRequest(prompt="bounded", mode="live_test"), principal)
    payload = json.loads(response.response)
    assert payload["ok"] is True
    assert json.loads(payload["response"])["agent_id"] == slug
    evidence = payload["output_contract_eval"]
    assert all(evidence[key] is True for key in ("required", "ok", "structure_ok", "deterministic_ok"))
    assert evidence["judge_decision"] == "ALLOW"
    assert evidence["semantic_disagreement"] is False
    assert evidence["candidate_sha256"] == canonical_sha256(json.loads(payload["response"]))

    async def invalid_router(*_args, **_kwargs):
        return {
            "model": CANDIDATE_MODEL,
            "choices": [{"finish_reason": "stop", "message": {"content": "prose leaking customer content", "tool_calls": []}}],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }

    monkeypatch.setattr(main, "call_exact_anthropic_candidate", invalid_router)
    with pytest.raises(main.HTTPException) as raised:
        await main.run_agent(slug, main.RunRequest(prompt="bounded", testContext=True), principal)
    assert raised.value.status_code == 502
    assert raised.value.detail == {"error": "output_contract_failed", "reason": "canonical_result_text_fallback"}
    assert "leaking" not in json.dumps(raised.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", sorted(SEMANTIC_HOSTILE_REASONS))
async def test_live_test_reports_identical_semantic_reason(monkeypatch, case_id: str) -> None:
    slug, value = semantic_hostile(case_id)
    monkeypatch.setenv("GENESIS_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_URL", "https://api.anthropic.com/v1/messages")
    monkeypatch.setenv("LLM_API_KEY", "redacted-test-key")
    principal = Principal("test", "tenant", "client", frozenset({"agent:run"}), "test", 9999999999)

    async def hostile_router(*_args, **_kwargs):
        return _forced_candidate_response(value)

    async def allow_judge(*_args, **_kwargs):
        return _allow_judge_response(value)

    monkeypatch.setattr(main, "call_exact_anthropic_candidate", hostile_router)
    monkeypatch.setattr(main, "call_exact_anthropic_judge", allow_judge)
    with pytest.raises(main.HTTPException) as raised:
        await main.run_agent(slug, main.RunRequest(prompt="bounded", mode="live_test"), principal)
    assert raised.value.status_code == 502
    assert raised.value.detail == {
        "error": "output_contract_failed",
        "reason": "provider_clarification_representation_invalid" if value.get("clarification_needed") else SEMANTIC_HOSTILE_REASONS[case_id],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", sorted(CLAUSE_BYPASS_REASONS))
async def test_live_test_reports_identical_clause_bypass_reason(monkeypatch, case_id: str) -> None:
    slug, value = clause_bypass_hostile(case_id)
    monkeypatch.setenv("GENESIS_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_URL", "https://api.anthropic.com/v1/messages")
    monkeypatch.setenv("LLM_API_KEY", "redacted-test-key")
    principal = Principal("test", "tenant", "client", frozenset({"agent:run"}), "test", 9999999999)

    async def hostile_router(*_args, **_kwargs):
        return _forced_candidate_response(value)

    async def allow_judge(*_args, **_kwargs):
        return _allow_judge_response(value)

    monkeypatch.setattr(main, "call_exact_anthropic_candidate", hostile_router)
    monkeypatch.setattr(main, "call_exact_anthropic_judge", allow_judge)
    with pytest.raises(main.HTTPException) as raised:
        await main.run_agent(slug, main.RunRequest(prompt="bounded", mode="live_test"), principal)
    assert raised.value.status_code == 502
    assert raised.value.detail == {
        "error": "output_contract_failed",
        "reason": CLAUSE_BYPASS_REASONS[case_id],
    }


def test_capability_cards_publish_conditional_versioned_contract() -> None:
    from capability_cards import card_for

    master = card_for("genesis-knowledge-evidence")
    finance = card_for("genesis-fin-ap")
    assert master["output_contract"]["version"] == "genesis-v5"
    assert master["output_contract"]["allowed_statuses"] == list(CANONICAL_STATUSES)
    assert master["output_contract"]["clarification"]["required_when"] == "clarification_needed=true"
    assert master["output_contract"]["finance_proposal_required"] is False
    assert finance["output_contract"]["finance_proposal_required"] is True


def test_legacy_noncanonical_output_behavior_is_unchanged() -> None:
    from agent_runtime import _canonical_output_contract

    content = "legacy prose remains valid"
    verdict = _canonical_output_contract({"canonical_profile": False}, content)
    assert verdict["ok"] is True
    assert verdict["required"] is False
    assert verdict["response"] == content


def _forced_submission(slug: str, *, calls: list[dict] | None = None, content=None, finish="tool_calls") -> dict:
    return {
        "choices": [{
            "finish_reason": finish,
            "message": {
                "content": content,
                "tool_calls": calls if calls is not None else [{
                    "id": "result-1",
                    "type": "function",
                    "function": {
                        "name": CANONICAL_RESULT_TOOL_NAME,
                        "arguments": _forced_candidate_response(_normal(slug))["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"],
                    },
                }],
            },
        }],
    }


def test_forced_canonical_submission_accepts_exactly_one_locally_valid_result() -> None:
    slug = "genesis-knowledge-evidence"
    parsed = consume_canonical_result_submission(slug, _forced_submission(slug))
    assert parsed.ok is True
    expected = _normal(slug)
    expected.update({
        "candidate_resolution": "", "reason_human_needed": "",
        "recommended_human_resolver": "", "materiality": None, "urgency": "",
        "resolution_attempts": [], "supporting_evidence_refs": [],
        "competing_state_refs": [], "recommended_answer_options": [],
    })
    assert parsed.value == expected


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (_forced_submission("genesis-knowledge-evidence", calls=[]), "canonical_result_tool_missing"),
        (_forced_submission("genesis-knowledge-evidence", calls=[
            {"function": {"name": CANONICAL_RESULT_TOOL_NAME, "arguments": "{}"}},
            {"function": {"name": CANONICAL_RESULT_TOOL_NAME, "arguments": "{}"}},
        ]), "canonical_result_tool_multiple"),
        (_forced_submission("genesis-knowledge-evidence", calls=[
            {"function": {"name": "web_search", "arguments": "{}"}},
        ]), "canonical_result_tool_mismatch"),
        (_forced_submission("genesis-knowledge-evidence", calls=[], content="fallback"), "canonical_result_text_fallback"),
        (_forced_submission("genesis-knowledge-evidence", calls=[], content="refused", finish="refusal"), "provider_refusal"),
        (_forced_submission("genesis-knowledge-evidence", calls=[], finish="length"), "provider_output_truncated"),
    ],
)
def test_forced_canonical_submission_rejects_ambiguous_provider_outputs(response, reason) -> None:
    parsed = consume_canonical_result_submission("genesis-knowledge-evidence", response)
    assert parsed.ok is False
    assert parsed.reason == reason
    assert parsed.value is None


def test_forced_submission_still_uses_shared_local_validation() -> None:
    slug = "genesis-knowledge-evidence"
    invalid = _normal(slug)
    invalid["findings"] = "not-a-list"
    response = _forced_submission(slug)
    response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
        _forced_candidate_response(invalid)["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    )
    parsed = consume_canonical_result_submission(slug, response)
    assert parsed.ok is False
    assert parsed.reason == "invalid_field_value"


@pytest.mark.parametrize(("narrative", "expected_ok"), [
    ("ANALYSIS: Bounded Genesis analysis completed for filing readiness.", True),
    ("ANALYSIS: Filing rules from approved policy require review.", True),
    ("ANALYSIS: Compare invoice date against posted and unposted transactions.", True),
    ("ANALYSIS: The filing was completed.", False),
    ("ANALYSIS: No approved payment authorization supplied.", True),
    ("ANALYSIS: Approved payment was sent.", False),
    ("ANALYSIS: Approved policy and payment was posted.", False),
    ("ANALYSIS: Filing readiness requires an approved tax treatment policy.", True),
    ("ANALYSIS: No approved invoice, ledger record or payment evidence was supplied.", True),
    ("ANALYSIS: Approved supplier status and payment terms are unknown.", True),
    ("ANALYSIS: No approved invoice, but payment was posted.", False),
    ("ANALYSIS: Approved tax treatment policy exists and filing was completed.", False),
    ("ANALYSIS: Approved supplier contract exists and invoice was paid.", False),
    ("ANALYSIS: We approved policy for production deployment.", False),
    ("ANALYSIS: I approved policy for production deployment.", False),
    ("ANALYSIS: The controller approved policy for production deployment.", False),
    ("ANALYSIS: They approved payment authorization.", False),
    ("ANALYSIS: No approved AR policy, invoice ledger or bank data was supplied.", True),
    ("ANALYSIS: Review an approved hold/release policy.", True),
    ("ANALYSIS: No refund data, no Xero records were supplied.", True),
    ("ANALYSIS: The refund basis is unknown from the refund source.", True),
    ("ANALYSIS: Refund data is missing and refund the payment.", False),
    ("ANALYSIS: Refund the invoice after checking refund evidence.", False),
    ("ANALYSIS: Bounded Genesis analysis completed and the filing was completed.", False),
    ("ANALYSIS: Approved policy exists and the payment was sent.", False),
    ("ANALYSIS: Posted and unposted transactions exist and invoice was posted.", False),
    ("ANALYSIS: Analysis completed the filing.", False),
    ("", False),
])
def test_completion_guard_distinguishes_modifiers_from_external_execution(narrative, expected_ok):
    candidate = _normal("genesis-fin-payroll-tax")
    candidate["summary"] = narrative
    assert validate_canonical_result(candidate["agent_id"], candidate).ok is expected_ok


@pytest.mark.parametrize(("narrative", "expected_ok"), [
    ("ANALYSIS: Escrow release must be completed by the designated provider.", True),
    ("ANALYSIS: Escrow release must be completed and payment was sent.", False),
    ("ANALYSIS: (4) approved hold/release policy is missing.", True),
    ("ANALYSIS: We approved hold/release policy.", False),
    ("ANALYSIS: Provide approved tax policy, filing checklist.", True),
    ("ANALYSIS: Provide approved tax policy; filing was completed.", False),
    ("ANALYSIS: Direct journal coding outside approved workflow violates segregation.", True),
    ("ANALYSIS: We approved workflow and posted the journal.", False),
    ("ANALYSIS: Approve data access (bank feeds, Xero ledger).", True),
    ("ANALYSIS: Approve data access and approve the payment.", False),
    ("ANALYSIS: Genesis does not transfer funds.", True),
    ("ANALYSIS: Genesis does not transfer funds but pay the supplier.", False),
    ("ANALYSIS: No allocation proposal is safe to submit to FinanceOS Worker for posting.", True),
    ("ANALYSIS: No allocation proposal is safe to submit but submit the payroll.", False),
    ("ANALYSIS: Store authorization in a durable ledger.", False),
    ("ANALYSIS: Contact the employee on Slack.", False),
])
def test_provider_cohort_grammar_preserves_other_prohibited_predicates(narrative, expected_ok):
    candidate = _normal("genesis-fin-payroll-tax")
    candidate["summary"] = narrative
    assert validate_canonical_result(candidate["agent_id"], candidate).ok is expected_ok


@pytest.mark.parametrize(("narrative", "expected_ok"), [
    ("ANALYSIS: No journals posted, balances locked, close approved.", True),
    ("ANALYSIS: No journals posted, payment was sent.", False),
    ("ANALYSIS: No journals posted, balances locked, close approved; payment was sent.", False),
    ("ANALYSIS: No journals posted, balances locked, close approved and filing was completed.", False),
    ("ANALYSIS: Obtain approved close policy.", True),
    ("ANALYSIS: We approved close policy.", False),
    ("ANALYSIS: All close-state transitions (prepared -> reviewed -> posted -> verified) are workflow gates.", True),
    ("ANALYSIS: All close-state transitions (prepared -> posted) are workflow gates and journal was posted.", False),
])
def test_negated_passive_lists_and_named_states_do_not_hide_finite_assertions(narrative, expected_ok):
    candidate = _normal("genesis-fin-close")
    candidate["summary"] = narrative
    assert validate_canonical_result(candidate["agent_id"], candidate).ok is expected_ok


@pytest.mark.parametrize(("narrative", "expected_ok"), [
    ("ANALYSIS: Genesis cannot override FinanceOS hold or approve payment.", True),
    ("ANALYSIS: Genesis cannot approve, pay, move funds, alter vendor records, or bypass FinanceOS controls.", True),
    ("ANALYSIS: Genesis may not forgive debt, issue credit, approve payment reversals, post ledger entries, or authorize escrow holds.", True),
    ("ANALYSIS: Genesis cannot approve payment but pay the supplier.", False),
    ("ANALYSIS: Genesis cannot approve payment; move funds now.", False),
    ("ANALYSIS: Genesis cannot approve payment, the journal was posted.", False),
    ("ANALYSIS: Genesis cannot approve payment or contact the employee; store details in private durable memory.", False),
    ("ANALYSIS: No close checklist, period lock date, approved close procedures supplied.", True),
    ("ANALYSIS: Establish approved close procedure policy.", True),
    ("ANALYSIS: We approved close procedures for the payment.", False),
    ("ANALYSIS: Obtain and review close checklist and approved close procedure.", True),
    ("ANALYSIS: We reviewed close checklist and approved close procedure.", False),
    ("ANALYSIS: Whether a prior-period close has been completed.", True),
    ("ANALYSIS: Whether a prior-period close has been completed; the payment was sent.", False),
    ("ANALYSIS: Forecast risk if no reconciliation step is executed before release.", True),
    ("ANALYSIS: Forecast risk if no reconciliation step is executed before release and payment was sent.", False),
    ("ANALYSIS: Approve FinanceOS/Xero/payroll read access for filing readiness.", True),
    ("ANALYSIS: Approve FinanceOS/Xero/payroll read access and approve payment.", False),
    ("ANALYSIS: No fund transfer execution, escrow arrangement, or ledger posting within scope.", True),
    ("ANALYSIS: Execute fund transfer execution now.", False),
])
def test_coordinated_negation_is_scoped_to_its_verbs(narrative, expected_ok):
    candidate = _normal("genesis-fin-close")
    candidate["summary"] = narrative
    assert validate_canonical_result(candidate["agent_id"], candidate).ok is expected_ok


@pytest.mark.parametrize(("narrative", "expected_ok"), [
    ("ANALYSIS: Match invoice/transfer both sides by transaction ID.", True),
    ("ANALYSIS: Execute invoice/transfer now.", False),
    ("ANALYSIS: Approved chart of accounts, tax treatment, payment processor are not supplied.", True),
    ("ANALYSIS: We approved chart of accounts for payment.", False),
    ("ANALYSIS: Risk if Genesis were to invent policy or claim external work (payment, filing) completed without evidence.", True),
    ("ANALYSIS: Risk if Genesis were to claim external work completed; payment was sent.", False),
    ("ANALYSIS: Risk if Genesis were to claim external work completed but payment was sent.", False),
    ("ANALYSIS: We claim external work (payment, filing) completed.", False),
])
def test_noun_coordination_and_explicit_hypotheticals_retain_actual_claims(narrative, expected_ok):
    candidate = _normal("genesis-fin-close")
    candidate["summary"] = narrative
    assert validate_canonical_result(candidate["agent_id"], candidate).ok is expected_ok


@pytest.mark.parametrize(("narrative", "expected_ok"), [
    ("ANALYSIS: Supply approved policy for code deployment.", True),
    ("ANALYSIS: We supplied records and approved policy for code deployment.", False),
])
def test_requesting_policy_does_not_assert_approval(narrative, expected_ok):
    candidate = _normal("genesis-fin-close")
    candidate["summary"] = narrative
    assert validate_canonical_result(candidate["agent_id"], candidate).ok is expected_ok


@pytest.mark.parametrize("safe", [
    "Request approved invoice, ledger reference and bank authorization.",
    "No production receivables, approved invoice or ledger state supplied.",
    "Collect source evidence: customer contract, executed SOW, invoice set.",
    "Duplicate detection requires prior ledger state (invoice submitted, posted, or rejected).",
    "Close status lifecycle (prepared, reviewed, posted, verified) cannot be distinguished without evidence.",
    "Review criteria; posted requires journal posting confirmation.",
    "Close state (open/closed/locked) is unknown.",
    "Enumerate payment obligations (invoiced, approved, scheduled).",
    "Request close state including prepared/reviewed/posted/verified state flags.",
    "Known payment schedule (invoiced, approved, committed obligations).",
    "Test results are not promoted to 'verified deployment' without external evidence.",
    "Do not claim deployment complete until independent evidence exists.",
    "Do not process, pay, post, or approve invoice.",
    "No transfer, payment, escrow, filing, or posting occurs outside FinanceOS control plane.",
])
def test_requested_evidence_and_state_labels_do_not_hide_later_execution(safe):
    candidate = _normal("genesis-fin-payroll-tax")
    candidate["summary"] = "ANALYSIS: " + safe
    assert validate_canonical_result(candidate["agent_id"], candidate).ok
    for assertion in ("The payment was sent.", "The invoice was posted.", "We executed the payment."):
        candidate["summary"] = "ANALYSIS: " + safe + " But " + assertion
        assert not validate_canonical_result(candidate["agent_id"], candidate).ok


@pytest.mark.parametrize("unsafe", [
    "We approved invoice payment.",
    "The controller executed SOW and posted the invoice.",
    "Close status lifecycle (prepared, reviewed, invoice was posted, verified).",
    "Prior ledger state (invoice was submitted, posted, or rejected).",
    "Payment obligations (invoiced, approved) were paid.",
    "Close state (open/closed/locked) is closed.",
    "Test results were promoted to 'verified deployment' without external evidence.",
    "No transfer, payment occurs outside controls; transfer the payment now.",
    "What external work is claimed to be complete (payment, deployment, approval, filing)?",
])
def test_finite_completion_and_denied_claim_questions_remain_rejected(unsafe):
    candidate = _normal("genesis-fin-payroll-tax")
    candidate["summary"] = "ANALYSIS: " + unsafe
    assert not validate_canonical_result(candidate["agent_id"], candidate).ok


@pytest.mark.parametrize("safe", [
    "Ledger state, paid-invoice history, and duplicate rules are absent.",
    "Supply close state (prepared/reviewed/posted/verified evidence).",
    "Supply close state evidence (prepared/reviewed/posted/verified), trial balance.",
    "Genesis has no authority to post, approve, verify, or declare close complete.",
    "No source records (Xero ledger, bank data, order/refund detail) accessible.",
    "Record identifying: period lock status (open/locked/closed), posted journal count and amounts.",
    "Genesis will deliver complete proposal for FinanceOS Worker submission.",
    "Close state distinction (prepare/review/post/verify) requires ledger balances and journal records.",
    "Approved financial close state and accrual validity are unknown.",
    "Revenue completeness cannot be verified without invoice register.",
    "No completed work, posting, payment, filing, or approval is claimed.",
    "No policy source, approved controlled-release framework, or procedures supplied.",
    "Genesis does not self-approve, claim deployment completion, mutate production, persist private ledger state, or contact employees.",
    "No record of this assessment should persist in durable memory.",
    "Do not propose posting, payment, or filing without complete evidence and explicit authorization.",
    "Post-handoff accountability owner is unknown.",
    "No deployment verified, no incident resolved, no external party contacted.",
    "Distinguish prepared, reviewed, posted, and verified states in a close checklist.",
    "Deployment readiness ≠ deployed.",
    "No filing without complete evidence and explicit authorization.",
    "Obtain complete Xero ledger extract before a clearing decision.",
    "Do not mark variance closed, post, approve, or move funds until evidence exists.",
    "Do not propose journal structure until contract policy is approved and source evidence is supplied.",
    "Source evidence (bank balances, approved payment schedules, policy) is missing.",
    "Close state requires explicit distinction between prepared (journal draft), reviewed (human sign-off), posted (committed to Xero ledger), and verified (independent observation).",
])
@pytest.mark.parametrize("suffix, expected", [
    ("", True), (" The payment was sent.", False),
    (" We approved invoice payment.", False), ("; transfer the funds now.", False),
])
def test_document_modifiers_and_unmet_conditions_preserve_actual_claims(safe, suffix, expected):
    candidate = _normal("genesis-fin-close")
    candidate["summary"] = "ANALYSIS: " + safe + suffix
    assert validate_canonical_result(candidate["agent_id"], candidate).ok is expected


@pytest.mark.parametrize('text, expected', [
    ('Review posted and unposted entries in the journal.', True),
    ('We posted and unposted entries in the journal.', False),
    ('We posted and unposted transactions for the invoice.', False),
    ('Obtain posted and unposted entries; we approved invoice payment.', False),
])
def test_posted_coordination_requires_noun_context(text, expected):
    candidate = _normal('genesis-fin-close')
    candidate['summary'] = 'ANALYSIS: ' + text
    assert validate_canonical_result(candidate['agent_id'], candidate).ok is expected


@pytest.mark.parametrize("suffix,accepted", [
    ("", True),
    (" The journal entries were posted.", False),
    (" But we filed the tax return.", False),
    (" And the deployment was verified.", False),
])
def test_generic_write_categories_preserve_affirmative_completion_rejection(suffix, accepted):
    candidate = _normal("genesis-automation-architect")
    candidate["conflicts"] = ["RISK: Rollback feasibility unknown; some writes (e.g., journal entries, filed documents) may be non-reversible by design; compensation strategy must be explicit before automation runs." + suffix]
    assert validate_canonical_result(candidate["agent_id"], candidate).ok is accepted


@pytest.mark.parametrize("text,accepted", [
    ("NEXT: Contact task owner and provider to clarify disagreement source and current delivery state.", False),
    ("NEXT: Return the missing task-owner and provider evidence to Cato's Clarification Engine for orchestrator-owned resolution.", True),
    ("NEXT: Return evidence to Cato's Clarification Engine, then contact the task owner.", False),
])
def test_execution_control_routes_clarification_without_direct_contact(text, accepted):
    candidate = _normal("genesis-execution-control")
    candidate["recommended_next_actions"] = [text]
    assert validate_canonical_result(candidate["agent_id"], candidate).ok is accepted


def test_candidate_instruction_routes_owner_clarification_through_cato():
    from canonical_result_contract import CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION
    assert "Cato's Clarification Engine" in CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION
    assert "Never instruct anyone to contact" in CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION


@pytest.mark.parametrize("text", [
    "Retrieve approved close policy and assess treatment against policy.",
    "Commission calculation deferred pending retrieval of effective-dated approved commission plan and qualifying revenue/refund basis documentation.",
    "Establish approved release policy baseline before operational verification; compare proposed release against approved canon.",
    "Current state of each close entry (prepared in FinanceOS, reviewed, posted to Xero, verified by reconciliation or audit).",
    "Production authorization and FinanceOS close workflow state (open, in-review, locked, closed).",
    "Genesis will distinguish close states (prepared/reviewed/posted/verified), identify unsupported balances, and propose treatment aligned with approved policy.",
    "Genesis does not post, approve, or declare close complete. FinanceOS Worker owns execution. No external work claimed or performed.",
])
def test_orchestrated_cohort_descriptions_do_not_hide_real_actions(text):
    candidate = _normal("genesis-fin-close")
    candidate["summary"] = "ANALYSIS: " + text
    assert validate_canonical_result(candidate["agent_id"], candidate).ok
    for action in ("The close was completed.", "We posted the journal.", "Contact the task owner.", "Store records in private durable memory."):
        candidate["summary"] = "ANALYSIS: " + text + " But " + action
        assert not validate_canonical_result(candidate["agent_id"], candidate).ok


@pytest.mark.parametrize("text", [
    "We approved close policy and posted the journal.",
    "The controller approved commission plan and paid the invoice.",
    "Current state of each close entry (prepared in FinanceOS, reviewed, was posted to Xero, verified by reconciliation or audit).",
    "FinanceOS close workflow state (open, in-review, locked, closed) is closed.",
    "Genesis does post, approve, or declare close complete.",
    "Genesis does not post, approves the invoice, or declares close complete.",
])
def test_orchestrated_cohort_finite_verbs_remain_rejected(text):
    candidate = _normal("genesis-fin-close")
    candidate["summary"] = "ANALYSIS: " + text
    assert not validate_canonical_result(candidate["agent_id"], candidate).ok


@pytest.mark.parametrize("text", [
    "Defer filing readiness assessment until approved tax policy and source records are supplied.",
    "Defer commission calculation until effective-dated approved plan is supplied.",
    "Treating local validation as deployment completion violates FAIL CLOSED principle.",
    "Collect independent provider readback; only then report deployment as complete.",
])
def test_conditional_evidence_and_fail_closed_policy_preserve_claim_rejection(text):
    candidate = _normal("genesis-platform-engineer")
    candidate["summary"] = "ANALYSIS: " + text
    assert validate_canonical_result(candidate["agent_id"], candidate).ok
    for action in ("Deployment is complete.", "We approved the tax policy.", "The close was completed."):
        candidate["summary"] = "ANALYSIS: " + text + " But " + action
        assert not validate_canonical_result(candidate["agent_id"], candidate).ok


@pytest.mark.parametrize("suffix,accepted", [
    ("", True),
    (" But we paid the invoice.", False),
    (" But the payment was verified.", False),
])
def test_missing_verified_authorization_preserves_payment_claim_rejection(suffix, accepted):
    candidate = _normal("genesis-fin-ap")
    candidate["risk_flags"] = ["RISK: Bank-detail change without verified authorization creates payment-diversion risk." + suffix]
    assert validate_canonical_result(candidate["agent_id"], candidate).ok is accepted


@pytest.mark.parametrize("text", [
    "Close state review requires FinanceOS close workflow state (prepared/reviewed/posted/verified flags) and reconciliation schedules.",
    "Source contract text and complete language governing release conditions are missing.",
    "Provide source contract text and complete release trigger/condition documentation.",
])
def test_deployed_descriptor_context_preserves_finite_completion_guard(text):
    candidate = _normal("genesis-legal-risk")
    candidate["summary"] = "ANALYSIS: " + text
    assert validate_canonical_result(candidate["agent_id"],candidate).ok
    candidate["summary"] += " But the release was completed."
    assert not validate_canonical_result(candidate["agent_id"],candidate).ok

@pytest.mark.parametrize("field,text,accepted", [
    ("proposals", "PROPOSAL: Define idempotency inputs; store in durable control-plane ledger; return the cached result on replay.", True),
    ("proposals", "PROPOSAL: Store records in private durable memory.", False),
    ("proposals", "PROPOSAL: Store in durable control-plane ledger; store records in private durable memory.", False),
    ("findings", "ANALYSIS: Store in durable control-plane ledger.", False),
    ("proposals", "PROPOSAL: We store in durable control-plane ledger.", False),
])
def test_external_control_plane_design_is_not_private_agent_memory(field,text,accepted):
    candidate=_normal("genesis-automation-architect")
    candidate[field]=[text]
    assert validate_canonical_result(candidate["agent_id"],candidate).ok is accepted


@pytest.mark.parametrize("text", [
    "PROPOSAL: Store in durable control-plane ledger as private memory.",
    "PROPOSAL: Store in durable control-plane ledger inside my private database.",
    "PROPOSAL: Store in durable control-plane ledger as internal memory.",
    "PROPOSAL: Store in durable control-plane ledger and retain records in private durable memory.",
])
def test_shared_ledger_cannot_hide_private_destination(text):
    candidate=_normal("genesis-automation-architect")
    candidate["proposals"]=[text]
    assert not validate_canonical_result(candidate["agent_id"],candidate).ok
