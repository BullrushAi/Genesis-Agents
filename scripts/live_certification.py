#!/usr/bin/env python3
"""Authenticated exact-SHA production certification for Genesis-Agents.

This is intentionally a production smoke/certification lane, not a unit test.
It proves the deployed revision, auth fail-closed behavior, canonical 19-agent
provider path, finance no-write boundaries, and one durable async job lifecycle.

Required env:
  GENESIS_CERT_GATEWAY_SECRET  legacy gateway secret used only for certification
  GENESIS_CERT_EXPECTED_SHA    exact deployed Git SHA to certify

Optional:
  GENESIS_CERT_BASE_URL        defaults to https://swarmsync-agents.onrender.com
  GENESIS_CERT_OUTPUT          evidence JSON path (default live-certification.json)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

BASE_URL = os.getenv(
    "GENESIS_CERT_BASE_URL", "https://swarmsync-agents.onrender.com"
).rstrip("/")
SECRET = os.getenv("GENESIS_CERT_GATEWAY_SECRET", "").strip()
EXPECTED_SHA = os.getenv("GENESIS_CERT_EXPECTED_SHA", "").strip()
OUTPUT = Path(os.getenv("GENESIS_CERT_OUTPUT", "live-certification.json"))

MASTER_AGENTS = [
    "genesis-knowledge-evidence",
    "genesis-execution-control",
    "genesis-automation-architect",
    "genesis-integration-architect",
    "genesis-platform-engineer",
    "genesis-data-intelligence",
    "genesis-security-identity",
    "genesis-legal-risk",
    "genesis-commercial-growth",
    "genesis-people-operations",
]
FINANCE_AGENTS = [
    "genesis-fin-revenue",
    "genesis-fin-ap",
    "genesis-fin-ar",
    "genesis-fin-reconciliation",
    "genesis-fin-treasury",
    "genesis-fin-close",
    "genesis-fin-fpa",
    "genesis-fin-payroll-tax",
    "genesis-fin-controller",
]
ALL_AGENTS = MASTER_AGENTS + FINANCE_AGENTS
FORMERLY_FAILING_FINANCE = {
    "genesis-fin-close",
    "genesis-fin-controller",
    "genesis-fin-payroll-tax",
    "genesis-fin-revenue",
}
PROHIBITED_FINANCE_TOOL = "finance_run_payroll_batch"
TERMINAL_JOB_STATUSES = {
    "DELIVERED",
    "DELIVERED_WITH_ARTIFACT_WARNING",
    "FAILED",
    "EXPIRED",
    "CANCELLED",
}


class CertificationFailure(RuntimeError):
    pass


def fail(message: str) -> None:
    raise CertificationFailure(message)


def clip_json(value: Any, limit: int = 3000) -> Any:
    raw = json.dumps(value, default=str)
    if len(raw) <= limit:
        return value
    return {"truncated": raw[:limit]}


def expect_status(resp: httpx.Response, expected: int, label: str) -> Any:
    try:
        body = resp.json()
    except Exception:
        body = {"text": resp.text[:2000]}
    if resp.status_code != expected:
        fail(f"{label}: expected HTTP {expected}, got {resp.status_code}: {body}")
    return body


def main() -> int:
    if not SECRET:
        print("GENESIS_CERT_GATEWAY_SECRET is required", file=sys.stderr)
        return 2
    if len(EXPECTED_SHA) != 40:
        print("GENESIS_CERT_EXPECTED_SHA must be the exact 40-character deployed SHA", file=sys.stderr)
        return 2

    evidence: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "expected_sha": EXPECTED_SHA,
        "checks": {},
    }
    headers = {"X-Agent-Gateway-Secret": SECRET}

    try:
        with httpx.Client(timeout=httpx.Timeout(75.0, connect=15.0)) as client:
            # 1) Service identity + exact deployed SHA.
            health = expect_status(client.get(f"{BASE_URL}/health"), 200, "health")
            evidence["checks"]["health"] = clip_json(health)

            worker = expect_status(
                client.get(f"{BASE_URL}/health/worker"), 200, "worker health"
            )
            deployed_sha = str(worker.get("commit") or "")
            if deployed_sha != EXPECTED_SHA:
                fail(
                    "exact-SHA mismatch: "
                    f"expected {EXPECTED_SHA}, deployed {deployed_sha or '<missing>'}"
                )
            if worker.get("separate_worker_required") is True:
                fail(f"worker topology is unresolved: {worker}")
            if worker.get("worker_mode") == "in_process":
                if worker.get("enabled") is not True:
                    fail(f"in-process worker configured but not enabled: {worker}")
                tick_raw = worker.get("last_tick_at")
                if not tick_raw:
                    fail(f"in-process worker has no observed tick: {worker}")
                try:
                    tick_at = datetime.fromisoformat(str(tick_raw).replace("Z", "+00:00"))
                    tick_age = (datetime.now(timezone.utc) - tick_at).total_seconds()
                except Exception as exc:
                    fail(f"in-process worker tick timestamp is invalid: {tick_raw}: {exc}")
                if tick_age < 0 or tick_age > 60:
                    fail(f"in-process worker tick is stale ({tick_age:.1f}s): {worker}")
            evidence["checks"]["worker"] = clip_json(worker)

            # 2) Auth must fail closed, then succeed with the certification secret.
            unauth = client.get(f"{BASE_URL}/auth/whoami")
            expect_status(unauth, 401, "missing-auth rejection")
            bad = client.get(
                f"{BASE_URL}/auth/whoami",
                headers={"X-Agent-Gateway-Secret": "invalid-certification-secret"},
            )
            expect_status(bad, 401, "invalid-auth rejection")
            whoami = expect_status(
                client.get(f"{BASE_URL}/auth/whoami", headers=headers),
                200,
                "positive auth",
            )
            evidence["checks"]["auth"] = {
                "missing_rejected": True,
                "invalid_rejected": True,
                "positive": clip_json(whoami),
            }

            # 3) Live canonical inventory must be exactly the expected 19.
            inventory = expect_status(
                client.get(f"{BASE_URL}/agents"), 200, "agent inventory"
            )
            live_entries = inventory.get("agents")
            if not isinstance(live_entries, list):
                fail(f"/agents returned invalid inventory: {inventory}")
            live_agents = [
                str(entry.get("slug") or "")
                for entry in live_entries
                if isinstance(entry, dict)
            ]
            if (
                len(live_entries) != 19
                or len(live_agents) != 19
                or set(live_agents) != set(ALL_AGENTS)
            ):
                fail(
                    "canonical inventory mismatch: "
                    f"expected={sorted(ALL_AGENTS)} actual={sorted(live_agents)}"
                )
            evidence["checks"]["inventory"] = {
                "count": 19,
                "agents": live_entries,
            }

            # 4) Every finance agent must reject a permanently prohibited money tool
            # before any provider call. This proves the live no-write boundary.
            no_write: dict[str, Any] = {}
            for slug in FINANCE_AGENTS:
                payload = {
                    "task": {
                        "description": "CERTIFICATION_NEGATIVE_CONTROL_ONLY",
                        "constraints": {"allowed_tools": [PROHIBITED_FINANCE_TOOL]},
                    },
                    "mode": "analysis",
                    "skip_internal_escrow": True,
                }
                resp = client.post(
                    f"{BASE_URL}/agents/{slug}/run", headers=headers, json=payload
                )
                body = expect_status(resp, 403, f"{slug} prohibited-tool rejection")
                no_write[slug] = clip_json(body)
            evidence["checks"]["finance_no_write"] = no_write

            # 5) Run the real provider path for all 19 agents. testContext is
            # deliberately false/omitted so this cannot degrade into a persona-only
            # or fake-client certification.
            live_runs: dict[str, Any] = {}
            for slug in ALL_AGENTS:
                payload = {
                    "task": {
                        "description": (
                            "AUDIT_PROBE_ONLY: perform bounded analysis of this "
                            "certification probe. Do not use tools, mutate state, "
                            "contact anyone, or claim external work occurred."
                        ),
                        "constraints": {"allowed_tools": []},
                    },
                    "mode": "analysis",
                    "skip_internal_escrow": True,
                }
                started = time.monotonic()
                resp = client.post(
                    f"{BASE_URL}/agents/{slug}/run", headers=headers, json=payload
                )
                body = expect_status(resp, 200, f"{slug} live provider run")
                elapsed = round(time.monotonic() - started, 3)
                response_text = body.get("response")
                if not isinstance(response_text, str) or not response_text.strip():
                    fail(f"{slug} returned empty/non-string response: {body}")
                live_runs[slug] = {
                    "elapsed_s": elapsed,
                    "formerly_failing_finance": slug in FORMERLY_FAILING_FINANCE,
                    "response_excerpt": response_text[:1200],
                }
            evidence["checks"]["live_19"] = live_runs

            # 6) Durable async lifecycle. The same authenticated principal submits
            # and polls the job. A delivered result proves queue ownership is not
            # accidentally delegated to the suspended legacy Render worker.
            job_payload = {
                "prompt": (
                    "AUDIT_PROBE_ONLY: return bounded analysis. Do not use tools "
                    "or perform external actions."
                ),
                "params": {"constraints": {"allowed_tools": []}},
                "idempotency_key": f"cert-{EXPECTED_SHA[:12]}-{time.time_ns()}",
            }
            submitted = expect_status(
                client.post(
                    f"{BASE_URL}/agents/genesis-execution-control/jobs",
                    headers=headers,
                    json=job_payload,
                ),
                200,
                "job submission",
            )
            job_id = str(submitted.get("job_id") or "")
            if not job_id:
                fail(f"job submission returned no job_id: {submitted}")

            deadline = time.monotonic() + 240
            last_job: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                polled = expect_status(
                    client.get(f"{BASE_URL}/agents/jobs/{job_id}", headers=headers),
                    200,
                    "job poll",
                )
                last_job = polled
                status = str(polled.get("status") or "")
                if status in TERMINAL_JOB_STATUSES:
                    break
                time.sleep(5)
            if not last_job:
                fail("job lifecycle produced no poll result")
            if last_job.get("status") not in {
                "DELIVERED",
                "DELIVERED_WITH_ARTIFACT_WARNING",
            }:
                fail(f"job did not deliver successfully: {last_job}")
            evidence["checks"]["job_lifecycle"] = {
                "job_id": job_id,
                "status": last_job.get("status"),
                "idempotent_hit": submitted.get("idempotent_hit"),
                "result_summary_present": bool(last_job.get("resultSummary")),
            }

        evidence["decision"] = "PASS"
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        OUTPUT.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(evidence, indent=2, sort_keys=True))
        return 0

    except Exception as exc:
        evidence["decision"] = "FAIL"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        OUTPUT.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(evidence, indent=2, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
