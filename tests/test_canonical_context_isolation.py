from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import agent_runtime
import main
from agent_runtime import AgentRuntime
from runtime import workspace_manager


def test_context_catalog_covers_fresh_long_conflicting_cross_session_and_resume() -> None:
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    matrix = json.loads((root / "eval/canonical_scenarios/canonical_route_tool_failure_matrix.json").read_text(encoding="utf-8"))
    assert set(matrix["context_classes"]) == {
        "fresh_session", "long_session", "conflicting_prior_instruction", "cross_session_contamination",
        "stale_state", "partial_state", "interrupted_execution", "resumed_execution",
    }


def test_resolved_context_is_tenant_bound_bounded_and_evidence_backed() -> None:
    principal = SimpleNamespace(tenant_id="tenant-a")
    good = SimpleNamespace(task={"description":"ignored", "resolved_context":[{
        "tenant_id":"tenant-a", "citation":"source:1", "content_sha256":"abc123", "content":"current evidence"
    }]})
    scoped = main._scoped_runtime_params(good, principal)
    assert scoped == {"resolved_context":[{"citation":"source:1","content_sha256":"abc123","content":"current evidence"}]}

    foreign = SimpleNamespace(task={"resolved_context":[{
        "tenant_id":"tenant-b", "citation":"source:2", "content_sha256":"def456", "content":"foreign"
    }]})
    with pytest.raises(HTTPException) as exc:
        main._scoped_runtime_params(foreign, principal)
    assert exc.value.status_code == 403

    too_many = SimpleNamespace(task={"resolved_context":[{}] * 9})
    with pytest.raises(HTTPException) as exc:
        main._scoped_runtime_params(too_many, principal)
    assert exc.value.status_code == 422

    partial = SimpleNamespace(task={"resolved_context":[{"citation":"source:3","content":"missing hash"}]})
    with pytest.raises(HTTPException) as exc:
        main._scoped_runtime_params(partial, principal)
    assert exc.value.status_code == 422


def test_job_workspaces_and_session_ids_are_isolated(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(workspace_manager, "WORKSPACE_ROOT", str(tmp_path))
    workspace_manager._registry.clear()
    one = workspace_manager.create_workspace("job-one", "session-one")
    two = workspace_manager.create_workspace("job-two", "session-two")
    assert one.path != two.path
    assert one.session_id != two.session_id
    assert workspace_manager.assert_inside_workspace("job-one", one.path / "artifact.json")
    with pytest.raises(PermissionError):
        workspace_manager.assert_inside_workspace("job-one", two.path / "artifact.json")


@pytest.mark.asyncio
async def test_sequential_runs_do_not_reuse_prior_messages_or_session_context(monkeypatch, tmp_path) -> None:
    captured = []

    async def inspect(_self, _model, messages, _tools, _budget):
        captured.append([dict(m) for m in messages])
        return {"choices": [{"message": {"content": "ok", "tool_calls": []}}], "usage": {"total_tokens": 1}}

    monkeypatch.setattr(AgentRuntime, "_call_llm", inspect)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    runtime = AgentRuntime("http://llm.invalid", "redacted")
    bundle = {"slug":"genesis-data-intelligence","tools_advertised":[],"token_budget":100,"system_prompt":"system"}
    first = await runtime._run_loop(bundle, "tenant A secret context", {}, "job-a", tmp_path / "a", None, "session-a")
    second = await runtime._run_loop(bundle, "tenant B independent context", {}, "job-b", tmp_path / "b", None, "session-b")
    assert first["trace"]["session_id"] == "session-a"
    assert second["trace"]["session_id"] == "session-b"
    assert "tenant A secret context" not in str(captured[1])
    assert "tenant B independent context" in str(captured[1])
