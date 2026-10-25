"""Durable store for /verify and arbitrage verification jobs (table genesis_verification_jobs, migration 006).

Replaces the in-memory dicts in main.py. Every function is synchronous psycopg through
``runtime.pg_store.transaction``; async callers wrap them in ``asyncio.to_thread``.

Key properties:
  * ``claim`` is the write-before-ack step: it commits the row (or finds the existing one for the same
    (kind, environment, verification_id)) before the HTTP 202 is sent.
  * ``outstanding`` is what startup recovery works from: runs that never finished and results SwarmSync
    has not accepted yet.
"""
from __future__ import annotations

import uuid
import os
from typing import Any

from psycopg.types.json import Jsonb

from runtime import pg_store

KIND_VERIFY = "verify"
KIND_ARBITRAGE = "arbitrage"

CALLBACK_NONE = "none"
CALLBACK_PENDING = "callback_pending"
CALLBACK_DELIVERED = "delivered"
CALLBACK_REJECTED = "rejected"

_COLUMNS = (
    "job_id, kind, environment, verification_id, request, status, result, error, callback_state, "
    "callback_body, callback_attempts, created_at, completed_at, heartbeat_at, owner_id, lease_expires_at"
)

#: Identifies this gateway process in genesis_verification_jobs.owner_id. Tests simulate a second worker by patching it.
OWNER_ID = uuid.uuid4().hex

DEFAULT_LEASE_SECONDS = 30.0


def lease_seconds() -> float:
    try:
        value = float(os.getenv("GENESIS_VERIFY_LEASE_SECONDS", ""))
    except ValueError:
        return DEFAULT_LEASE_SECONDS
    return value if value > 0 else DEFAULT_LEASE_SECONDS


# A worker may take a job that nobody holds: never owned, owned by itself, or whose lease ran out / was released.
_FREE_OR_MINE = "(owner_id IS NULL OR owner_id = %s OR lease_expires_at IS NULL OR lease_expires_at <= now())"


def _row(cur: Any) -> dict[str, Any] | None:
    raw = cur.fetchone()
    if raw is None:
        return None
    names = [d.name for d in cur.description]
    row = dict(zip(names, raw)) if not isinstance(raw, dict) else dict(raw)
    for key in ("created_at", "completed_at", "heartbeat_at", "lease_expires_at"):
        if row.get(key) is not None:
            row[key] = row[key].isoformat()
    return row


def claim(kind: str, environment: str, verification_id: str, request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Insert the job, or return the existing one for the same key. Returns (row, created)."""
    with pg_store.transaction() as cur:
        cur.execute(
            "INSERT INTO genesis_verification_jobs (job_id, kind, environment, verification_id, request) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (kind, environment, verification_id) DO NOTHING "
            f"RETURNING {_COLUMNS}",
            (str(uuid.uuid4()), kind, environment, verification_id, Jsonb(request)),
        )
        created = _row(cur)
        if created is not None:
            return created, True
        cur.execute(
            f"SELECT {_COLUMNS} FROM genesis_verification_jobs "
            "WHERE kind = %s AND environment = %s AND verification_id = %s",
            (kind, environment, verification_id),
        )
        existing = _row(cur)
    assert existing is not None  # the conflict proves the row exists
    return existing, False


def get(kind: str, job_id: str) -> dict[str, Any] | None:
    with pg_store.transaction() as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM genesis_verification_jobs WHERE job_id = %s AND kind = %s", (job_id, kind))
        return _row(cur)


def claim_for_run(job_id: str, lease: float | None = None) -> dict[str, Any] | None:
    """Atomically take the lease on an unfinished job and mark it running. None = another live owner holds it."""
    seconds = lease if lease is not None else lease_seconds()
    with pg_store.transaction() as cur:
        cur.execute(
            "UPDATE genesis_verification_jobs SET owner_id = %s, status = 'running', heartbeat_at = now(), "
            "lease_expires_at = now() + make_interval(secs => %s) "
            f"WHERE job_id = %s AND status IN ('pending', 'running') AND {_FREE_OR_MINE} "
            f"RETURNING {_COLUMNS}",
            (OWNER_ID, seconds, job_id, OWNER_ID),
        )
        return _row(cur)


def claim_for_delivery(job_id: str, lease: float | None = None) -> dict[str, Any] | None:
    """Same lease, for re-sending the stored result of a finished job. None = another live owner holds it."""
    seconds = lease if lease is not None else lease_seconds()
    with pg_store.transaction() as cur:
        cur.execute(
            "UPDATE genesis_verification_jobs SET owner_id = %s, heartbeat_at = now(), "
            "lease_expires_at = now() + make_interval(secs => %s) "
            "WHERE job_id = %s AND status IN ('completed', 'failed') AND callback_state = 'callback_pending' "
            f"AND {_FREE_OR_MINE} RETURNING {_COLUMNS}",
            (OWNER_ID, seconds, job_id, OWNER_ID),
        )
        return _row(cur)


def heartbeat(job_id: str, lease: float | None = None) -> bool:
    """Renew the lease. False = this process no longer owns the job (it was taken over): stop working on it."""
    seconds = lease if lease is not None else lease_seconds()
    with pg_store.transaction() as cur:
        cur.execute(
            "UPDATE genesis_verification_jobs SET heartbeat_at = now(), "
            "lease_expires_at = now() + make_interval(secs => %s) WHERE job_id = %s AND owner_id = %s",
            (seconds, job_id, OWNER_ID),
        )
        return cur.rowcount == 1


def release(job_id: str) -> None:
    """Hand the job off at once: the lease ends now, so a successor can claim it without waiting for expiry."""
    with pg_store.transaction() as cur:
        cur.execute(
            "UPDATE genesis_verification_jobs SET lease_expires_at = now() WHERE job_id = %s AND owner_id = %s",
            (job_id, OWNER_ID),
        )


def release_all_mine() -> int:
    """Release every unfinished lease this process holds (graceful shutdown). Returns how many."""
    with pg_store.transaction() as cur:
        cur.execute(
            "UPDATE genesis_verification_jobs SET lease_expires_at = now() "
            "WHERE owner_id = %s AND (status IN ('pending', 'running') OR callback_state = 'callback_pending') "
            "AND (lease_expires_at IS NULL OR lease_expires_at > now())",
            (OWNER_ID,),
        )
        return cur.rowcount


def store_outcome(
    job_id: str,
    *,
    status: str,
    result: dict[str, Any] | None,
    error: str | None,
    callback_body: dict[str, Any],
) -> bool:
    """Persist the run's outcome and the exact callback body, and mark the callback as owed.

    Only the current lease owner may write. False = ownership was lost (another worker took the job over) and
    nothing was written: a worker that lost its lease must not overwrite the new owner's result.
    """
    with pg_store.transaction() as cur:
        cur.execute(
            "UPDATE genesis_verification_jobs SET status = %s, result = %s, error = %s, callback_body = %s, "
            "callback_state = %s, completed_at = now(), heartbeat_at = now() "
            "WHERE job_id = %s AND owner_id = %s AND status IN ('pending', 'running')",
            (
                status,
                Jsonb(result) if result is not None else None,
                error,
                Jsonb(callback_body),
                CALLBACK_PENDING,
                job_id,
                OWNER_ID,
            ),
        )
        return cur.rowcount == 1


def record_callback_attempt(job_id: str) -> None:
    with pg_store.transaction() as cur:
        cur.execute(
            "UPDATE genesis_verification_jobs SET callback_attempts = callback_attempts + 1, heartbeat_at = now() "
            "WHERE job_id = %s",
            (job_id,),
        )


def set_callback_state(job_id: str, state: str) -> None:
    """Record the callback outcome. A final state (delivered / rejected) also ends this owner's lease."""
    with pg_store.transaction() as cur:
        cur.execute(
            "UPDATE genesis_verification_jobs SET callback_state = %s, "
            "lease_expires_at = CASE WHEN owner_id = %s THEN now() ELSE lease_expires_at END WHERE job_id = %s",
            (state, OWNER_ID, job_id),
        )


def outstanding() -> list[dict[str, Any]]:
    """Jobs whose run never finished, or whose finished result SwarmSync has not accepted. Oldest first."""
    with pg_store.transaction() as cur:
        cur.execute(
            f"SELECT {_COLUMNS}, "
            "(lease_expires_at IS NOT NULL AND lease_expires_at > now() AND owner_id <> %s) AS held_by_other "
            "FROM genesis_verification_jobs "
            "WHERE status IN ('pending', 'running') OR callback_state = 'callback_pending' "
            "ORDER BY created_at",
            (OWNER_ID,),
        )
        rows = []
        while (row := _row(cur)) is not None:
            rows.append(row)
        return rows
