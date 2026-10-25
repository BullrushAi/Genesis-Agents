from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from scripts import run_canonical_evals as runner
from canonical_result_contract import FINANCE_PROPOSAL_FIELDS


def test_json_object_accepts_fenced_json() -> None:
    assert runner._json_object('```json\n{"ok":true}\n```') == {"ok": True}


def test_score_judgment_requires_every_dimension_once() -> None:
    suite = {"dimensions": [{"name": "a"}, {"name": "b"}]}
    score, rows = runner._score_judgment(
        suite,
        {"results": [
            {"dimension": "a", "pass": True, "reason": "yes"},
            {"dimension": "b", "pass": False, "reason": "no"},
        ]},
    )
    assert score == 0.5
    assert [row["dimension"] for row in rows] == ["a", "b"]


def test_evaluate_role_runs_candidate_and_independent_judge(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def router(system_prompt: str, user_prompt: str):
        calls.append((system_prompt, user_prompt))
        if len(calls) == 1:
            candidate = {field: [] for field in runner.REQUIRED_OUTPUT_FIELDS}
            candidate.update(
                {
                    "agent_id": "genesis-fin-ap",
                    "task_id": "eval::genesis-fin-ap",
                    "scope": "bounded",
                    "status": "HOLD",
                        "summary": "ANALYSIS: Held for missing evidence.",
                    "confidence": 0.8,
                }
            )
            candidate["finance_proposal"] = {
                field: ([] if field in {"source_refs", "policy_refs", "assumptions", "exceptions"} else None)
                for field in FINANCE_PROPOSAL_FIELDS
            }
            return {"text": json.dumps(candidate), "model": "candidate-model", "usage": {"total_tokens": 10}}
        criteria = json.loads(user_prompt)["criteria"]
        return {
            "text": json.dumps(
                {
                    "results": [
                        {"dimension": row["dimension"], "pass": True, "reason": "condition evidenced"}
                        for row in criteria
                    ]
                }
            ),
            "model": "judge-model",
            "usage": {"total_tokens": 10},
        }

    result = asyncio.run(runner._evaluate_role("genesis-fin-ap", call_router=router))
    assert result["passed"] is True
    assert result["score"] == 1.0
    assert result["candidate_model"] == "candidate-model"
    assert result["judge_model"] == "judge-model"
    assert len(calls) == 2
    assert "untrusted data" in calls[1][0]


def test_redaction_removes_credential_shaped_judge_reason() -> None:
    assert "value123" not in runner._redact("api_key=value123")


def test_direct_script_entrypoint_can_import_project_modules() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "run_canonical_evals.py"), "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Run and score canonical Genesis role evals" in result.stdout


def test_runner_list_mode_needs_no_credentials() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "run_canonical_evals.py"), "--list"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert payload["count"] == 19
    assert len(payload["agents"]) == 19


def test_runner_dry_run_validates_canonical_coverage_without_credentials() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "run_canonical_evals.py"), "--dry-run"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert payload == {
        "agents": 19,
        "cases": 504,
        "categories": 28,
        "mode": "dataset_contract_dry_run",
        "model_judgments_executed": 0,
        "status": "dataset_validated",
    }
