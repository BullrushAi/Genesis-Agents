from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import agent_runtime
from agent_runtime import AgentRuntime
from bundle_loader import load_bundle
from runtime.tool_policy import HUMAN_ONLY_TOOLS, PROHIBITED_TOOLS, check_tool_policy
from tools import register_default_tools, tool_schemas_for, validate_tool_arguments
from tools.genesis_call_tool import genesis_call


ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads(
    (ROOT / "proof-artifacts/canonical-tool-policy-decision/policy_matrix.json").read_text(
        encoding="utf-8"
    )
)
TOOL_CASES = [
    json.loads(line)
    for line in (ROOT / "eval/canonical/genesis_tool_matrix.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
]


def _bundle(*, tool: str = "code_format") -> dict:
    return {
        "slug": "genesis-platform-engineer",
        "system_prompt": "Use only the requested bounded tool and report its real outcome.",
        "tools_advertised": [tool],
        "token_budget": 1000,
        "timeout_s": 30,
        "success_criteria": None,
    }


def _tool_call(call_id: str, arguments: object, *, tool: str = "code_format") -> dict:
    encoded = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": tool, "arguments": encoded},
    }


async def _run_responses(monkeypatch, tmp_path: Path, responses: list[dict], tool) -> dict:
    async def llm(*_args, **_kwargs):
        return responses.pop(0)

    monkeypatch.setattr(AgentRuntime, "_call_llm", llm)
    monkeypatch.setattr(agent_runtime, "get_tool", lambda name: tool if name == "code_format" else None)
    monkeypatch.setattr(agent_runtime, "append_tool_intent", lambda **_kwargs: 1)
    monkeypatch.setattr(agent_runtime, "append_tool_event", lambda **_kwargs: 1)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_args, **_kwargs: None)
    return await AgentRuntime("http://llm.invalid", "redacted")._run_loop(
        _bundle(), "bounded test", {}, "proof-job", tmp_path, None, "proof-session"
    )


def test_all_51_canonical_tool_cases_and_all_19_policy_rows_are_closed() -> None:
    register_default_tools()
    assert len(TOOL_CASES) == 51
    assert len(POLICY["agents"]) == 19
    assert {case["metadata"]["tool_outcome"] for case in TOOL_CASES} == {
        "positive",
        "negative",
        "failure",
    }
    for row in POLICY["agents"]:
        slug = row["slug"]
        bundle = load_bundle(slug)
        assert bundle is not None
        exposed = [
            name
            for name in bundle["tools_advertised"]
            if check_tool_policy(slug, name)["ok"]
        ]
        assert exposed == row["allowed_effective_target"]
        assert [schema["function"]["name"] for schema in tool_schemas_for(exposed)] == exposed
        assert all(
            schema["function"]["parameters"]["additionalProperties"] is False
            for schema in tool_schemas_for(exposed)
        )
        assert all(not check_tool_policy(slug, name)["ok"] for name in PROHIBITED_TOOLS)
        assert all(not check_tool_policy(slug, name)["ok"] for name in HUMAN_ONLY_TOOLS)


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ({}, "missing_required"),
        ({"content": "x=1", "_job_dir": "attacker"}, "unexpected_arguments"),
        ({"content": "x=1", "language": "ruby"}, "enum_violation"),
        ({"content": 7}, "wrong_type"),
        (["x=1"], "arguments_must_be_object"),
    ],
)
def test_required_prohibited_type_and_enum_arguments_fail_closed(arguments, reason) -> None:
    register_default_tools()
    refusal = validate_tool_arguments("code_format", arguments)
    assert refusal is not None
    assert refusal["error"] == "invalid_tool_arguments"
    assert refusal["reason"] == reason


@pytest.mark.asyncio
async def test_identical_tool_calls_are_executed_once_and_replayed_deterministically(
    monkeypatch, tmp_path
) -> None:
    executions = 0

    async def tool(**_kwargs):
        nonlocal executions
        executions += 1
        return {"ok": True, "content": "x = 1\n"}

    responses = [
        {
            "choices": [{"message": {"content": None, "tool_calls": [
                _tool_call("first", {"content": "x=1"}),
                _tool_call("second", {"content": "x=1"}),
            ]}}],
            "usage": {"total_tokens": 1},
        },
        {"choices": [{"message": {"content": "done", "tool_calls": []}}], "usage": {"total_tokens": 1}},
    ]
    result = await _run_responses(monkeypatch, tmp_path, responses, tool)
    assert executions == 1
    calls = result["trace"]["tool_calls"]
    assert len(calls) == 2 and calls[0]["ok"] and calls[1]["ok"]
    assert calls[1]["deduplicated"] is True
    assert calls[1]["duplicate_of_tool_call_id"] == "first"


@pytest.mark.asyncio
async def test_reused_tool_call_id_with_different_arguments_is_a_conflict(
    monkeypatch, tmp_path
) -> None:
    received: list[str] = []

    async def tool(**kwargs):
        received.append(kwargs["content"])
        return {"ok": True, "content": kwargs["content"]}

    responses = [
        {
            "choices": [{"message": {"content": None, "tool_calls": [
                _tool_call("same-id", {"content": "first"}),
                _tool_call("same-id", {"content": "conflict"}),
            ]}}],
            "usage": {"total_tokens": 1},
        },
        {"choices": [{"message": {"content": "conflict reported", "tool_calls": []}}], "usage": {"total_tokens": 1}},
    ]
    result = await _run_responses(monkeypatch, tmp_path, responses, tool)
    assert received == ["first"]
    assert result["ok"] is False
    assert result["error"] == "tool_call_failed"
    assert "tool_call_id_conflict" in result["trace"]["tool_calls"][1]["result_summary"]


@pytest.mark.asyncio
async def test_failed_tool_outcome_cannot_be_synthesized_as_parent_success(monkeypatch, tmp_path) -> None:
    async def failed(**_kwargs):
        return {"ok": False, "error": "provider_unavailable"}

    responses = [
        {"choices": [{"message": {"content": None, "tool_calls": [
            _tool_call("failed", {"content": "x=1"})
        ]}}], "usage": {"total_tokens": 1}},
        {"choices": [{"message": {"content": "success", "tool_calls": []}}], "usage": {"total_tokens": 1}},
    ]
    result = await _run_responses(monkeypatch, tmp_path, responses, failed)
    assert result["ok"] is False
    assert result["error"] == "success_criteria_failed"
    assert result["trace"]["tool_calls"][0]["ok"] is False


@pytest.mark.asyncio
async def test_three_agent_parallel_handoff_preserves_context_evidence_and_order() -> None:
    class Runtime:
        def __init__(self):
            self.started: list[str] = []
            self.release = asyncio.Event()

        async def execute_agent(self, slug, task, params, **kwargs):
            self.started.append(slug)
            if len(self.started) == 2:
                self.release.set()
            await asyncio.wait_for(self.release.wait(), timeout=1)
            return {
                "ok": True,
                "response": f"evidence:{slug}:{task}:{params['evidence_ref']}",
                "resource_usage": {"total_tokens": 1},
                "trace": {"session_id": kwargs["session_id"], "evidence_refs": [params["evidence_ref"]]},
            }

        def record_delegated_spend(self, *_args, **_kwargs):
            return None

    runtime = Runtime()
    common = {
        "_runtime": runtime,
        "_parent_job_id": "parent",
        "_session_id": "parent-session",
        "_parent_agent_slug": "genesis-meta",
        "_delegation_chain": (),
        "_delegation_depth": 0,
        "_remaining_token_budget": 10,
        "_remaining_cost_budget_cents": 0,
    }
    research, finance = await asyncio.gather(
        genesis_call(agent="genesis-research", task="research", params={"evidence_ref": "E1"}, **common),
        genesis_call(agent="genesis-finance", task="finance", params={"evidence_ref": "E2"}, **common),
    )
    assert runtime.started == ["genesis-research", "genesis-finance"]
    assert research["child_ok"] and finance["child_ok"]
    assert research["child_trace"]["evidence_refs"] == ["E1"]
    assert finance["child_trace"]["evidence_refs"] == ["E2"]
    assert "evidence:genesis-research:research:E1" in research["child_response_summary"]
    assert "evidence:genesis-finance:finance:E2" in finance["child_response_summary"]


@pytest.mark.asyncio
async def test_circular_and_ping_pong_delegation_are_refused_before_dispatch() -> None:
    class Runtime:
        calls = 0

        async def execute_agent(self, *_args, **_kwargs):
            self.calls += 1
            return {"ok": True}

    runtime = Runtime()
    result = await genesis_call(
        agent="genesis-research",
        task="loop",
        _runtime=runtime,
        _parent_agent_slug="genesis-meta",
        _delegation_chain=("genesis-research",),
        _delegation_depth=1,
    )
    assert result == {
        "ok": False,
        "error": "delegation_cycle_detected",
        "target_agent_slug": "genesis-research",
    }
    assert runtime.calls == 0
