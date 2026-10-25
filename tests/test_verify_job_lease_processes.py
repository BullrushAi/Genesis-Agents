"""Lease handoff between REAL gateway processes (real PostgreSQL, GENESIS_TEST_DATABASE_URL).

test_verify_job_leases.py proves the lease logic in one process. Render cannot be made to kill a gateway mid-job, so
this file proves it with two actual uvicorn subprocesses serving main:app against the same database:

  * hard kill  : A is killed (SIGKILL / TerminateProcess, no shutdown handlers) mid-job; B takes the SAME job over once
                 A's lease expires and delivers exactly one callback.
  * graceful   : A gets SIGTERM (CTRL_BREAK on Windows); A releases its lease at once and B takes over before the lease
                 would have expired.

Only the network boundary is faked: ``main.run_verification`` (sleeps FAKE_RUN_SECONDS, then passes) and the SwarmSync
callback endpoint (a local HTTP server that counts callbacks per verification_id). Leases, heartbeat, recovery loop
and callback delivery are the real code, with short timings: lease 6s, recovery tick 2s, heartbeat 2s.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from runtime import pg_store
from verification_models import (
    VerificationContext,
    VerificationSpec,
    VerifyRequest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
IS_WINDOWS = sys.platform == "win32"

LEASE_SECONDS = 6
RECOVERY_SECONDS = 2
FAKE_RUN_SECONDS = 25
INTERNAL = "proc-internal-secret-0123456789"
GATEWAY = "proc-gateway-secret-0123456789"

WRAPPER = textwrap.dedent(
    """
    import asyncio, logging, os, pathlib, sys, time

    sys.path.insert(0, os.environ["REPO_ROOT"])
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(name)s %(message)s")

    import uvicorn
    import main
    import verification_job_store as store
    from verification_models import VerificationResult

    name, port, out = sys.argv[1], int(sys.argv[2]), pathlib.Path(sys.argv[3])
    (out / f"owner-{name}.txt").write_text(store.OWNER_ID)


    def note(line):
        with open(out / f"runs-{name}.log", "a", encoding="utf8") as handle:
            handle.write(f"{time.time():.3f} {line}\\n")


    async def fake_run_verification(session_id, spec, context):
        note(f"start {context.verification_id}")
        await asyncio.sleep(float(os.environ["FAKE_RUN_SECONDS"]))
        note(f"finish {context.verification_id}")
        return VerificationResult(passed=True, proof_hash="ab" * 32, conduit_session_sig="cd" * 32, extracted_content="ok")


    main.run_verification = fake_run_verification
    main._VERIFY_HEARTBEAT_INTERVAL_S = float(os.environ["HEARTBEAT_SECONDS"])
    uvicorn.run(main.app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    """
)


# ----------------------------------------------------------------------------------------------- harness


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeSwarmSync:
    """Stands in for SwarmSync's POST /conduit/verifications/{id}/callback and counts callbacks per verification."""

    def __init__(self):
        self.callbacks: dict[str, list[dict]] = defaultdict(list)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                parts = self.path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["conduit", "verifications"] and parts[3] == "callback":
                    outer.callbacks[parts[2]].append(
                        {"body": body, "secret": self.headers.get("X-Internal-Secret")}
                    )
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class GatewayProcess:
    def __init__(self, name: str, tmp_path: Path, env: dict[str, str], script: Path):
        self.name = name
        self.port = _free_port()
        self.out = tmp_path
        self.log_path = tmp_path / f"gateway-{name}.log"
        self._log = open(self.log_path, "wb")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
        self.proc = subprocess.Popen(
            [sys.executable, str(script), name, str(self.port), str(tmp_path)],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )

    @property
    def owner_id(self) -> str:
        return (self.out / f"owner-{self.name}.txt").read_text()

    def log(self) -> str:
        self._log.flush()
        return self.log_path.read_text(encoding="utf8", errors="replace")

    def runs(self) -> list[str]:
        path = self.out / f"runs-{self.name}.log"
        return path.read_text(encoding="utf8").splitlines() if path.exists() else []

    def wait_ready(self, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                pytest.fail(f"gateway {self.name} exited early with {self.proc.returncode}:\n{self.log()[-3000:]}")
            try:
                if httpx.get(f"http://127.0.0.1:{self.port}/health", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        pytest.fail(f"gateway {self.name} not ready after {timeout}s:\n{self.log()[-3000:]}")

    def post_verify(self, verification_id: str) -> httpx.Response:
        body = VerifyRequest(
            spec=VerificationSpec(inline_content="audit done"),
            context=VerificationContext(escrow_ref="esc-proc", verification_id=verification_id),
        ).model_dump()
        return httpx.post(
            f"http://127.0.0.1:{self.port}/verify", json=body, headers={"X-Internal-Secret": INTERNAL}, timeout=15
        )

    def hard_kill(self) -> None:
        """SIGKILL / TerminateProcess: no shutdown handlers run."""
        self.proc.kill()
        self.proc.wait(timeout=20)

    def terminate_gracefully(self) -> None:
        if IS_WINDOWS:
            self.proc.send_signal(signal.CTRL_BREAK_EVENT)  # uvicorn handles SIGBREAK like SIGTERM
        else:
            self.proc.send_signal(signal.SIGTERM)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=20)
        self._log.close()


def wait_until(predicate, what: str, timeout: float, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    pytest.fail(f"timed out after {timeout}s waiting for: {what}")


def job_rows(verification_id: str) -> list[dict]:
    with pg_store.transaction() as cur:
        cur.execute(
            "SELECT job_id, verification_id, status, callback_state, owner_id, "
            "(lease_expires_at <= now()) AS lease_ended FROM genesis_verification_jobs WHERE verification_id = %s",
            (verification_id,),
        )
        return list(cur.fetchall())


@pytest.fixture
def fleet(migrated_postgres_url, tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_JOB_DATABASE_URL", migrated_postgres_url)
    monkeypatch.setenv("GENESIS_STORE_BACKEND", "postgres")
    with pg_store.transaction() as cur:
        cur.execute("TRUNCATE genesis_verification_jobs")

    swarmsync = FakeSwarmSync()
    script = tmp_path / "gateway_wrapper.py"
    script.write_text(WRAPPER, encoding="utf8")
    env = dict(os.environ)
    env.update(
        {
            "REPO_ROOT": str(REPO_ROOT),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GENESIS_JOB_DATABASE_URL": migrated_postgres_url,
            "GENESIS_STORE_BACKEND": "postgres",
            "GATEWAY_API_KEY": "proc-gateway-api-key-0123456789",
            "GENESIS_PRINCIPAL_TOKEN_KEY": "p" * 40,
            "GENESIS_ACTION_GRANT_KEY": "g" * 40,
            "AGENT_GATEWAY_SECRET": "unused-default-gateway-secret-0123",
            "INTERNAL_SECRET": "unused-default-internal-secret-0123",
            "SWARMSYNC_ENVIRONMENTS": json.dumps(
                [{"name": "proc-test", "api_url": swarmsync.url, "gateway_secret": GATEWAY, "internal_secret": INTERNAL}]
            ),
            "GENESIS_VERIFY_LEASE_SECONDS": str(LEASE_SECONDS),
            "GENESIS_VERIFY_RECOVERY_INTERVAL_SECONDS": str(RECOVERY_SECONDS),
            "HEARTBEAT_SECONDS": "2",
            "FAKE_RUN_SECONDS": str(FAKE_RUN_SECONDS),
            "GENESIS_WORKER_ENABLED": "false",
        }
    )
    started: list[GatewayProcess] = []

    def launch(name: str) -> GatewayProcess:
        gateway = GatewayProcess(name, tmp_path, env, script)
        started.append(gateway)
        gateway.wait_ready()
        return gateway

    yield launch, swarmsync
    for gateway in started:
        gateway.stop()
    swarmsync.close()
    with pg_store.transaction() as cur:
        cur.execute("TRUNCATE genesis_verification_jobs")


def start_job_on_a(launch):
    a = launch("A")
    vid = f"ver-proc-{uuid.uuid4().hex[:8]}"
    posted = a.post_verify(vid)
    assert posted.status_code == 202, posted.text
    job_id = posted.json()["jobId"]
    wait_until(
        lambda: any(r["status"] == "running" and r["owner_id"] == a.owner_id for r in job_rows(vid)),
        "job running and owned by A",
        timeout=30,
    )
    wait_until(lambda: len(a.runs()) == 1, "A's fake verification to start", timeout=10)
    return a, vid, job_id


def assert_b_stays_out_while_a_holds_the_lease(a, b, vid, job_id):
    """B overlaps A (a rolling restart). Over several recovery ticks B must not touch the job."""
    wait_until(lambda: b.log().count("skipping job") >= 2, "B to skip the leased job over >= 2 recovery ticks", timeout=20)
    rows = job_rows(vid)
    assert len(rows) == 1 and rows[0]["job_id"] == job_id
    assert rows[0]["owner_id"] == a.owner_id, "B must not take a job while A's lease is live"
    assert b.runs() == [] and len(a.runs()) == 1, "exactly one execution while A holds the lease"


def assert_b_completed_it_exactly_once(a, b, vid, job_id, swarmsync):
    rows = wait_until(
        lambda: (lambda r: r if r and r[0]["status"] == "completed" and r[0]["callback_state"] == "delivered" else None)(
            job_rows(vid)
        ),
        "B to complete the job and deliver the callback",
        timeout=FAKE_RUN_SECONDS + 40,
    )
    assert len(rows) == 1
    assert rows[0]["job_id"] == job_id, "B took over the SAME job"
    assert rows[0]["owner_id"] == b.owner_id
    assert [line.split()[1] for line in b.runs()] == ["start", "finish"], f"B ran it once, fully: {b.runs()}"
    assert [line.split()[1] for line in a.runs()] == ["start"], "A only got part-way"
    assert len(swarmsync.callbacks[vid]) == 1
    callback = swarmsync.callbacks[vid][0]
    assert callback["body"]["passed"] is True and callback["secret"] == INTERNAL
    # a few more recovery ticks: nothing else may ever run or be sent
    time.sleep(RECOVERY_SECONDS * 3 + 1)
    assert len(swarmsync.callbacks[vid]) == 1, "no second callback, ever"
    assert len(b.runs()) == 2 and len(job_rows(vid)) == 1


# ----------------------------------------------------------------------------------------------- tests


def test_hard_kill_of_the_owner_is_taken_over_by_the_other_gateway_after_the_lease_expires(fleet):
    launch, swarmsync = fleet
    a, vid, job_id = start_job_on_a(launch)

    b = launch("B")
    assert_b_stays_out_while_a_holds_the_lease(a, b, vid, job_id)

    a.hard_kill()  # no shutdown handler runs, so the lease is NOT released
    killed_at = time.monotonic()
    row = job_rows(vid)[0]
    assert row["owner_id"] == a.owner_id and row["status"] == "running"
    assert row["lease_ended"] is False, "a crashed owner leaves its lease to run out"

    wait_until(lambda: job_rows(vid)[0]["owner_id"] == b.owner_id, "B to take over after the lease expires", timeout=LEASE_SECONDS + 15)
    assert time.monotonic() - killed_at >= 1.0
    assert_b_completed_it_exactly_once(a, b, vid, job_id, swarmsync)


@pytest.mark.skipif(
    IS_WINDOWS and not hasattr(signal, "CTRL_BREAK_EVENT"),
    reason="graceful shutdown needs SIGTERM or CTRL_BREAK_EVENT",
)
def test_graceful_shutdown_of_the_owner_hands_the_job_over_before_the_lease_would_expire(fleet):
    launch, swarmsync = fleet
    a, vid, job_id = start_job_on_a(launch)

    b = launch("B")
    assert_b_stays_out_while_a_holds_the_lease(a, b, vid, job_id)

    a.terminate_gracefully()
    a.proc.wait(timeout=30)
    exited_at = time.monotonic()
    assert "Released 1 verification job lease" in a.log(), f"A did not release its lease on shutdown:\n{a.log()[-2000:]}"

    row = job_rows(vid)[0]
    assert row["status"] == "running", "a cancelled run stores no result"
    assert row["lease_ended"] is True or row["owner_id"] == b.owner_id, "the lease was released, not left to expire"

    wait_until(lambda: job_rows(vid)[0]["owner_id"] == b.owner_id, "B to take over", timeout=LEASE_SECONDS + 10)
    assert time.monotonic() - exited_at < LEASE_SECONDS, "handoff must not wait for the lease to run out"
    assert_b_completed_it_exactly_once(a, b, vid, job_id, swarmsync)
