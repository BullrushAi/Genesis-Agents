"""genesis_proofs and genesis_jobs."proofId" exist after migrating, and work.

proof_bridge._persist_proof swallows every exception ("non-fatal"), so a missing
table makes every proof silently vanish while all unit tests stay green. These
tests run the real runner-built schema and the real _persist_proof SQL against
a REAL PostgreSQL server (see tests/conftest.py::postgres_url) and read the rows
back the way main.py::verify_proof does.
"""

from __future__ import annotations

import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

import proof_bridge  # noqa: E402


def _insert_job(url: str, job_id: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO genesis_jobs (id, \"agentSlug\", status) VALUES (%s, %s, %s)",
            (job_id, "proof-test-agent", "COMPLETED_TEST"),
        )


def _persist(job_id: str, **overrides):
    fields = dict(
        job_id=job_id,
        agent_slug="proof-test-agent",
        buyer_client_id="client-1",
        buyer_wallet_id=None,
        proof_bundle_uri="file:///tmp/bundle.tar.gz",
        proof_bundle_signed_url="",
        conduit_session_id="unknown",
        conduit_pubkey="",
        input_hash="a" * 64,
        output_hash="b" * 64,
        vcap_jwt="header.payload.",
        duration_s=1.5,
    )
    fields.update(overrides)
    return proof_bridge._persist_proof(**fields)


def test_persist_proof_writes_the_row_and_links_it_to_the_job(pg_env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_env)
    job_id = f"job-{uuid.uuid4().hex[:12]}"
    _insert_job(pg_env, job_id)

    proof_id = _persist(job_id)

    assert proof_id, "_persist_proof returned None: the insert failed and was swallowed"
    with psycopg.connect(pg_env, row_factory=dict_row) as conn:
        proof = conn.execute("SELECT * FROM genesis_proofs WHERE id = %s", (proof_id,)).fetchone()
        job = conn.execute('SELECT "proofId" FROM genesis_jobs WHERE id = %s', (job_id,)).fetchone()
    assert proof is not None
    assert proof["jobId"] == job_id
    assert proof["agentSlug"] == "proof-test-agent"
    assert proof["inputHash"] == "a" * 64 and proof["outputHash"] == "b" * 64
    assert proof["durationSeconds"] == 1.5
    assert proof["totalAuditEntries"] == 0 and proof["totalToolCalls"] == 0
    assert proof["createdAt"] is not None
    # every column main.py::verify_proof reads is present
    for column in (
        "jobId", "agentSlug", "createdAt", "vcapWrapperJwt", "proofBundleUri",
        "proofBundleSignedUrl", "inputHash", "outputHash", "conduitSessionId",
    ):
        assert column in proof
    assert job["proofId"] == proof_id


def test_persist_proof_is_an_upsert_on_job_id(pg_env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_env)
    job_id = f"job-{uuid.uuid4().hex[:12]}"
    _insert_job(pg_env, job_id)

    first = _persist(job_id, proof_bundle_uri="file:///first")
    second = _persist(job_id, proof_bundle_uri="file:///second", vcap_jwt="new.jwt.")

    assert first and second
    with psycopg.connect(pg_env, row_factory=dict_row) as conn:
        rows = conn.execute('SELECT * FROM genesis_proofs WHERE "jobId" = %s', (job_id,)).fetchall()
    assert len(rows) == 1, "a second proof for the same job must update, not duplicate"
    assert rows[0]["proofBundleUri"] == "file:///second"
    assert rows[0]["vcapWrapperJwt"] == "new.jwt."


def test_a_job_can_only_carry_one_proof_id(pg_env):
    job_a = f"job-{uuid.uuid4().hex[:12]}"
    job_b = f"job-{uuid.uuid4().hex[:12]}"
    _insert_job(pg_env, job_a)
    _insert_job(pg_env, job_b)
    with psycopg.connect(pg_env) as conn:
        conn.execute('UPDATE genesis_jobs SET "proofId" = %s WHERE id = %s', ("p-shared", job_a))
    with pytest.raises(psycopg.errors.UniqueViolation):
        with psycopg.connect(pg_env) as conn:
            conn.execute('UPDATE genesis_jobs SET "proofId" = %s WHERE id = %s', ("p-shared", job_b))


def test_migration_009_is_re_runnable_on_a_migrated_database(pg_env):
    """Idempotent: every statement tolerates the objects already existing."""
    from migrations import runner

    sql = runner.strip_outer_transaction(
        (runner.MIGRATIONS_DIR / "009_genesis_proofs.sql").read_text(encoding="utf-8")
    )
    with psycopg.connect(pg_env, autocommit=True) as conn:
        conn.execute(sql)
        conn.execute(sql)
