"""Run the real bounded Genesis-24 baseline and persist local execution evidence."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical_profiles import CANONICAL_SLUGS
from eval.canonical.datasets import load_cases, load_tool_cases, validate_dataset
from eval.canonical.experiments import (
    ExperimentConfig,
    aggregate_results,
    execute_case,
    failed_record,
    failure_clusters,
    projected_cost_usd,
    repeatability_summary,
    rescore_measured_record,
    select_baseline_cases,
    select_repeatability_cases,
    select_tool_matrix_cases,
    utc_now,
)
from eval.canonical.model_catalog import (
    CANDIDATE_LOGICAL_MODEL,
    JUDGE_LOGICAL_MODEL,
    ExperimentPreflightError,
    fetch_anthropic_model_catalogue,
    fetch_model_catalogue,
    preflight_anthropic_models,
    preflight_models,
)


SAFE_ENV_NAMES = (
    "LLM_API_URL",
    "LLM_API_KEY",
    "ANTHROPIC_API_KEY",
)


def source_fingerprint() -> dict[str, Any]:
    """Hash executable policy/runtime inputs so concurrent edits invalidate a run."""
    import subprocess

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip() or "UNAVAILABLE"
    paths = [
        ROOT / "agent_runtime.py",
        ROOT / "main.py",
        ROOT / "worker.py",
        ROOT / "runtime" / "tool_policy.py",
        ROOT / "tools" / "file_write_tool.py",
        ROOT / "tools" / "sandbox_tool.py",
        ROOT / "proof-artifacts" / "system-inventory" / "system_inventory.json",
        *sorted((ROOT / "skill_bundles").glob("genesis-*.json")),
    ]
    digest = hashlib.sha256()
    file_hashes: dict[str, str] = {}
    for path in paths:
        if not path.is_file():
            continue
        content = path.read_bytes()
        relative = str(path.relative_to(ROOT)).replace("\\", "/")
        file_hash = hashlib.sha256(content).hexdigest()
        file_hashes[relative] = file_hash
        digest.update(relative.encode())
        digest.update(content)
    return {"git_head": head, "content_sha256": digest.hexdigest(), "file_count": len(file_hashes), "files": file_hashes}


def evaluation_logic_fingerprint() -> dict[str, Any]:
    paths = [
        ROOT / "eval" / "canonical" / "datasets.py",
        ROOT / "eval" / "canonical" / "evaluators.py",
        ROOT / "eval" / "canonical" / "experiments.py",
        ROOT / "eval" / "canonical" / "contracts.py",
        ROOT / "eval" / "canonical" / "contract_caller.py",
        ROOT / "eval" / "canonical" / "model_catalog.py",
        ROOT / "runtime" / "anthropic_wire.py",
        ROOT / "scripts" / "run_canonical_experiments.py",
    ]
    digest = hashlib.sha256()
    files: dict[str, str] = {}
    for path in paths:
        content = path.read_bytes()
        relative = str(path.relative_to(ROOT)).replace("\\", "/")
        files[relative] = hashlib.sha256(content).hexdigest()
        digest.update(relative.encode())
        digest.update(content)
    return {"content_sha256": digest.hexdigest(), "files": files}


def dataset_fingerprints() -> dict[str, str]:
    paths = [ROOT / "eval" / "canonical" / "genesis_24.jsonl", ROOT / "eval" / "canonical" / "genesis_tool_matrix.jsonl"]
    return {
        str(path.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def load_safe_env(path: Path) -> dict[str, bool]:
    """Load only authorized experiment settings; return presence, never values."""
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            if name in SAFE_ENV_NAMES and name not in os.environ:
                os.environ[name] = value.strip().strip('"').strip("'")
    return {name: bool(os.getenv(name)) for name in SAFE_ENV_NAMES}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read local responses that may be generated mappings or model objects."""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class ExperimentRunAbort(RuntimeError):
    """Safe cohort abort raised only after completed rows are checkpointed."""


def _checkpoint_row(row: dict[str, Any], cohort_name: str) -> dict[str, Any]:
    """Whitelist non-payload evidence fields for durable abort recovery."""
    attempts = [
        {
            key: attempt.get(key)
            for key in (
                "phase", "attempt", "trace_id", "latency_ms", "error", "transient",
                "usage", "cost_usd", "cost_source",
            )
        }
        for attempt in row.get("contract_attempts", [])
        if isinstance(attempt, dict)
    ]
    return {
        "schema_version": 1,
        "cohort": cohort_name,
        "example_id": row.get("example_id"),
        "agent_slug": row.get("agent_slug"),
        "category": row.get("category"),
        "repetition": row.get("repetition"),
        "trace_id": row.get("trace_id"),
        "critical_pass": row.get("critical_pass") is True,
        "error": row.get("error"),
        "total_latency_ms": float(row.get("total_latency_ms", 0.0)),
        "total_cost_usd": float(row.get("total_cost_usd", 0.0)),
        "step_count": int(row.get("step_count", 0)),
        "retry_count": int(row.get("retry_count", 0)),
        "candidate_usage": row.get("candidate_usage", {}),
        "judge_usage": row.get("judge_usage", {}),
        "contract_attempts": attempts,
        "failed_deterministic_names": [
            str(score.get("name"))
            for score in row.get("deterministic_scores", [])
            if isinstance(score, dict) and score.get("passed") is not True
        ],
        "failed_business_names": [
            str(score.get("name"))
            for score in row.get("business_scores", [])
            if isinstance(score, dict) and score.get("passed") is not True
        ],
        "failed_llm_scores": [
            {"name": str(score.get("name")), "score": float(score.get("score", 0.0))}
            for score in row.get("llm_scores", [])
            if isinstance(score, dict) and score.get("passed") is not True
        ],
    }


def persist_execution_checkpoint(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    status: str,
    reason: str | None = None,
) -> None:
    """Atomically persist redacted rows before an execution guard can abort."""
    payload = {
        "schema_version": 1,
        "status": status,
        "reason": reason,
        "completed_records": len(rows),
        "measured_cost_usd": round(sum(float(row.get("total_cost_usd", 0.0)) for row in rows), 8),
        "rows": rows,
        "credentials_included": False,
        "provider_payloads_included": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


async def run_records(
    cases: list[dict[str, Any]],
    repetitions: int,
    call_model,
    config: ExperimentConfig,
    *,
    checkpoint_path: Path | None = None,
    cohort_name: str = "unspecified",
    post_checkpoint_guard: Callable[[list[dict[str, Any]]], str | None] | None = None,
) -> list[dict[str, Any]]:
    # Targeted cohorts deliberately have empty baseline/tool lanes. Avoid a
    # redundant atomic rewrite of the shared checkpoint, which can collide with
    # Windows indexers immediately after the paid lane committed its row.
    if not cases:
        return []
    semaphore = asyncio.Semaphore(config.concurrency)

    async def one(index: int, case: dict[str, Any], repetition: int) -> tuple[int, dict[str, Any]]:
        async with semaphore:
            try:
                row = await execute_case(
                    case,
                    repetition=repetition,
                    call_model=call_model,
                    config=config,
                )
            except Exception as exc:
                row = failed_record(case, repetition, exc)
            return index, row

    inputs = [(case, repetition) for case in cases for repetition in range(1, repetitions + 1)]
    tasks = [asyncio.create_task(one(index, case, repetition)) for index, (case, repetition) in enumerate(inputs)]
    completed: list[tuple[int, dict[str, Any]]] = []
    completed_indices: set[int] = set()
    checkpoint_rows: list[dict[str, Any]] = []
    if checkpoint_path and checkpoint_path.exists():
        try:
            existing = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint_rows = list(existing.get("rows", [])) if isinstance(existing, dict) else []
        except (OSError, json.JSONDecodeError):
            checkpoint_rows = []
    try:
        for task in asyncio.as_completed(tasks):
            indexed_row = await task
            completed.append(indexed_row)
            completed_indices.add(indexed_row[0])
            checkpoint_rows.append(_checkpoint_row(indexed_row[1], cohort_name))
            if checkpoint_path:
                persist_execution_checkpoint(checkpoint_path, checkpoint_rows, status="running")
            reason = post_checkpoint_guard(checkpoint_rows) if post_checkpoint_guard else None
            if reason:
                # Preserve any rows that completed concurrently before cancelling
                # remaining work; an abort must not strand already-paid evidence.
                for original in tasks:
                    if not original.done() or original.cancelled():
                        continue
                    extra = original.result()
                    if extra[0] in completed_indices:
                        continue
                    completed.append(extra)
                    completed_indices.add(extra[0])
                    checkpoint_rows.append(_checkpoint_row(extra[1], cohort_name))
                if checkpoint_path:
                    persist_execution_checkpoint(checkpoint_path, checkpoint_rows, status="aborted", reason=reason)
                raise ExperimentRunAbort(reason)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    if checkpoint_path:
        persist_execution_checkpoint(checkpoint_path, checkpoint_rows, status="complete")
    return [row for _, row in sorted(completed)]


def build_scorecard(records: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for slug in sorted(CANONICAL_SLUGS):
        agent = [row for row in records if row["agent_slug"] == slug]
        llm = [score for row in agent for score in row.get("llm_scores", [])]
        by_name = {name: [score["score"] for score in llm if score["name"] == name] for name in ("correctness", "tool_use")}
        deterministic = [score for row in agent for score in row.get("deterministic_scores", [])]
        routing = [score["score"] for score in deterministic if score["name"] == "routing"]
        rows.append(
            {
                "agent": slug,
                "tests": len(agent),
                "pass_rate": round(sum(row["critical_pass"] for row in agent) / len(agent), 6) if agent else 0.0,
                "correctness": round(sum(by_name["correctness"]) / len(by_name["correctness"]), 6) if by_name["correctness"] else 0.0,
                "tool_use": round(sum(by_name["tool_use"]) / len(by_name["tool_use"]), 6) if by_name["tool_use"] else 0.0,
                "routing": round(sum(routing) / len(routing), 6) if routing else 0.0,
                "avg_steps": round(sum(row["step_count"] for row in agent) / len(agent), 3) if agent else 0.0,
                "p95_latency_ms": max((row["total_latency_ms"] for row in agent), default=0.0),
                "errors": sum(bool(row.get("error")) for row in agent),
                "status": "PASS" if agent and all(row["critical_pass"] for row in agent) else "FAIL",
            }
        )
    return {"schema_version": 1, "agent_count": len(rows), "agents": rows, "system": aggregate_results(records)}


async def async_main(args: argparse.Namespace) -> int:
    config = ExperimentConfig(
        dataset_name=args.dataset,
        dataset_version=args.dataset_version,
        project_name=args.project,
        experiment_name=args.experiment,
        candidate_model=args.candidate_model,
        judge_model=args.judge_model,
        provider=args.provider,
        concurrency=args.concurrency,
        # The shared full-suite contract historically requires >=2 repeats.
        # A targeted staged canary may intentionally request exactly one.
        repeat_count=max(2, args.repeat_count) if args.target_agent else args.repeat_count,
        spend_cap_usd=args.spend_cap,
        max_contract_retries=args.contract_retries,
        contract_retry_backoff_seconds=args.contract_backoff_seconds,
    )
    config.validate()
    cases = load_cases()
    tool_dataset_cases = load_tool_cases()
    coverage = validate_dataset(cases, CANONICAL_SLUGS)
    baseline_cases = select_baseline_cases(cases)
    repeat_cases = select_repeatability_cases(cases)
    tool_cases = select_tool_matrix_cases(tool_dataset_cases)
    if args.target_agent:
        targeted = [
            case
            for case in cases
            if case["metadata"].get("agent_slug") == args.target_agent
            and case["metadata"].get("category") == args.target_category
        ]
        if len(targeted) != 1:
            print(json.dumps({"error": "target_case_not_unique", "matches": len(targeted)}), file=sys.stderr)
            return 2
        baseline_cases = []
        repeat_cases = targeted
        tool_cases = []
    execution_repeat_count = args.repeat_count if args.target_agent else config.repeat_count
    projected_runs = len(baseline_cases) + len(repeat_cases) * execution_repeat_count + len(tool_cases)
    catalogue_evidence: dict[str, Any] = {"status": "not_run_dry_run"}
    if not args.dry_run:
        presence = load_safe_env(args.env_file)
        required_names = (
            ("ANTHROPIC_API_KEY")
            if args.provider == "anthropic"
            else ("LLM_API_URL", "LLM_API_KEY")
        )
        if not all(presence[name] for name in required_names):
            _write_json(args.output_dir / "failure_clusters.json", {"schema_version": 1, "clusters": [{"root_cause": "configuration_missing", "presence": presence}]})
            print('{"error":"required_configuration_missing"}', file=sys.stderr)
            return 2
        try:
            if args.provider == "anthropic":
                catalogue = await fetch_anthropic_model_catalogue(
                    "https://api.anthropic.com", os.environ["ANTHROPIC_API_KEY"]
                )
                checked = preflight_anthropic_models(
                    catalogue, config.candidate_model, config.judge_model
                )
            else:
                catalogue = await fetch_model_catalogue(os.environ["LLM_API_URL"], os.environ["LLM_API_KEY"])
                checked = preflight_models(catalogue, config.candidate_model, config.judge_model)
            catalogue_evidence = {"status": "passed", **checked}
        except ExperimentPreflightError as exc:
            print(json.dumps({"error": str(exc), "stage": "model_catalogue_preflight"}), file=sys.stderr)
            return 2
    projected = projected_cost_usd(projected_runs, config)
    if projected > config.spend_cap_usd:
        print(json.dumps({"error": "projected_spend_exceeds_cap", "projected_cost_usd": projected}), file=sys.stderr)
        return 2
    artifact_dir = args.output_dir
    prior_retry_attempts: list[dict[str, Any]] = []
    if args.resume and (artifact_dir / "baseline_results.json").exists():
        prior_doc = json.loads((artifact_dir / "baseline_results.json").read_text(encoding="utf-8"))
        prior_ledger = prior_doc.get("fingerprint_ledger", {})
        prior_retry_attempts = list(prior_ledger.get("retry_attempts", []))
        if not prior_retry_attempts and prior_ledger.get("retry", {}).get("status") == "stable":
            recovered = dict(prior_ledger["retry"])
            recovered["attempt"] = 1
            recovered["attempted_call_count"] = 5
            recovered["note"] = "first bounded resume of the five errors in the immutable 86-record run"
            prior_retry_attempts.append(recovered)
    public_config = {
        **config.public_dict(),
        "dataset_coverage": coverage,
        "baseline_case_ids": [case["example_id"] for case in baseline_cases],
        "repeatability_case_ids": [case["example_id"] for case in repeat_cases],
        "tool_matrix_case_ids": [case["example_id"] for case in tool_cases],
        "projected_execution_count": projected_runs,
        "projected_cost_upper_bound_usd": projected,
        "synthetic_only": True,
        "tool_writes_allowed": False,
        "created_at": utc_now(),
        "model_catalogue_preflight": catalogue_evidence,
        "provider": args.provider,
        "target_agent": args.target_agent,
        "target_category": args.target_category if args.target_agent else None,
        "execution_repeat_count": execution_repeat_count,
        "source_fingerprint_start": source_fingerprint(),
    }
    raw_runtime = public_config["source_fingerprint_start"]
    logic_start = evaluation_logic_fingerprint()
    fingerprints = {
        "raw_execution": {
            "runtime": raw_runtime,
            "datasets": dataset_fingerprints(),
            "prompt_version": config.prompt_version,
            "candidate_model": config.candidate_model,
            "judge_model": config.judge_model,
            "original_86_runtime_unchanged": True,
            "original_evaluator_logic_fingerprint": "NOT_CAPTURED_BEFORE_POSTFIX_RESCORE",
        },
        "postfix_rescore": {"evaluation_logic": logic_start, "method": "recomputed only from recorded events/tools/scores; no model output synthesized"},
        "retry": {"runtime": raw_runtime, "evaluation_logic": logic_start, "status": "pending"},
        "retry_attempts": prior_retry_attempts,
    }
    public_config["fingerprint_ledger"] = fingerprints
    _write_json(artifact_dir / "experiment_config.json", public_config)
    if args.dry_run:
        print(json.dumps({"status": "validated", "runs": projected_runs, "projected_cost_usd": projected, **coverage}, sort_keys=True))
        return 0

    from eval.canonical.contract_caller import ContractModelCaller

    if args.provider == "anthropic":
        call_model = ContractModelCaller(
            "https://api.anthropic.com",
            os.environ["ANTHROPIC_API_KEY"],
            provider="anthropic",
        )
    else:
        call_model = ContractModelCaller(os.environ["LLM_API_URL"], os.environ["LLM_API_KEY"])

    checkpoint_path = artifact_dir / "execution_checkpoint.json"
    persist_execution_checkpoint(checkpoint_path, [], status="running")
    guard_expected_records = [projected_runs]

    def execution_guard(rows: list[dict[str, Any]]) -> str | None:
        if source_fingerprint() != public_config["source_fingerprint_start"] or evaluation_logic_fingerprint() != logic_start:
            return "fingerprint_changed"
        measured = sum(float(row.get("total_cost_usd", 0.0)) for row in rows)
        expected = max(len(rows), guard_expected_records[0])
        remaining = max(0, expected - len(rows))
        per_record_projection = projected / projected_runs if projected_runs else 0.0
        if measured + remaining * per_record_projection > config.spend_cap_usd:
            return "rolling_spend_projection_exceeded"
        return None

    async def guarded_records(selected, repetitions, cohort_name):
        return await run_records(
            selected,
            repetitions,
            call_model,
            config,
            checkpoint_path=checkpoint_path,
            cohort_name=cohort_name,
            post_checkpoint_guard=execution_guard,
        )

    try:
        if args.resume:
            baseline_doc = json.loads((artifact_dir / "baseline_results.json").read_text(encoding="utf-8"))
            repeat_doc = json.loads((artifact_dir / "repeatability_results.json").read_text(encoding="utf-8"))
            baseline = baseline_doc["results"]
            tool_matrix = baseline_doc["tool_matrix_results"]
            repeats = repeat_doc["results"]
            case_index = {case["example_id"]: case for case in [*cases, *tool_dataset_cases]}
            attempted_call_count = sum(
                bool(row.get("error")) for rows in (baseline, tool_matrix, repeats) for row in rows
            )
            guard_expected_records[0] = attempted_call_count
            for cohort_name, rows in (("baseline_resume", baseline), ("tool_matrix_resume", tool_matrix), ("repeat_resume", repeats)):
                failures = [row for row in rows if row.get("error")]
                replacements = []
                for failed in failures:
                    replacements.extend(await guarded_records([case_index[failed["example_id"]]], 1, cohort_name))
                    replacements[-1]["repetition"] = failed["repetition"]
                replacement_index = {(row["example_id"], row["repetition"]): row for row in replacements}
                rows[:] = [replacement_index.get((row["example_id"], row["repetition"]), row) for row in rows]
        else:
            attempted_call_count = projected_runs
            baseline = await guarded_records(baseline_cases, 1, "baseline")
            repeats = await guarded_records(repeat_cases, execution_repeat_count, "repeatability")
            tool_matrix = await guarded_records(tool_cases, 1, "tool_matrix")
    except ExperimentRunAbort as exc:
        print(json.dumps({"status": "aborted", "reason": str(exc), "checkpoint": str(checkpoint_path)}))
        return 4
    try:
        if args.provider == "anthropic":
            catalogue_end = await fetch_anthropic_model_catalogue(
                "https://api.anthropic.com", os.environ["ANTHROPIC_API_KEY"]
            )
        else:
            catalogue_end = await fetch_model_catalogue(os.environ["LLM_API_URL"], os.environ["LLM_API_KEY"])
    except ExperimentPreflightError:
        persist_execution_checkpoint(checkpoint_path, [*baseline, *repeats, *tool_matrix], status="aborted", reason="model_catalogue_recheck_failed")
        print(json.dumps({"status": "aborted", "reason": "model_catalogue_recheck_failed", "checkpoint": str(checkpoint_path)}))
        return 4
    if catalogue_end.sha256 != catalogue.sha256:
        persist_execution_checkpoint(checkpoint_path, [*baseline, *repeats, *tool_matrix], status="aborted", reason="model_catalogue_changed")
        print(json.dumps({"status": "aborted", "reason": "model_catalogue_changed", "checkpoint": str(checkpoint_path)}))
        return 4
    baseline = [rescore_measured_record(row) for row in baseline]
    repeats = [rescore_measured_record(row) for row in repeats]
    tool_matrix = [rescore_measured_record(row) for row in tool_matrix]
    all_records = [*baseline, *repeats, *tool_matrix]
    fingerprint_end = source_fingerprint()
    logic_end = evaluation_logic_fingerprint()
    contaminated = fingerprint_end != public_config["source_fingerprint_start"]
    logic_contaminated = logic_end != logic_start
    fingerprints["retry"]["runtime_end"] = fingerprint_end
    fingerprints["retry"]["evaluation_logic_end"] = logic_end
    fingerprints["retry"]["status"] = "stable" if not contaminated and not logic_contaminated else "CONTAMINATED"
    current_retry = dict(fingerprints["retry"])
    current_retry["attempt"] = len(prior_retry_attempts) + 1
    current_retry["attempted_call_count"] = attempted_call_count
    fingerprints["retry_attempts"] = [*prior_retry_attempts, current_retry]
    _write_json(artifact_dir / "experiment_config.json", public_config)
    actual_cost = sum(row["total_cost_usd"] for row in all_records)
    if actual_cost > config.spend_cap_usd:
        raise RuntimeError("measured_spend_exceeded_cap")

    _write_json(artifact_dir / "baseline_results.json", {"schema_version": 1, "fingerprint_ledger": fingerprints, "summary": aggregate_results(baseline), "results": baseline, "tool_matrix_results": tool_matrix})
    repeat_artifact = repeatability_summary(repeats)
    repeat_artifact["fingerprint_ledger"] = fingerprints
    _write_json(artifact_dir / "repeatability_results.json", repeat_artifact)
    scorecard = build_scorecard(all_records)
    scorecard["fingerprint_ledger"] = fingerprints
    _write_json(artifact_dir / "scorecard_initial.json", scorecard)
    _write_json(artifact_dir / "cost_latency_summary.json", {"schema_version": 1, "fingerprint_ledger": fingerprints, "projected_cost_upper_bound_usd": projected, "measured": aggregate_results(all_records), "source_fingerprint_end": fingerprint_end, "contaminated": contaminated, "evaluation_logic_contaminated": logic_contaminated})
    clusters = failure_clusters(all_records)
    clusters["fingerprint_ledger"] = fingerprints
    _write_json(artifact_dir / "failure_clusters.json", clusters)

    if contaminated or logic_contaminated:
        print(json.dumps({"status": "contaminated", "executions": len(all_records), "measured_cost_usd": round(actual_cost, 8)}))
        return 3

    critical_ok = all(row["critical_pass"] for row in all_records)
    complete_roster = (
        all(row["agent_slug"] == args.target_agent for row in all_records)
        if args.target_agent
        else {row["agent_slug"] for row in baseline} == set(CANONICAL_SLUGS)
    )
    summary = {
        "status": "passed" if critical_ok and complete_roster else "failed",
        "executions": len(all_records),
        "baseline_agents": len({row["agent_slug"] for row in all_records}),
        "critical_passes": sum(row["critical_pass"] for row in all_records),
        "errors": sum(bool(row.get("error")) for row in all_records),
        "measured_cost_usd": round(actual_cost, 8),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["status"] == "passed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "proof-artifacts" / "experiment-runner")
    parser.add_argument("--dataset", default="genesis-24-canonical")
    parser.add_argument("--dataset-version", default="v1")
    parser.add_argument("--project", default="genesis-24-experiments")
    parser.add_argument("--experiment", default="baseline-2026-08-25")
    parser.add_argument("--candidate-model", default=CANDIDATE_LOGICAL_MODEL)
    parser.add_argument(
        "--judge-model",
        default=JUDGE_LOGICAL_MODEL,
        help="Immutable Haiku judge role; its distinct tool/schema/policy/hash binding is preflighted.",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--repeat-count", type=int, default=5)
    parser.add_argument("--spend-cap", type=float, default=10.0)
    parser.add_argument("--contract-retries", type=int, default=0)
    parser.add_argument("--contract-backoff-seconds", type=float, default=0.25)
    parser.add_argument("--provider", choices=("openai", "anthropic"), default="anthropic")
    parser.add_argument("--target-agent", help="Run only one exact agent/category cohort.")
    parser.add_argument("--target-category", default="happy")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Retry only failed records, then finish local result receipts.")
    return asyncio.run(async_main(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
