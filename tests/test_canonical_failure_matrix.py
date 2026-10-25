from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import agent_runtime
from agent_runtime import AgentRuntime
from bundle_loader import load_bundle
from eval.canonical.evaluators import evaluate_genesis_business_rules


NO_TOOL_BUNDLE = {
    "slug": "genesis-data-intelligence",
    "tools_advertised": [],
    "token_budget": 1000,
    "conduit_budget_cents": 0,
    "timeout_s": 10,
    "system_prompt": "bounded test prompt",
    "success_criteria": [{"type": "non_empty"}],
}

CANONICAL_BUNDLE = {
    **NO_TOOL_BUNDLE,
    "canonical_profile": True,
    "output_shape_hint": ["status", "summary"],
}


async def _run(monkeypatch, tmp_path: Path, response_or_error):
    async def injected(*_args, **_kwargs):
        if isinstance(response_or_error, BaseException):
            raise response_or_error
        return response_or_error

    monkeypatch.setattr(AgentRuntime, "_call_llm", injected)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    return await AgentRuntime("http://llm.invalid", "redacted")._run_loop(
        dict(NO_TOOL_BUNDLE), "task", {}, "job-failure", tmp_path, None, "session-failure"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("prose", "response_not_json_object"),
        ('["array"]', "response_not_json_object"),
        ('{"status":"HOLD"}', "missing_required_keys"),
    ],
)
async def test_canonical_runtime_fails_closed_on_output_contract_violation(
    monkeypatch, tmp_path, content, reason
) -> None:
    async def injected(*_args, **_kwargs):
        return {"choices": [{"message": {"content": content, "tool_calls": []}}], "usage": {}}

    monkeypatch.setattr(AgentRuntime, "_call_llm", injected)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    result = await AgentRuntime("http://llm.invalid", "redacted")._run_loop(
        dict(CANONICAL_BUNDLE), "task", {}, "job-contract", tmp_path, None, "session-contract"
    )
    assert result["ok"] is False
    assert result["error"] == "output_contract_failed"
    assert result["output_contract_eval"]["reason"] == reason
    assert "response" not in result["output_contract_eval"]


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    asyncio.TimeoutError("timeout"),
    ConnectionError("connection refused"),
    RuntimeError("LLM HTTP 401: unauthorized"),
    RuntimeError("LLM HTTP 429: rate limited"),
    RuntimeError("LLM HTTP 500: unavailable"),
])
async def test_transport_auth_rate_limit_and_server_failures_do_not_become_success(monkeypatch, tmp_path, exc) -> None:
    result = await _run(monkeypatch, tmp_path, exc)
    assert result["ok"] is False
    assert result["error"] == "llm_call_failed"
    assert result["type"] == type(exc).__name__


@pytest.mark.asyncio
@pytest.mark.parametrize(("payload", "expected"), [
    ({}, "no_choices_in_llm_response"),
    ({"choices": []}, "no_choices_in_llm_response"),
    ({"choices": [{}], "usage": {}}, "success_criteria_failed"),
    ({"choices": [{"message": {"content": "", "tool_calls": []}}]}, "success_criteria_failed"),
])
async def test_empty_partial_and_structurally_incomplete_responses_fail_closed(monkeypatch, tmp_path, payload, expected) -> None:
    result = await _run(monkeypatch, tmp_path, payload)
    assert result["ok"] is False
    assert result["error"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], "not-json-object"])
async def test_non_mapping_model_responses_fail_closed_without_success(monkeypatch, tmp_path, payload) -> None:
    result = await _run(monkeypatch, tmp_path, payload)
    assert result["ok"] is False
    assert result["error"] == "llm_call_failed"
    assert result["type"] == "AttributeError"
    assert "has no attribute 'get'" in result["message"]


@pytest.mark.asyncio
async def test_malformed_tool_arguments_are_not_interpreted_as_valid_arguments(monkeypatch, tmp_path) -> None:
    calls = {"llm": 0, "tool": 0, "received": None}

    async def fake_llm(*_a, **_k):
        calls["llm"] += 1
        if calls["llm"] == 1:
            return {"choices": [{"message": {"content": None, "tool_calls": [{
                "id": "bad-args", "type": "function",
                "function": {"name": "code_format", "arguments": "{not-json"},
            }]}}], "usage": {"total_tokens": 1}}
        return {"choices": [{"message": {"content": "recovered", "tool_calls": []}}], "usage": {"total_tokens": 1}}

    async def fake_tool(**kwargs):
        calls["tool"] += 1
        calls["received"] = {k: v for k, v in kwargs.items() if not k.startswith("_")}
        return {"ok": False, "error": "validation_failed"}

    monkeypatch.setattr(AgentRuntime, "_call_llm", fake_llm)
    monkeypatch.setattr(agent_runtime, "get_tool", lambda name: fake_tool if name == "code_format" else None)
    monkeypatch.setattr(agent_runtime, "tool_schemas_for", lambda names: [])
    monkeypatch.setattr(agent_runtime, "append_tool_intent", lambda **_k: 1)
    monkeypatch.setattr(agent_runtime, "append_tool_event", lambda **_k: 1)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    bundle = dict(NO_TOOL_BUNDLE, slug="genesis-platform-engineer", tools_advertised=["code_format"])
    result = await AgentRuntime("http://llm.invalid", "redacted")._run_loop(
        bundle, "format", {}, "job-malformed", tmp_path, None, "session-malformed"
    )
    assert calls == {"llm": 2, "tool": 0, "received": None}
    assert result["ok"] is False
    assert result["error"] == "success_criteria_failed"
    assert result["trace"]["tool_calls"][0]["ok"] is False
    assert "invalid_tool_arguments" in result["trace"]["tool_calls"][0]["result_summary"]


@pytest.mark.asyncio
async def test_tool_exception_is_returned_to_model_and_recovery_does_not_hallucinate_tool_success(monkeypatch, tmp_path) -> None:
    calls = 0
    observed_messages = []

    async def fake_llm(_self, _model, messages, _tools, _budget):
        nonlocal calls
        calls += 1
        observed_messages.append(list(messages))
        if calls == 1:
            return {"choices": [{"message": {"content": None, "tool_calls": [{
                "id": "boom", "type": "function",
                "function": {"name": "code_format", "arguments": '{"content":"x=1","language":"python"}'},
            }]}}], "usage": {"total_tokens": 1}}
        return {"choices": [{"message": {"content": "dependency failed; no formatted artifact produced", "tool_calls": []}}], "usage": {"total_tokens": 1}}

    async def broken_tool(**_kwargs):
        raise ConnectionError("formatter unavailable")

    monkeypatch.setattr(AgentRuntime, "_call_llm", fake_llm)
    monkeypatch.setattr(agent_runtime, "get_tool", lambda _name: broken_tool)
    monkeypatch.setattr(agent_runtime, "tool_schemas_for", lambda _names: [])
    monkeypatch.setattr(agent_runtime, "append_tool_intent", lambda **_k: 1)
    monkeypatch.setattr(agent_runtime, "append_tool_event", lambda **_k: 1)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    bundle = dict(NO_TOOL_BUNDLE, slug="genesis-platform-engineer", tools_advertised=["code_format"])
    result = await AgentRuntime("http://llm.invalid", "redacted")._run_loop(
        bundle, "format", {}, "job-tool-fail", tmp_path, None, "session-tool-fail"
    )
    tool_message = observed_messages[1][-1]
    assert '"error": "tool_exception"' in tool_message["content"]
    assert result["ok"] is False
    assert result["error"] == "success_criteria_failed"
    assert result["trace"]["tool_calls"][0]["ok"] is False


def test_commercial_growth_analysis_complete_contract_requires_real_evidence_reference() -> None:
    bundle = load_bundle("genesis-commercial-growth")
    prompt = bundle["system_prompt"]
    assert "ANALYSIS EVIDENCE INVARIANT" in prompt
    assert "evidence_refs MUST be a non-empty JSON array" in prompt
    assert "Never invent an evidence marker or source" in prompt
    assert "MUST NOT use ANALYSIS_COMPLETE" in prompt

    case = {
        "expected_output": {
            "business_rules": {
                "cato_dispatch_only": True,
                "evidence_required_for_completion": True,
                "finance_proposal_only": False,
                "fail_closed_on_conflict": True,
            }
        },
        "metadata": {"category": "happy"},
    }
    unsupported = evaluate_genesis_business_rules(
        case,
        {"output": {"status": "ANALYSIS_COMPLETE", "evidence_refs": []}, "tool_calls": []},
    )
    supported = evaluate_genesis_business_rules(
        case,
        {
            "output": {"status": "ANALYSIS_COMPLETE", "evidence_refs": ["source:experiment-brief-v1"]},
            "tool_calls": [],
        },
    )
    unsupported_gate = next(row for row in unsupported if row.name == "evidence_required_for_completion")
    supported_gate = next(row for row in supported if row.name == "evidence_required_for_completion")
    assert unsupported_gate.passed is False
    assert supported_gate.passed is True
