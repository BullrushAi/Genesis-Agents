from __future__ import annotations


import asyncio


import json


import sys


from pathlib import Path


from unittest.mock import AsyncMock, patch


import pytest


import agent_runtime as agent_runtime_module  # noqa: E402


from agent_runtime import AgentRuntime  # noqa: E402


from bundle_loader import load_bundle  # noqa: E402


from canonical_result_contract import canonical_result_tool_schema  # noqa: E402


from runtime.anthropic_wire import ExactForcedToolResponseError  # noqa: E402


from runtime.semantic_safety_judge import (  # noqa: E402
    CANDIDATE_MODEL,
    ExactAnthropicTransportError,
    JUDGE_MODEL,
    SafetyJudgeVerdict,
    project_candidate_narrative,
    role_contract_metadata,
)


@pytest.fixture(autouse=True)
def hermetic_audit(tmp_path, monkeypatch):
    from runtime import genesis_audit

    monkeypatch.setenv("GENESIS_AUDIT_DB_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setattr(genesis_audit, "_instance", None, raising=False)
    yield
    genesis_audit._instance = None


def _bundle(tool_name: str = "file_write") -> dict:
    return {
        "slug": "genesis-platform-engineer",
        "system_prompt": "Build safely.",
        "tools_advertised": [tool_name],
        "token_budget": 1000,
        "model_hint": "auto",
        "success_criteria": None,
        "timeout_s": 30,
    }


def _tool_then_answer(tool_name: str = "file_write") -> list[dict]:
    arguments = (
        {"query": "Genesis observability contract", "num_results": 1}
        if tool_name == "web_search"
        else {"path": "observability-proof.txt", "content": "contract proof"}
    )
    return [
        {
            "choices": [{"message": {
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps(arguments)},
                }],
            }}],
            "model": "test-model",
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            "_genesis_observability": {
                "attempt_count": 2,
                "fallback_used": True,
                "candidate_count": 3,
            },
        },
        {
            "choices": [{"message": {"content": "done", "tool_calls": []}}],
            "model": "test-model",
            "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
            "_genesis_observability": {
                "attempt_count": 1,
                "fallback_used": False,
                "candidate_count": 3,
            },
        },
    ]


def test_local_rejection_overrides_judge_allow_without_mutating_candidate(
    tmp_path, monkeypatch,
):
    slug = "genesis-platform-engineer"
    candidate = {
        "agent_id": slug, "task_id": "task", "scope": "bounded",
        "status": "ANALYSIS_COMPLETE",
        "summary": "ANALYSIS: Deployment completed and independently verified.",
        "findings": [], "proposals": [], "evidence_refs": [], "assumptions": [],
        "conflicts": [], "unknowns": [], "confidence": 0.9, "risk_flags": [],
        "recommended_next_actions": [], "requested_human_decisions": [],
        "tool_calls_summary": [], "clarification_needed": False,
        "candidate_resolution": "", "reason_human_needed": "",
        "recommended_human_resolver": "", "materiality": None, "urgency": "",
        "resolution_attempts": [], "supporting_evidence_refs": [],
        "competing_state_refs": [], "recommended_answer_options": [],
    }
    provider_candidate = {
        key: value for key, value in candidate.items()
        if key not in {
            "clarification_needed", "candidate_resolution", "reason_human_needed",
            "recommended_human_resolver", "materiality", "urgency",
            "resolution_attempts", "supporting_evidence_refs",
            "competing_state_refs", "recommended_answer_options",
        }
    }
    provider_candidate["clarification"] = None
    provider_snapshot = json.loads(json.dumps(provider_candidate))
    projection = project_candidate_narrative(candidate)
    response = {
        "model": CANDIDATE_MODEL,
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {"content": None, "tool_calls": [{
                "id": "result", "type": "function",
                "function": {
                    "name": "submit_genesis_result_v5",
                    "arguments": json.dumps(provider_candidate),
                },
            }]},
        }],
        "usage": {"total_tokens": 10},
    }

    async def candidate_call(*_args, **_kwargs):
        return response

    async def allow(*_args, **_kwargs):
        return SafetyJudgeVerdict(
            True, None, "ALLOW", "NONE", (), projection.candidate_sha256,
            projection.projection_sha256, projection.projection_bytes, JUDGE_MODEL,
            10, 2, 0.00002, "estimated", "anthropic-public-2025-11-01-v1",
        )

    monkeypatch.setenv("GENESIS_LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(agent_runtime_module, "emit_event", lambda *_a, **_k: None)
    runtime = AgentRuntime("https://api.anthropic.com/v1/messages", "redacted-test-key")
    with (
        patch.object(runtime, "_call_llm", side_effect=candidate_call),
        patch.object(agent_runtime_module, "judge_candidate_semantics", side_effect=allow),
    ):
        result = asyncio.run(runtime._run_loop(
            load_bundle(slug), "bounded", {}, "job-semantic-order", tmp_path,
            None, "session-semantic-order",
        ))
    assert result["ok"] is False
    bindings = role_contract_metadata(slug)
    assert provider_candidate == provider_snapshot


def test_async_trace_carrier_is_never_model_visible():
    carrier = {"traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"}
    _context, clean = agent_runtime_module._deployment_side_effect_authorized({
        "business_field": "allowed",
        "_phoenix_trace_context": carrier,
    })
    assert clean == {"business_field": "allowed"}


def test_denied_advertised_tools_do_not_force_impossible_evidence(tmp_path):
    """Regression for the effective-tools HIGH finding from the launch gate."""
    runtime = AgentRuntime("https://router.invalid", "router-key")

    async def final_answer(*_args, **_kwargs):
        return {
            "choices": [{"message": {"content": "safe answer", "tool_calls": []}}],
            "model": "test-model",
            "usage": {"total_tokens": 3},
        }

    with (
        patch.object(runtime, "_call_llm_inner", side_effect=final_answer),
        patch.object(agent_runtime_module, "check_tool_policy", return_value={"ok": False}),
    ):
        result = asyncio.run(runtime._run_loop(
            _bundle("file_write"), "answer without denied tools", {},
            "job-denied-tools", tmp_path, None, "session-denied-tools",
        ))

    assert result["ok"] is True
    assert result["success_criteria_eval"] == {"ok": True, "failed": []}


@pytest.mark.parametrize(
    ("params", "expected_ok", "expected_required"),
    [
        ({}, True, False),
        ({"require_artifact": True}, False, True),
        ({"require_artifact": "true"}, True, False),
    ],
)
def test_semantic_profile_tools_obey_task_level_evidence_opt_in(
    tmp_path, params, expected_ok, expected_required
):
    runtime = AgentRuntime("https://router.invalid", "router-key")

    async def final_answer(*_args, **_kwargs):
        return {
            "choices": [{"message": {"content": "safe read-only answer", "tool_calls": []}}],
            "model": "test-model",
            "usage": {"total_tokens": 3},
        }

    bundle = _bundle("file_write")
    bundle["semantically_required_tools"] = ["file_write"]
    with patch.object(runtime, "_call_llm_inner", side_effect=final_answer):
        result = asyncio.run(runtime._run_loop(
            bundle, "bounded task", params, "job-semantic-opt-in", tmp_path,
            None, "session-semantic-opt-in",
        ))

    assert result["ok"] is expected_ok


@pytest.mark.parametrize(
    ("policy", "delegated_allowed_risks", "expected_error"),
    [
        ({"ok": False, "risk_class": "filesystem_write"}, None, "tool_policy_denied"),
        ({"ok": True, "risk_class": "filesystem_write"}, frozenset({"read_only"}),
         "delegation_privilege_escalation"),
    ],
)
def test_hidden_model_tool_call_cannot_bypass_dispatch_policy(
    tmp_path, policy, delegated_allowed_risks, expected_error
):
    """A model may emit a tool omitted from schemas; dispatcher remains authoritative."""
    runtime = AgentRuntime("https://router.invalid", "router-key")
    responses = _tool_then_answer("file_write")

    async def fake_llm(*_args, **_kwargs):
        return responses.pop(0)

    forbidden_tool = AsyncMock(return_value={"ok": True})
    with (
        patch.object(runtime, "_call_llm_inner", side_effect=fake_llm),
        patch.object(agent_runtime_module, "get_tool", return_value=forbidden_tool),
        patch.object(agent_runtime_module, "check_tool_policy", return_value=policy),
    ):
        result = asyncio.run(runtime._run_loop(
            _bundle("file_write"), "malicious hidden call", {}, "job-hidden-call",
            tmp_path, None, "session-hidden-call",
            delegated_allowed_risks=delegated_allowed_risks,
        ))

    forbidden_tool.assert_not_awaited()
    call = result["trace"]["tool_calls"][0]
    assert call["ok"] is False
    assert expected_error in call["result_summary"]
