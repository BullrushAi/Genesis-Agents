"""One job id must report one status.

Drives the real submit function and the real status reader. The model call is
the only stand-in, matching the other lifecycle tests. Postgres is a throwaway
cluster on 127.0.0.1 — this file never reads a configured database URL.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _release_psycopg_stub() -> None:
    """test_job_lifecycle.py installs a psycopg stub at import. Drop it."""
    mod = sys.modules.get("psycopg")
    if mod is None or getattr(mod, "__file__", None):
        return
    for name in list(sys.modules):
        if name == "psycopg" or name.startswith("psycopg."):
            del sys.modules[name]


_release_psycopg_stub()
import psycopg  # noqa: E402

if not getattr(psycopg, "__file__", None):
    raise RuntimeError("psycopg is a stub; the status test cannot use it")


def _binary(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for base in (
        r"C:\Program Files\PostgreSQL\15\bin",
        r"C:\Program Files\PostgreSQL\16\bin",
        r"C:\Program Files\PostgreSQL\17\bin",
    ):
        candidate = Path(base) / f"{name}.exe"
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(f"{name} was not found")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _result_status(job: dict) -> str:
    """Status a caller gets from the result payload on a status read."""
    raw = job.get("resultSummary")
    if not isinstance(raw, str) or not raw:
        return ""
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or "status" not in parsed or parsed["status"] is None:
        return ""
    return str(parsed["status"])


@pytest.fixture
def local_job_database(tmp_path, monkeypatch):
    """Throwaway Postgres on loopback, migrated by the real runner."""
    required = {name: _binary(name) for name in ("initdb", "pg_ctl", "createdb")}
    data_dir = tmp_path / "pgdata"
    log_path = tmp_path / "postgres.log"
    port = _free_port()
    subprocess.run(
        [required["initdb"], "-D", str(data_dir), "-A", "trust", "--no-locale", "--encoding=UTF8"],
        check=True, capture_output=True, text=True, timeout=180,
    )
    # `-w` on Windows often never returns even after the port is open.
    subprocess.Popen(
        [required["pg_ctl"], "-D", str(data_dir), "-l", str(log_path),
         "-o", f"-h 127.0.0.1 -p {port}", "start"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.4)
    else:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-500:] if log_path.exists() else ""
        raise RuntimeError(f"local postgres did not accept connections: {tail}")
    try:
        subprocess.run(
            [required["createdb"], "-h", "127.0.0.1", "-p", str(port), "genesis_test"],
            check=True, capture_output=True, text=True, timeout=60,
        )
        user = quote(os.environ.get("USERNAME") or os.environ.get("USER") or "postgres", safe="")
        url = f"postgresql://{user}@127.0.0.1:{port}/genesis_test"
        host = urlsplit(url).hostname
        if host not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("refusing a non-loopback database")
        monkeypatch.setenv("GENESIS_JOB_DATABASE_URL", url)
        monkeypatch.delenv("DIRECT_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("TRIGGER_SECRET_KEY", raising=False)
        monkeypatch.delenv("GENESIS_WORKER_ENABLED", raising=False)
        from migrations import runner

        runner.migrate()
        yield url
    finally:
        subprocess.run(
            [required["pg_ctl"], "-D", str(data_dir), "-m", "immediate", "stop"],
            check=False, capture_output=True, text=True, timeout=60,
        )


def test_one_job_status_read_returns_the_same_status_from_the_row_and_the_result(
    local_job_database,
):
    """Submit one job, deliver it, and poll that id. Row and result must agree.

    Empty prompt is the edge input. An unknown id is the rejection path.
    The delivered id is read three times and must be one status, not two.
    """
    from fastapi import HTTPException
    from runtime.request_auth import Principal

    principal = Principal(
        principal_id="cato:status-read",
        tenant_id="e4l",
        client_id="cato",
        scopes=frozenset({"agent.invoke", "job.read"}),
        auth_method="test",
        expires_at=int(time.time()) + 300,
    )

    async def _drive() -> None:
        from job_store import claim_job_by_id
        from main import get_job_status, submit_job
        from worker import process_job

        with pytest.raises(HTTPException) as missing:
            await get_job_status("c" + "0" * 24, principal)
        assert missing.value.status_code == 404

        # This commit's submit_job rejects a body with no prompt, input, or
        # task. The stored prompt stays empty; task only clears that guard.
        created = await submit_job(
            "genesis-knowledge-evidence",
            {"prompt": "", "task": "status consistency"},
            principal,
        )
        job_id = created["job_id"]
        assert created["status"] == "QUEUED"
        assert job_id

        queued = await get_job_status(job_id, principal)
        assert queued["id"] == job_id
        assert queued["status"] == "QUEUED"

        claimed = claim_job_by_id(job_id)
        assert claimed is not None
        assert claimed["status"] == "RUNNING"
        assert claimed["id"] == job_id

        class _Model:
            async def execute_agent(self, slug, prompt, params, *, job_id=None):
                assert job_id == claimed["id"]
                return {"ok": True, "response": "Task complete.", "trace": {}, "slug": slug}

        await process_job(claimed, _Model())

        reads = [await get_job_status(job_id, principal) for _ in range(3)]
        assert [row["id"] for row in reads] == [job_id, job_id, job_id]
        row_sequence = [row["status"] for row in reads]
        payload_sequence = [_result_status(row) for row in reads]
        assert row_sequence == ["DELIVERED", "DELIVERED", "DELIVERED"]
        assert row_sequence == payload_sequence == ["DELIVERED", "DELIVERED", "DELIVERED"]

    asyncio.run(_drive())
