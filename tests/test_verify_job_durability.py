"""DRAFT regression tests, destination: genesis-env/tests/test_verify_job_durability.py  (pytest + real PostgreSQL)

The gateway half of  delivery -> Conduit verification -> escrow settlement.

What is true on current main (main.py ~L1843, ~L1884, ~L3927):
  * POST /verify keeps its job in the module-level dict ``_verification_jobs``. A restart loses every pending or
    running job, and GET /verify/{jobId} then answers 404 for a jobId SwarmSync already stored as
    ConduitVerification.conduitSessionId.
  * The result goes back in ONE callback POST. A non-200 only logs. A raised exception is caught by the broad
    ``except Exception`` and turns into a ``passed: false`` error callback, even though the verification PASSED.
  * Nothing keys a job on (environment, verification_id): every POST /verify is a new job, a new run and a new callback.

How these tests restart the gateway
  The real FastAPI app is served by uvicorn on a loopback socket in its own thread. ``crash()`` force-exits it (the
  event loop is closed, in-flight background tasks are cancelled, no shutdown handlers run) and then empties the
  module-level containers, which is exactly what a process kill does to in-memory state. Only the database survives.

Network boundary fakes (the only fakes): ``run_verification`` (the Conduit browser/MCP run) and ``httpx.AsyncClient``
as used for the callback to SwarmSync. Persistence is the real thing: tests use the repo's real-PostgreSQL fixtures and
SKIP (with the conftest's explicit reason) when no database is configured. CI must therefore run them with
GENESIS_TEST_DATABASE_URL set and fail the job on any skip.

Fix contract assumed by the recovery tests (one place to adapt, see ``run_recovery``):
  * an entrypoint ``main.recover_verification_jobs()`` (override with env VERIFY_RECOVERY_ENTRYPOINT) that is safe to
    call any number of times, resumes interrupted jobs, redelivers undelivered callbacks, and returns when that work
    is done; sync or ``async def``.
  * any in-process cache the fix adds must be droppable through ``main.reset_in_memory_state_for_tests()``.

Expected on current main:  G1-G7 and G9 FAIL, G8 and G10 PASS (they stop the fix from over-correcting).
After the fix:             all PASS.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
import uvicorn

PROJECT_ROOT = Path(os.getenv("GENESIS_REPO_ROOT", Path(__file__).resolve().parent.parent))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402
from verification_models import (  # noqa: E402
    VerificationContext,
    VerificationResult,
    VerificationSpec,
    VerifyRequest,
)

PROD_URL = "https://api.swarmsync.ai"
CERT_URL = "https://swarmsync-api-cert-staging.onrender.com"
PROD_GW, PROD_INT = "prod-gateway-secret-0123456789", "prod-internal-secret-0123456789"
CERT_GW, CERT_INT = "cert-gateway-secret-0123456789", "cert-internal-secret-0123456789"
API_KEY = "genesis-api-key-for-direct-callers"
RECOVERY_ENTRYPOINT = os.getenv("VERIFY_RECOVERY_ENTRYPOINT", "recover_verification_jobs")
_REAL_SLEEP = asyncio.sleep


# --------------------------------------------------------------------------------------------- environment


@pytest.fixture(autouse=True)
def two_environments(monkeypatch):
    monkeypatch.setenv("SWARMSYNC_API_URL", PROD_URL)
    monkeypatch.setenv("AGENT_GATEWAY_SECRET", PROD_GW)
    monkeypatch.setenv("INTERNAL_SECRET", PROD_INT)
    monkeypatch.setenv("GATEWAY_API_KEY", API_KEY)
    monkeypatch.setenv(
        "SWARMSYNC_ENVIRONMENTS",
        json.dumps([{"name": "cert-staging", "api_url": CERT_URL, "gateway_secret": CERT_GW, "internal_secret": CERT_INT}]),
    )


@pytest.fixture(autouse=True)
def durable_database(migrated_postgres_url, monkeypatch):
    """The gateway's durable state lives in a real PostgreSQL database (skips with the conftest reason if none)."""
    monkeypatch.setenv("GENESIS_JOB_DATABASE_URL", migrated_postgres_url)
    monkeypatch.setenv("GENESIS_STORE_BACKEND", "postgres")
    return migrated_postgres_url


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    """Retry back-off is a clock concern, not behaviour under test: cap every asyncio.sleep at 10 ms."""

    async def quick(delay=0, result=None):
        return await _REAL_SLEEP(min(delay, 0.01), result)

    monkeypatch.setattr(asyncio, "sleep", quick)


def lose_in_memory_state() -> None:
    for name in ("_verification_jobs", "_arbitrage_verification_jobs"):
        store = getattr(main, name, None)
        if isinstance(store, dict):
            store.clear()
    hook = getattr(main, "reset_in_memory_state_for_tests", None)
    if callable(hook):
        hook()


def run_recovery() -> None:
    entry = getattr(main, RECOVERY_ENTRYPOINT, None)
    if entry is None:
        pytest.fail(
            f"main.{RECOVERY_ENTRYPOINT}() does not exist: nothing resumes an interrupted /verify job or redelivers "
            "an undelivered callback after a restart. This is the gap the durability fix has to close."
        )
    outcome = entry()
    if inspect.isawaitable(outcome):
        asyncio.run(outcome)


def wait_until(predicate, what: str, timeout: float = 6.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail(f"timed out after {timeout}s waiting for: {what}")


def stays(predicate, what: str, seconds: float = 0.4) -> None:
    """The condition holds for the whole window (used for 'and nothing more happens')."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        assert predicate(), what
        time.sleep(0.02)


# --------------------------------------------------------------------------------------------- fakes at the boundary


class Verifier:
    """Stands in for the Conduit run. A gate keeps a verification 'running' until the test opens it."""

    def __init__(self):
        self.runs: list[str] = []
        self.gates: dict[str, threading.Event] = {}
        self.passed = True

    def hold(self, verification_id: str) -> threading.Event:
        gate = self.gates.setdefault(verification_id, threading.Event())
        gate.clear()
        return gate

    async def __call__(self, session_id, spec, context):
        self.runs.append(context.verification_id)
        gate = self.gates.get(context.verification_id)
        while gate is not None and not gate.is_set():
            await _REAL_SLEEP(0.01)  # cancellable, unlike an executor thread blocked on an Event
        return VerificationResult(
            passed=self.passed, proof_hash="ab" * 32, conduit_session_sig="cd" * 32, extracted_content="audit done"
        )


class Callbacks:
    """Stands in for SwarmSync's POST /conduit/verifications/{id}/callback. outcome(n) -> status code or exception."""

    def __init__(self, outcome=lambda n: 200):
        self.outcome = outcome
        self.attempts: list[dict] = []
        self._lock = threading.Lock()

    @property
    def delivered(self) -> list[dict]:
        return [a for a in self.attempts if a["status"] == 200]

    def for_verification(self, verification_id: str, only_delivered=False) -> list[dict]:
        rows = self.delivered if only_delivered else self.attempts
        return [a for a in rows if a["url"].endswith(f"/conduit/verifications/{verification_id}/callback")]

    def client_class(self):
        outer = self

        class _Resp:
            def __init__(self, status):
                self.status_code = status
                self.text = "scripted"

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None, **k):
                with outer._lock:
                    n = len(outer.attempts) + 1
                    result = outer.outcome(n)
                    status = 0 if isinstance(result, BaseException) else result
                    outer.attempts.append({"n": n, "url": url, "json": json, "headers": dict(headers or {}), "status": status})
                if isinstance(result, BaseException):
                    raise result
                return _Resp(result)

        return FakeClient


@pytest.fixture
def verifier(monkeypatch):
    v = Verifier()
    monkeypatch.setattr(main, "run_verification", v)
    return v


@pytest.fixture
def callbacks(monkeypatch):
    cb = Callbacks()
    monkeypatch.setattr(main.httpx, "AsyncClient", cb.client_class())
    return cb


# --------------------------------------------------------------------------------------------- the gateway process


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Gateway:
    """The real app on a real socket. crash() is a kill: no shutdown hooks, tasks cancelled, memory gone."""

    instances: list["Gateway"] = []

    def __init__(self):
        self.port = _free_port()
        Gateway.instances.append(self)
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=10)

    def start(self) -> "Gateway":
        config = uvicorn.Config(main.app, host="127.0.0.1", port=self.port, lifespan="off", log_level="warning", access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        wait_until(lambda: self.server.started, "gateway to accept connections")
        return self

    def crash(self) -> None:
        assert self.server is not None and self.thread is not None
        self.server.force_exit = True
        self.server.should_exit = True
        self.thread.join(timeout=15)
        assert not self.thread.is_alive(), "gateway thread did not stop"
        self.client.close()
        lose_in_memory_state()

    def restart(self) -> "Gateway":
        self.crash()
        return Gateway().start()

    def post_verify(self, verification_id: str, secret: str = CERT_INT) -> httpx.Response:
        body = VerifyRequest(
            spec=VerificationSpec(inline_content="audit done"),
            context=VerificationContext(escrow_ref="esc-1", verification_id=verification_id),
        ).model_dump()
        return self.client.post("/verify", json=body, headers={"X-Internal-Secret": secret})

    def get_job(self, job_id: str, secret: str = CERT_INT) -> httpx.Response:
        return self.client.get(f"/verify/{job_id}", headers={"X-Internal-Secret": secret})


@pytest.fixture
def gateway():
    Gateway.instances = []
    gw = Gateway().start()
    yield gw
    for g in Gateway.instances:  # the original and every restart of it
        if g.server is not None and g.thread is not None and g.thread.is_alive():
            g.server.force_exit = True
            g.server.should_exit = True
            g.thread.join(timeout=15)


def new_verification_id() -> str:
    return f"ver-{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------------------------- tests


def test_G1_pending_job_is_still_pollable_after_the_gateway_restarts(gateway, verifier, callbacks):
    """restart during pending verification. SwarmSync stored the jobId; polling it after a restart must not 404."""
    vid = new_verification_id()
    verifier.hold(vid)
    posted = gateway.post_verify(vid)
    assert posted.status_code == 202
    job_id = posted.json()["jobId"]
    wait_until(lambda: vid in verifier.runs, "the verification to be running")
    created_at = gateway.get_job(job_id).json()["created_at"]

    after = gateway.restart()
    polled = after.get_job(job_id)
    assert polled.status_code == 200, f"jobId {job_id} vanished with the process: HTTP {polled.status_code}"
    assert polled.json()["status"] in {"pending", "running", "queued"}
    assert polled.json()["created_at"] == created_at
    # a durable job is still private to the environment that created it
    assert after.get_job(job_id, secret=PROD_INT).status_code == 404
    verifier.gates[vid].set()


def test_G2_interrupted_job_is_resumed_after_restart_and_calls_back_exactly_once_to_its_own_environment(gateway, verifier, callbacks):
    """restart during pending verification (resume side). One callback, to the right origin, with the right secret."""
    vid = new_verification_id()
    gate = verifier.hold(vid)
    assert gateway.post_verify(vid).status_code == 202
    wait_until(lambda: vid in verifier.runs, "the verification to be running")

    gateway.restart()
    gate.set()  # the resumed run is allowed to finish
    run_recovery()
    wait_until(lambda: len(callbacks.for_verification(vid, only_delivered=True)) == 1, "the resumed job's callback")

    run_recovery()  # a second sweep must find nothing left to do
    stays(lambda: len(callbacks.for_verification(vid)) == 1, "recovery sweeps must not duplicate the callback")
    cb = callbacks.for_verification(vid)[0]
    assert cb["url"] == f"{CERT_URL}/conduit/verifications/{vid}/callback"
    assert cb["headers"]["X-Internal-Secret"] == CERT_INT
    assert cb["json"]["passed"] is True


def test_G3_a_failed_callback_is_retried_with_an_identical_body_until_swarmsync_accepts_it(gateway, verifier, callbacks):
    """retry after timeout. 503, then a dropped connection, then 200: the SAME result is redelivered, then it stops."""
    callbacks.outcome = lambda n: 503 if n == 1 else httpx.ConnectTimeout("scripted timeout") if n == 2 else 200
    vid = new_verification_id()
    assert gateway.post_verify(vid).status_code == 202
    wait_until(lambda: len(callbacks.for_verification(vid, only_delivered=True)) == 1, "the callback to be accepted eventually")
    attempts = callbacks.for_verification(vid)
    assert len(attempts) == 3, f"expected 503, timeout, 200 but saw {[a['status'] for a in attempts]}"
    assert all(a["json"] == attempts[0]["json"] for a in attempts), "a retried callback must carry the identical result"
    assert attempts[0]["json"]["passed"] is True and attempts[0]["json"]["proofHash"] == "ab" * 32
    stays(lambda: len(callbacks.for_verification(vid)) == 3, "no attempts after SwarmSync accepted it")


def test_G4_an_undelivered_callback_survives_a_restart_and_is_delivered_once(gateway, verifier, callbacks):
    """restart after verification but before the result reached SwarmSync (so before release)."""
    api_down = {"value": True}
    callbacks.outcome = lambda n: 503 if api_down["value"] else 200
    vid = new_verification_id()
    assert gateway.post_verify(vid).status_code == 202
    wait_until(lambda: len(callbacks.for_verification(vid)) >= 1, "the first (failing) callback attempt")

    gateway.restart()
    api_down["value"] = False
    attempts_before = len(callbacks.for_verification(vid))
    run_recovery()
    wait_until(lambda: len(callbacks.for_verification(vid, only_delivered=True)) == 1, "the undelivered callback to be delivered after restart")
    delivered = callbacks.for_verification(vid, only_delivered=True)[0]
    assert delivered["json"]["passed"] is True
    assert verifier.runs.count(vid) == 1, "the finished verification must not be run again, only its result redelivered"

    run_recovery()
    stays(lambda: len(callbacks.for_verification(vid, only_delivered=True)) == 1, "an acknowledged callback is never sent again")
    assert len(callbacks.for_verification(vid)) >= attempts_before + 1


def test_G5_a_repeated_verify_request_for_the_same_verification_is_deduplicated(gateway, verifier, callbacks):
    """duplicate delivery -> SwarmSync re-dispatches (e.g. its own retry). One run, one callback, whatever the count."""
    vid = new_verification_id()
    first = gateway.post_verify(vid)
    wait_until(lambda: len(callbacks.for_verification(vid, only_delivered=True)) == 1, "first job to call back")
    second = gateway.post_verify(vid)
    assert first.status_code == 202 and second.status_code == 202
    assert second.json()["jobId"] == first.json()["jobId"], "the same verification maps to the same job"
    stays(lambda: verifier.runs.count(vid) == 1 and len(callbacks.for_verification(vid)) == 1, "no second run or callback")


def test_G6_concurrent_verify_requests_for_one_verification_run_once(gateway, verifier, callbacks):
    """concurrent retry (parallel). Five simultaneous POSTs while the first run is still going."""
    vid = new_verification_id()
    gate = verifier.hold(vid)
    responses: list[httpx.Response] = []

    def fire():
        responses.append(gateway.post_verify(vid))

    first = threading.Thread(target=fire)
    first.start()
    wait_until(lambda: vid in verifier.runs, "the first run to start")
    others = [threading.Thread(target=fire) for _ in range(4)]
    for t in others:
        t.start()
    for t in others + [first]:
        t.join(timeout=10)
    assert [r.status_code for r in responses] == [202] * 5
    assert len({r.json()["jobId"] for r in responses}) == 1, "one verification, one job"
    gate.set()
    wait_until(lambda: len(callbacks.for_verification(vid, only_delivered=True)) == 1, "the single callback")
    stays(lambda: verifier.runs.count(vid) == 1 and len(callbacks.for_verification(vid)) == 1, "exactly one run and one callback")


def test_G7_a_verify_request_after_a_restart_is_answered_from_the_durable_record(gateway, verifier, callbacks):
    """duplicate verification callback source: SwarmSync retries /verify after the gateway came back."""
    vid = new_verification_id()
    first = gateway.post_verify(vid)
    wait_until(lambda: len(callbacks.for_verification(vid, only_delivered=True)) == 1, "callback delivered before the crash")

    after = gateway.restart()
    again = after.post_verify(vid)
    assert again.status_code == 202
    assert again.json()["jobId"] == first.json()["jobId"]
    stays(lambda: verifier.runs.count(vid) == 1 and len(callbacks.for_verification(vid, only_delivered=True)) == 1, "no re-run, no second delivered callback")


def test_G8_the_same_verification_id_in_two_environments_is_two_jobs(gateway, verifier, callbacks):
    """Guard against over-deduplicating: the key is (environment, verification_id), never verification_id alone."""
    vid = new_verification_id()
    cert = gateway.post_verify(vid, secret=CERT_INT)
    prod = gateway.post_verify(vid, secret=PROD_INT)
    assert cert.status_code == 202 and prod.status_code == 202
    assert cert.json()["jobId"] != prod.json()["jobId"]
    wait_until(lambda: len(callbacks.delivered) == 2, "one callback per environment")
    by_url = {a["url"]: a for a in callbacks.delivered}
    assert by_url[f"{CERT_URL}/conduit/verifications/{vid}/callback"]["headers"]["X-Internal-Secret"] == CERT_INT
    assert by_url[f"{PROD_URL}/conduit/verifications/{vid}/callback"]["headers"]["X-Internal-Secret"] == PROD_INT


def test_G9_a_passing_verification_is_never_reported_as_failed_because_the_callback_transport_failed(gateway, verifier, callbacks):
    """The broad except in _run_and_callback sends passed:false after a PASSING run whose callback POST raised."""
    callbacks.outcome = lambda n: httpx.ConnectError("scripted: SwarmSync unreachable") if n == 1 else 200
    vid = new_verification_id()
    assert gateway.post_verify(vid).status_code == 202
    wait_until(lambda: len(callbacks.for_verification(vid, only_delivered=True)) >= 1, "some callback to be delivered")
    stays(lambda: all(a["json"]["passed"] is True for a in callbacks.for_verification(vid)), "no passed:false for a run that passed")
    assert callbacks.for_verification(vid, only_delivered=True)[0]["json"]["passed"] is True
    assert callbacks.for_verification(vid, only_delivered=True)[0]["json"]["proofHash"] == "ab" * 32


def test_G10_a_genuinely_failing_run_still_reports_failure_exactly_once(gateway, verifier, callbacks, monkeypatch):
    """Guard against the fix hiding real failures: a run that raises is still reported passed:false, once."""

    async def boom(session_id, spec, context):
        raise RuntimeError("browser exploded")

    monkeypatch.setattr(main, "run_verification", boom)
    vid = new_verification_id()
    assert gateway.post_verify(vid).status_code == 202
    wait_until(lambda: len(callbacks.for_verification(vid, only_delivered=True)) == 1, "the failure callback")
    stays(lambda: len(callbacks.for_verification(vid)) == 1, "one failure callback, not a storm")
    body = callbacks.for_verification(vid)[0]["json"]
    assert body["passed"] is False and "browser exploded" in body["failureReason"]
