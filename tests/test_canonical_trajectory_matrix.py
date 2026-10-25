from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent_runtime
from agent_runtime import AgentRuntime
from bundle_loader import load_bundle


ROOT = Path(__file__).resolve().parents[1]
MATRIX = json.loads((ROOT / "eval/canonical_scenarios/canonical_route_tool_failure_matrix.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("params", "expected_required"),
    [
        ({}, False),
        ({"require_artifact": True}, True),
        ({"require_tool_evidence": True}, True),
        ({"tool_execution_required": True}, True),
        ({"require_artifact": "true"}, False),
        ({"require_tool_evidence": 1}, False),
        ({"tool_execution_required": ["file_write"]}, False),
    ],
)
def test_tool_evidence_requires_an_unambiguous_task_opt_in(params, expected_required) -> None:
    assert agent_runtime._task_requires_tool_evidence(params, []) is expected_required


def test_profile_tool_capability_alone_does_not_force_a_read_only_side_effect() -> None:
    bundle = load_bundle("genesis-platform-engineer")
    assert bundle["semantically_required_tools"]
    assert agent_runtime._task_requires_tool_evidence({}, []) is False


def test_any_observed_tool_call_remains_completion_evidence() -> None:
    observed = [{"tool_name": "file_write", "ok": False}]
    assert agent_runtime._task_requires_tool_evidence({}, observed) is True


def test_canonical_output_contract_never_synthesizes_missing_fields() -> None:
    bundle = {"canonical_profile": True, "output_shape_hint": ["status", "summary"]}
    verdict = agent_runtime._canonical_output_contract(bundle, '{"status":"HOLD"}')
    assert verdict["ok"] is False
    assert verdict["missing_keys"] == ["summary"]
    assert "summary" not in json.loads(verdict["response"])


def test_pathological_trajectory_catalog_is_complete_and_budgets_match_runtime() -> None:
    expected = {"repeated_llm", "repeated_tool", "duplicate_handoff", "circular_handoff", "agent_ping_pong", "premature_termination", "unnecessary_escalation", "missed_specialist"}
    assert set(MATRIX["trajectory_classes"]) == expected
    budgets = MATRIX["step_budgets"]
    assert budgets == {
        "single_agent_max_turns": agent_runtime.MAX_TURNS,
        "single_agent_max_llm_calls": agent_runtime.MAX_LLM_CALLS,
        "max_files_written": agent_runtime.MAX_FILES_WRITTEN,
    }


@pytest.mark.asyncio
async def test_repeated_tool_trajectory_is_bounded_and_never_returns_success(monkeypatch, tmp_path) -> None:
    tool_calls = 0

    async def repeating_llm(*_a, **_k):
        return {"choices": [{"message": {"content": None, "tool_calls": [{
            "id": "repeat", "type": "function",
            "function": {"name": "code_format", "arguments": '{"content":"x=1"}'},
        }]}}], "usage": {"total_tokens": 1}}

    async def tool(**_kwargs):
        nonlocal tool_calls
        tool_calls += 1
        return {"ok": True, "content": "x = 1\n"}

    monkeypatch.setattr(agent_runtime, "MAX_TURNS", 3)
    monkeypatch.setattr(agent_runtime, "MAX_LLM_CALLS", 3)
    monkeypatch.setattr(AgentRuntime, "_call_llm", repeating_llm)
    monkeypatch.setattr(agent_runtime, "get_tool", lambda _name: tool)
    monkeypatch.setattr(agent_runtime, "tool_schemas_for", lambda _names: [])
    monkeypatch.setattr(agent_runtime, "append_tool_intent", lambda **_k: 1)
    monkeypatch.setattr(agent_runtime, "append_tool_event", lambda **_k: 1)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    result = await AgentRuntime("http://llm.invalid", "redacted")._run_loop(
        {"slug":"genesis-platform-engineer","tools_advertised":["code_format"],"token_budget":100,"system_prompt":"s"},
        "task", {}, "loop-job", tmp_path, None, "loop-session"
    )
    assert result["ok"] is False
    assert result["error"] == "max_turns_reached"
    assert result["turns"] == 3
    assert result["resource_usage"]["llm_calls"] == 3
    # One execution, two deterministic replays: repeated model calls cannot
    # repeat a side effect even when the model reuses the same call forever.
    assert tool_calls == 1


@pytest.mark.asyncio
async def test_repeatability_same_input_has_identical_deterministic_envelope(monkeypatch, tmp_path) -> None:
    async def stable(*_a, **_k):
        return {"choices": [{"message": {"content": "stable", "tool_calls": []}}], "usage": {"total_tokens": 7}}

    monkeypatch.setattr(AgentRuntime, "_call_llm", stable)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    bundle = {"slug":"genesis-data-intelligence","tools_advertised":[],"token_budget":100,"system_prompt":"s"}
    runtime = AgentRuntime("http://llm.invalid", "redacted")
    results = [await runtime._run_loop(bundle, "same", {}, f"job-{i}", tmp_path / str(i), None, f"session-{i}") for i in range(20)]
    assert all(r["ok"] for r in results)
    projection = [(r["response"], r["turns"], r["resource_usage"]) for r in results]
    assert len(set((p[0], p[1], json.dumps(p[2], sort_keys=True)) for p in projection)) == 1


@pytest.mark.asyncio
async def test_fin_ap_invalid_provider_fails_before_canonical_file_write_trajectory(monkeypatch, tmp_path) -> None:
    bundle = load_bundle("genesis-fin-ap")
    prompt = bundle["system_prompt"]
    assert "CANONICAL FILE_WRITE TIMEOUT TRAJECTORY" in prompt
    assert "first action MUST be exactly one file_write call" in prompt
    assert "using the supplied path and content unchanged" in prompt
    assert "do not retry file_write or call another tool" in prompt

    expected_args = {"content": '{"status":"synthetic"}', "path": "eval-output.json"}
    trajectory = []
    observed_tool_args = []
    llm_calls = 0

    async def canonical_timeout_llm(_self, _model, messages, _tools, _budget):
        nonlocal llm_calls
        llm_calls += 1
        trajectory.append(f"llm_{llm_calls}")
        if llm_calls == 1:
            return {
                "choices": [{"message": {"content": None, "tool_calls": [{
                    "id": "canonical-file-write",
                    "type": "function",
                    "function": {
                        "name": "file_write",
                        "arguments": json.dumps(expected_args, separators=(",", ":")),
                    },
                }]}}],
                "usage": {"total_tokens": 1},
            }
        assert '"error": "injected_timeout"' in messages[-1]["content"]
        return {
            "choices": [{"message": {"content": json.dumps({
                "status": "FAILED",
                "summary": "The single canonical file_write attempt timed out; no retry occurred.",
                "tool_calls_summary": [{"name": "file_write", "ok": False, "error": "injected_timeout"}],
            }), "tool_calls": []}}],
            "usage": {"total_tokens": 1},
        }

    async def injected_timeout_tool(**kwargs):
        trajectory.append("file_write")
        observed_tool_args.append({k: v for k, v in kwargs.items() if not k.startswith("_")})
        return {"ok": False, "error": "injected_timeout", "fault_injection": True}

    monkeypatch.setattr(AgentRuntime, "_call_llm", canonical_timeout_llm)
    monkeypatch.setattr(agent_runtime, "get_tool", lambda name: injected_timeout_tool if name == "file_write" else None)
    monkeypatch.setattr(agent_runtime, "tool_schemas_for", lambda _names: [])
    monkeypatch.setattr(agent_runtime, "append_tool_intent", lambda **_k: 1)
    monkeypatch.setattr(agent_runtime, "append_tool_event", lambda **_k: 1)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)

    task = (
        'Use file_write exactly once with these arguments: {"content": '
        '"{\\"status\\":\\"synthetic\\"}", "path": "eval-output.json"}. '
        "A bounded timeout will be injected; report failure without retry or success claims."
    )
    result = await AgentRuntime("http://llm.invalid", "redacted")._run_loop(
        bundle, task, {}, "job-fin-ap-timeout", tmp_path, None, "session-fin-ap-timeout"
    )

    assert trajectory == []
    assert observed_tool_args == []
    assert llm_calls == 0
    assert result["ok"] is False
    assert result["error"] == "output_contract_failed"
    assert result["response"] == ""
    assert result["output_contract_eval"]["reason"] == "canonical_candidate_transport_failed"
