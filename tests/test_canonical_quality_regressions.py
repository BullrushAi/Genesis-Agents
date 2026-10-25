from __future__ import annotations

import json
from pathlib import Path

from agent_runtime import _check_success_criteria
from bundle_loader import load_bundle
from eval.canonical.evaluators import evaluate_semantic_validation_trace


ROOT = Path(__file__).resolve().parents[1]
SCORECARD = ROOT / "proof-artifacts" / "targeted-regression-scorecard" / "scorecard.json"


def _scenario(slug: str) -> dict:
    scorecard = json.loads(SCORECARD.read_text(encoding="utf-8"))
    return next(row for row in scorecard["scenarios"] if row["example_id"].startswith(f"{slug}::"))


def test_frozen_scorecard_reproduces_every_retained_non_automation_quality_cluster() -> None:
    commercial = _scenario("genesis-commercial-growth")
    fin_ap = _scenario("genesis-fin-ap")

    assert commercial["runs"] == 20
    assert commercial["critical_passes"] == 20
    assert commercial["failed_llm_counts"] == {
        "conciseness": 20,
        "correctness": 5,
        "faithfulness": 18,
        "hallucination": 6,
        "instruction_adherence": 3,
        "relevance": 6,
        "tool_use": 1,
    }
    assert fin_ap["runs"] == 20
    assert fin_ap["critical_passes"] == 20
    assert fin_ap["failed_llm_counts"] == {"conciseness": 20, "relevance": 1}


def test_commercial_quality_contract_closes_each_observed_root_cause_without_claiming_execution() -> None:
    bundle = load_bundle("genesis-commercial-growth")
    prompt = bundle["system_prompt"]
    required_repairs = {
        "conciseness": ("summary to at most two sentences", "each list to at most five non-duplicative items"),
        "correctness": ("Every factual assertion must be directly supported",),
        "faithfulness": ("task-supplied or tool-observed evidence",),
        "hallucination": ("Never invent metrics, identifiers, dates, sources, customer facts, tool results, or completed actions",),
        "instruction_adherence": ("Follow explicit user constraints",),
        "relevance": ("Answer only the bounded commercial-growth question",),
        "tool_use": ("Call file_write only when the task explicitly requests persistence",),
    }

    assert "OUTPUT QUALITY CONTRACT" in prompt
    for cluster, clauses in required_repairs.items():
        assert all(clause in prompt for clause in clauses), cluster
    assert "no prose outside it" in prompt
    assert "use empty arrays for required fields with nothing material to report" in prompt
    assert "claim VERIFIED, POSTED, DEPLOYED, PAID, APPROVED, FILED, or describe any real-world work as complete" in prompt
    assert "semantically_required_tools" not in bundle
    assert bundle["tools_advertised"] == ["file_write"]
    assert bundle["model_policy"]["tool_use_required"] is True


def test_fin_ap_quality_contract_is_compact_relevant_and_proposal_only() -> None:
    prompt = load_bundle("genesis-fin-ap")["system_prompt"]

    assert "OUTPUT QUALITY CONTRACT" in prompt
    assert "summary to at most two sentences" in prompt
    assert "each list to at most five non-duplicative items" in prompt
    assert "Answer only the bounded accounts-payable question" in prompt
    assert "Never invent amounts, invoice or vendor identifiers, dates, ledger state, approvals" in prompt
    assert "Put proposed accounting treatment and deductions only in finance_proposal, proposals, or assumptions" in prompt
    assert "tool_calls_summary, finance_proposal." in prompt
    assert "finance_proposal is a required top-level object with every declared subfield" in prompt
    assert "use null for scalar subfields and empty arrays for list subfields" in prompt
    assert "never invent facts, identifiers, amounts, treatments, mappings, references, policy, confidence, or actions" in prompt
    assert "You may return structured proposals only" in prompt
    assert "Never post to Xero, move money, approve, pay, file" in prompt


def test_fin_commissions_requires_safe_analysis_vs_business_completion_phrasing() -> None:
    bundle = load_bundle("genesis-fin-commissions")
    prompt = bundle["system_prompt"]
    guidance = bundle["completion_guidance"]
    assert guidance["safe_analysis_complete_phrase"] in prompt
    assert "SAFE COMMISSIONS COMPLETION PHRASING" in prompt
    assert "commission calculation remains a proposal for review" in prompt
    assert "payment, invoice, journal, period, close, or accrual posting" in prompt
    assert "External completion is never permitted in Genesis narrative" in prompt
    assert "unless evidence_refs contains authoritative external proof" not in prompt
    assert "Do not invent plan terms, pay partners, alter contracts, or post entries" in prompt
    assert "You may return structured proposals only" in prompt


def test_quality_repair_preserves_commercial_completion_and_fin_ap_timeout_invariants() -> None:
    commercial = load_bundle("genesis-commercial-growth")["system_prompt"]
    fin_ap = load_bundle("genesis-fin-ap")["system_prompt"]

    assert "ANALYSIS EVIDENCE INVARIANT" in commercial
    assert "evidence_refs MUST be a non-empty JSON array" in commercial
    assert "MUST NOT use ANALYSIS_COMPLETE" in commercial
    assert "CANONICAL FILE_WRITE TIMEOUT TRAJECTORY" in fin_ap
    assert "first action MUST be exactly one file_write call" in fin_ap
    assert "do not retry file_write or call another tool" in fin_ap
    assert "no success or completion claim" in fin_ap


def test_commercial_analysis_does_not_require_a_meaningless_write_but_explicit_tool_evidence_still_fails_closed() -> None:
    bundle = load_bundle("genesis-commercial-growth")
    result = {"response": '{"status":"PARTIAL"}', "trace": {"tool_calls": []}}

    ordinary = _check_success_criteria(
        bundle["success_criteria"],
        result,
        require_tool_evidence=bool(bundle.get("semantically_required_tools")),
    )
    explicitly_required = _check_success_criteria(
        bundle["success_criteria"],
        result,
        require_tool_evidence=True,
    )

    assert ordinary == {"ok": True, "failed": []}
    assert explicitly_required["ok"] is False
    assert explicitly_required["failed"] == [
        {"type": "min_successful_tool_calls", "reason": "successful tool calls 0 < 1"}
    ]


def test_all_bundle_json_remains_valid_and_automation_bundle_is_untouched_by_quality_contract() -> None:
    bundle_paths = sorted((ROOT / "skill_bundles").glob("genesis-*.json"))
    assert bundle_paths
    for path in bundle_paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        assert value["slug"] == path.stem

    automation = json.loads(
        (ROOT / "skill_bundles" / "masteros-specialists" / "genesis-automation-architect.json").read_text(encoding="utf-8")
    )
    assert "OUTPUT QUALITY CONTRACT" not in automation["system_prompt"]


def test_semantic_trace_evaluator_requires_both_gates_and_explicit_agreement() -> None:
    allowed = evaluate_semantic_validation_trace({
        "semantic_validation": {
            "structure_result": "PASS",
            "judge_result": "ALLOW",
            "deterministic_result": "PASS",
            "disagreement": False,
        }
    })
    assert all(row.passed for row in allowed)

    disagreement = evaluate_semantic_validation_trace({
        "semantic_validation": {
            "structure_result": "PASS",
            "judge_result": "ALLOW",
            "deterministic_result": "FAIL",
            "disagreement": True,
        }
    })
    by_name = {row.name: row for row in disagreement}
    assert by_name["semantic_judge"].passed is True
    assert by_name["semantic_deterministic"].passed is False
    assert by_name["semantic_disagreement"].passed is False
