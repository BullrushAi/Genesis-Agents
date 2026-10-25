"""Lease ownership for durable /verify jobs (real PostgreSQL, GENESIS_TEST_DATABASE_URL).

Bug under test: during a Render rolling restart the OLD container kept running an in-flight verification while the NEW
container's startup recovery re-ran the same job. A job may now be executed only by the worker holding its lease;
leases are renewed by a heartbeat, released on graceful shutdown and simply expire after a crash.

A second worker is simulated by switching ``verification_job_store.OWNER_ID`` (each gateway process has its own).
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from contextlib import contextmanager

import pytest

import main
import swarmsync_environments as swarmsync_envs
import verification_job_store as store
from runtime import pg_store
from verification_models import (
    VerificationContext,
    VerificationResult,
    VerificationSpec,
    VerifyRequest,
)

API_URL = "https://api.swarmsync.ai"
INTERNAL = "lease-internal-secret-0123456789"


@pytest.fixture(autouse=True)
def lease_env(migrated_postgres_url, monkeypatch):
    monkeypatch.setenv("GENESIS_JOB_DATABASE_URL", migrated_postgres_url)
    monkeypatch.setenv("GENESIS_STORE_BACKEND", "postgres")
    monkeypatch.setenv("SWARMSYNC_API_URL", API_URL)
    monkeypatch.setenv("INTERNAL_SECRET", INTERNAL)
    monkeypatch.delenv("SWARMSYNC_ENVIRONMENTS", raising=False)
    monkeypatch.delenv("GENESIS_VERIFY_LEASE_SECONDS", raising=False)
    monkeypatch.setattr(main, "_VERIFY_HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(main, "_VERIFY_CALLBACK_RETRY_DELAYS_S", (0.01, 0.01, 0.01))
    main.reset_in_memory_state_for_tests()
    with pg_store.transaction() as cur:
        cur.execute("TRUNCATE genesis_verification_jobs")
    yield
    main.reset_in_memory_state_for_tests()


@contextmanager
def as_worker(name: str):
    previous = store.OWNER_ID
    store.OWNER_ID = name
    try:
        yield
    finally:
        store.OWNER_ID = previous


def request_for(verification_id: str) -> VerifyRequest:
    return VerifyRequest(
        spec=VerificationSpec(inline_content="audit done"),
        context=VerificationContext(escrow_ref="esc-1", verification_id=verification_id),
    )


def new_job(verification_id: str | None = None) -> dict:
    vid = verification_id or f"ver-{uuid.uuid4().hex[:10]}"
    env = swarmsync_envs.default_environment()
    row, created = store.claim(store.KIND_VERIFY, env.name, vid, request_for(vid).model_dump(mode="json"))
    assert created
    return row


def current(job_id: str) -> dict:
    row = store.get(store.KIND_VERIFY, job_id)
    assert row is not None
    return row


def set_lease(job_id: str, owner: str | None, seconds: float | None) -> None:
    with pg_store.transaction() as cur:
        cur.execute(
            "UPDATE genesis_verification_jobs SET owner_id = %s, "
            "lease_expires_at = CASE WHEN %s::float8 IS NULL THEN NULL ELSE now() + make_interval(secs => %s) END "
            "WHERE job_id = %s",
            (owner, seconds, seconds, job_id),
        )


class Callbacks:
    def __init__(self, monkeypatch):
        self.posts: list[dict] = []
        outer = self

        class _Resp:
            status_code = 200
            text = ""

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None, **k):
                outer.posts.append({"url": url, "json": json})
                return _Resp()

        monkeypatch.setattr(main.httpx, "AsyncClient", _Client)


class Verifier:
    def __init__(self, monkeypatch):
        self.started: list[str] = []
        self.finished: list[str] = []
        self.gates: dict[str, threading.Event] = {}
        monkeypatch.setattr(main, "run_verification", self)

    def hold(self, vid: str) -> threading.Event:
        gate = self.gates.setdefault(vid, threading.Event())
        gate.clear()
        return gate

    async def __call__(self, session_id, spec, context):
        self.started.append(context.verification_id)
        gate = self.gates.get(context.verification_id)
        while gate is not None and not gate.is_set():
            await asyncio.sleep(0.01)
        self.finished.append(context.verification_id)
        return VerificationResult(passed=True, proof_hash="ab" * 32, conduit_session_sig="cd" * 32)


async def wait_for(predicate, what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"timed out waiting for: {what}")


# ------------------------------------------------------------------------------------------------ store level


def test_exactly_one_of_two_workers_claims_a_job():
    job = new_job()
    with as_worker("worker-A"):
        assert store.claim_for_run(job["job_id"], 30) is not None
    with as_worker("worker-B"):
        assert store.claim_for_run(job["job_id"], 30) is None
    assert current(job["job_id"])["owner_id"] == "worker-A"
    assert current(job["job_id"])["status"] == "running"
    with as_worker("worker-A"):  # the owner may re-claim its own job (restart of the same worker)
        assert store.claim_for_run(job["job_id"], 30) is not None


def test_a_second_worker_cannot_take_a_job_while_the_lease_is_valid_and_heartbeat_extends_it():
    job = new_job()
    with as_worker("worker-A"):
        store.claim_for_run(job["job_id"], 1.0)
        assert store.heartbeat(job["job_id"], 5.0) is True  # renewed from 1s to 5s
    time.sleep(1.4)  # the original 1s lease would have run out by now
    with as_worker("worker-B"):
        assert store.claim_for_run(job["job_id"], 30) is None
        assert store.heartbeat(job["job_id"], 30) is False  # not the owner: cannot renew


def test_an_expired_lease_lets_the_second_worker_take_over_and_the_first_learns_it():
    job = new_job()
    with as_worker("worker-A"):
        store.claim_for_run(job["job_id"], 0.3)
    time.sleep(0.5)
    with as_worker("worker-B"):
        assert store.claim_for_run(job["job_id"], 30) is not None
    with as_worker("worker-A"):
        assert store.heartbeat(job["job_id"]) is False


def test_a_worker_that_lost_its_lease_cannot_store_an_outcome():
    job = new_job()
    jid = job["job_id"]
    with as_worker("worker-A"):
        store.claim_for_run(jid, 0.2)
    time.sleep(0.4)
    with as_worker("worker-B"):
        store.claim_for_run(jid, 30)
    with as_worker("worker-A"):
        stale = store.store_outcome(jid, status="completed", result={"x": 1}, error=None, callback_body={"passed": True})
        assert stale is False
    row = current(jid)
    assert row["status"] == "running" and row["result"] is None and row["callback_state"] == "none"
    with as_worker("worker-B"):
        assert store.store_outcome(jid, status="completed", result={"x": 2}, error=None, callback_body={"passed": True})
    assert current(jid)["result"] == {"x": 2}


def test_release_hands_the_job_off_immediately():
    job = new_job()
    jid = job["job_id"]
    with as_worker("worker-A"):
        store.claim_for_run(jid, 300)
    with as_worker("worker-B"):
        assert store.claim_for_run(jid, 30) is None
    with as_worker("worker-A"):
        store.release(jid)
    with as_worker("worker-B"):
        assert store.claim_for_run(jid, 30) is not None


def test_release_all_mine_only_releases_my_unfinished_jobs():
    mine, theirs = new_job(), new_job()
    with as_worker("worker-A"):
        store.claim_for_run(mine["job_id"], 300)
    with as_worker("worker-B"):
        store.claim_for_run(theirs["job_id"], 300)
    with as_worker("worker-A"):
        assert store.release_all_mine() == 1
    with as_worker("worker-C"):
        assert store.claim_for_run(mine["job_id"], 30) is not None
        assert store.claim_for_run(theirs["job_id"], 30) is None


def test_a_finished_jobs_callback_resend_respects_the_lease():
    job = new_job()
    jid = job["job_id"]
    with as_worker("worker-A"):
        store.claim_for_run(jid, 300)
        assert store.store_outcome(jid, status="completed", result={}, error=None, callback_body={"passed": True})
    with as_worker("worker-B"):
        assert store.claim_for_delivery(jid, 30) is None  # A still holds it
    with as_worker("worker-A"):
        store.release(jid)
    with as_worker("worker-B"):
        assert store.claim_for_delivery(jid, 30) is not None
        store.set_callback_state(jid, store.CALLBACK_DELIVERED)
    with as_worker("worker-C"):
        assert store.claim_for_delivery(jid, 30) is None  # delivered: nothing left to send


def test_the_same_verification_id_never_creates_a_second_row():
    first = new_job("ver-reused")
    env = swarmsync_envs.default_environment()
    again, created = store.claim(store.KIND_VERIFY, env.name, "ver-reused", request_for("ver-reused").model_dump(mode="json"))
    assert created is False and again["job_id"] == first["job_id"]
    with pg_store.transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM genesis_verification_jobs WHERE verification_id = 'ver-reused'")
        assert cur.fetchone()['n'] == 1


# ------------------------------------------------------------------------------------------------ gateway level


@pytest.mark.asyncio
async def test_rolling_restart_old_holds_new_boot_skips_old_releases_new_takes_over_once(monkeypatch):
    callbacks = Callbacks(monkeypatch)
    verifier = Verifier(monkeypatch)
    job = new_job("ver-rolling")
    jid = job["job_id"]
    with as_worker("old-container"):
        store.claim_for_run(jid, 300)  # the old container is mid-run with a live lease

    await main.recover_verification_jobs()  # new container's boot recovery
    assert verifier.started == [] and callbacks.posts == [], "boot recovery must not run a job the old container holds"

    with as_worker("old-container"):  # old container receives SIGTERM and shuts down gracefully
        store.release_all_mine()

    await main.recover_verification_jobs()  # new container's periodic loop
    await main.recover_verification_jobs()  # and again: nothing left to do
    assert verifier.started == ["ver-rolling"]
    assert len(callbacks.posts) == 1 and callbacks.posts[0]["json"]["passed"] is True
    row = current(jid)
    assert row["status"] == "completed" and row["callback_state"] == "delivered"
    assert row["owner_id"] == store.OWNER_ID


@pytest.mark.asyncio
async def test_graceful_shutdown_cancels_the_run_stores_nothing_and_releases_the_lease_for_a_successor(monkeypatch):
    callbacks = Callbacks(monkeypatch)
    verifier = Verifier(monkeypatch)
    vid = "ver-shutdown"
    gate = verifier.hold(vid)
    job = new_job(vid)
    jid = job["job_id"]
    env = swarmsync_envs.default_environment()

    main._spawn_verification_task(main._run_and_callback(jid, request_for(vid), env))
    await wait_for(lambda: vid in verifier.started, "the run to start")
    assert current(jid)["owner_id"] == store.OWNER_ID

    await main._release_verification_jobs_on_shutdown(None)
    row = current(jid)
    assert row["status"] == "running" and row["result"] is None, "a cancelled run must not store a result"
    assert callbacks.posts == []
    assert not main._verification_tasks
    with as_worker("successor"):
        assert store.claim_for_run(jid, 30) is not None  # takes over at once, no waiting for expiry
        store.release(jid)

    gate.set()
    main.reset_in_memory_state_for_tests()
    with as_worker("successor"):
        await main.recover_verification_jobs()
    assert verifier.finished == [vid], "exactly one execution ran to completion"
    assert len(callbacks.posts) == 1
    assert current(jid)["callback_state"] == "delivered"


@pytest.mark.asyncio
async def test_hard_crash_leaves_the_lease_to_expire_then_a_successor_takes_over(monkeypatch):
    callbacks = Callbacks(monkeypatch)
    verifier = Verifier(monkeypatch)
    job = new_job("ver-crash")
    jid = job["job_id"]
    with as_worker("crashed-container"):
        store.claim_for_run(jid, 0.4)  # killed: never heartbeats, never releases

    await main.recover_verification_jobs()
    assert verifier.started == [], "the lease is still valid: no takeover yet"

    await asyncio.sleep(0.6)
    await main.recover_verification_jobs()
    assert verifier.started == ["ver-crash"] and len(callbacks.posts) == 1
    assert current(jid)["status"] == "completed"


@pytest.mark.asyncio
async def test_pending_callback_recovery_respects_the_lease(monkeypatch):
    callbacks = Callbacks(monkeypatch)
    verifier = Verifier(monkeypatch)
    job = new_job("ver-callback")
    jid = job["job_id"]
    with as_worker("other-container"):
        store.claim_for_run(jid, 300)
        store.store_outcome(jid, status="completed", result={}, error=None, callback_body={"passed": True, "proofHash": "x"})

    await main.recover_verification_jobs()
    assert callbacks.posts == [], "another live owner is still delivering this callback"

    with as_worker("other-container"):
        store.release(jid)
    await main.recover_verification_jobs()
    await main.recover_verification_jobs()
    assert len(callbacks.posts) == 1 and callbacks.posts[0]["json"] == {"passed": True, "proofHash": "x"}
    assert verifier.started == [], "a finished job is re-delivered, never re-run"
    assert current(jid)["callback_state"] == "delivered"


@pytest.mark.asyncio
async def test_a_worker_whose_lease_was_taken_stops_and_sends_no_callback(monkeypatch):
    callbacks = Callbacks(monkeypatch)
    verifier = Verifier(monkeypatch)
    vid = "ver-stolen"
    verifier.hold(vid)
    job = new_job(vid)
    jid = job["job_id"]
    env = swarmsync_envs.default_environment()

    task = main._spawn_verification_task(main._run_and_callback(jid, request_for(vid), env))
    await wait_for(lambda: vid in verifier.started, "the run to start")
    set_lease(jid, "thief", 300)  # another worker took the job over

    await asyncio.wait_for(task, timeout=5)  # the heartbeat noticed, cancelled the run, and the task ended cleanly
    assert verifier.finished == [] and callbacks.posts == []
    row = current(jid)
    assert row["owner_id"] == "thief" and row["status"] == "running" and row["result"] is None


@pytest.mark.asyncio
async def test_failing_heartbeats_leave_the_lease_to_expire_and_the_run_notices_when_it_is_back(monkeypatch):
    callbacks = Callbacks(monkeypatch)
    verifier = Verifier(monkeypatch)
    monkeypatch.setenv("GENESIS_VERIFY_LEASE_SECONDS", "0.4")
    vid = "ver-netfail"
    verifier.hold(vid)
    job = new_job(vid)
    jid = job["job_id"]
    env = swarmsync_envs.default_environment()
    real_heartbeat = store.heartbeat
    broken = {"on": True}

    def flaky(job_id, lease=None):
        if broken["on"]:
            raise ConnectionError("database unreachable")
        return real_heartbeat(job_id, lease)

    monkeypatch.setattr(store, "heartbeat", flaky)
    task = main._spawn_verification_task(main._run_and_callback(jid, request_for(vid), env))
    await wait_for(lambda: vid in verifier.started, "the run to start")

    await asyncio.sleep(0.7)  # renewals keep failing: the lease runs out on its own, the run is not killed by that
    assert not task.done()
    with as_worker("successor"):
        assert store.claim_for_run(jid, 30) is not None  # takeover after expiry

    broken["on"] = False  # network is back: the next renewal reports the lease is gone
    await asyncio.wait_for(task, timeout=5)
    assert callbacks.posts == [] and verifier.finished == []
    assert current(jid)["owner_id"] == "successor"


@pytest.mark.asyncio
async def test_a_repeated_dispatch_of_a_job_another_container_holds_does_not_run_it(monkeypatch):
    callbacks = Callbacks(monkeypatch)
    verifier = Verifier(monkeypatch)
    env = swarmsync_envs.default_environment()
    job = new_job("ver-dup")
    with as_worker("old-container"):
        store.claim_for_run(job["job_id"], 300)

    again = await main._accept_verification_job(
        store.KIND_VERIFY, env, "ver-dup", request_for("ver-dup").model_dump(mode="json")
    )
    await asyncio.gather(*list(main._verification_tasks))
    assert again == job["job_id"]
    assert verifier.started == [] and callbacks.posts == []
    with pg_store.transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM genesis_verification_jobs")
        assert cur.fetchone()['n'] == 1

    with as_worker("old-container"):
        store.release_all_mine()
    await main._accept_verification_job(store.KIND_VERIFY, env, "ver-dup", request_for("ver-dup").model_dump(mode="json"))
    await asyncio.gather(*list(main._verification_tasks))
    assert verifier.started == ["ver-dup"] and len(callbacks.posts) == 1


def test_lease_length_comes_from_the_environment(monkeypatch):
    assert store.lease_seconds() == 30.0
    monkeypatch.setenv("GENESIS_VERIFY_LEASE_SECONDS", "12")
    assert store.lease_seconds() == 12.0
    monkeypatch.setenv("GENESIS_VERIFY_LEASE_SECONDS", "garbage")
    assert store.lease_seconds() == 30.0
    monkeypatch.setenv("GENESIS_VERIFY_RECOVERY_INTERVAL_SECONDS", "7")
    assert main._verification_recovery_interval_s() == 7.0
    monkeypatch.delenv("GENESIS_VERIFY_RECOVERY_INTERVAL_SECONDS")
    assert main._verification_recovery_interval_s() == 15.0
