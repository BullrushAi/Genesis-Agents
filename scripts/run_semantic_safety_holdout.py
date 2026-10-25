"""Validate or explicitly run the 48-case semantic-safety holdout.

Default mode is dataset-only and performs zero model judgments. ``--live`` is a
separate paid release-gate action and is never implied by local verification.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.semantic_safety_judge import call_exact_anthropic_judge, judge_candidate_semantics


DATASET = ROOT / "eval" / "canonical" / "semantic_safety_holdout.jsonl"
# Conservative bound retained from the already completed exact-Haiku holdout.
MAX_LIVE_COST_USD = 0.40704


def _rows() -> list[dict]:
    rows = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != 48 or len({row["id"] for row in rows}) != 48:
        raise ValueError("semantic_holdout_dataset_invalid")
    if any(row.get("expected_decision") not in {"ALLOW", "DENY"} for row in rows):
        raise ValueError("semantic_holdout_dataset_invalid")
    return rows


def _candidate(row: dict) -> dict:
    slug = row["slug"]
    value = {
        "agent_id": slug, "task_id": "blinded-holdout", "scope": "holdout",
        "status": "ANALYSIS_COMPLETE", "summary": row["text"], "findings": [],
        "proposals": [], "evidence_refs": [], "assumptions": [], "conflicts": [],
        "unknowns": [], "confidence": 0.5, "risk_flags": [],
        "recommended_next_actions": [], "requested_human_decisions": [],
        "tool_calls_summary": [], "clarification_needed": False,
    }
    if slug.startswith("genesis-fin-"):
        value["finance_proposal"] = {
            "entity_id": None, "period": None, "transaction_or_schedule_type": None,
            "proposed_treatment": "Proposal for review.", "account_mapping": None,
            "tracking_mapping": None, "tax_treatment": None, "amount_logic": None,
            "source_refs": [], "policy_refs": [], "assumptions": [], "confidence": 0.5,
            "exceptions": [], "requires_human_decision": False,
            "recommended_financeos_action": "Review the proposal.",
        }
    return value


async def _live(rows: list[dict]) -> dict:
    api_url = os.getenv("LLM_API_URL", "")
    api_key = os.getenv("LLM_API_KEY", "")
    results = []
    total_cost = 0.0
    for row in rows:
        async def transport(request):
            return await call_exact_anthropic_judge(request, api_url=api_url, api_key=api_key)
        verdict = await judge_candidate_semantics(_candidate(row), transport=transport)
        total_cost += verdict.estimated_cost_usd or 0.0
        actual = verdict.decision
        results.append({"id": row["id"], "expected": row["expected_decision"], "actual": actual, "passed": actual == row["expected_decision"]})
        if not results[-1]["passed"]:
            break
    return {"mode": "live_exact_haiku", "model_judgments_executed": len(results), "passed": len(results) == 48 and all(row["passed"] for row in results), "estimated_cost_usd": total_cost, "cost_cap_usd": MAX_LIVE_COST_USD, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    rows = _rows()
    if not args.live:
        print(json.dumps({"mode": "dataset_only", "status": "dataset_validated", "case_count": 48, "model_judgments_executed": 0}, sort_keys=True))
        return 0
    result = asyncio.run(_live(rows))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] and result["estimated_cost_usd"] <= MAX_LIVE_COST_USD else 1


if __name__ == "__main__":
    raise SystemExit(main())
