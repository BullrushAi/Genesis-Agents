"""Router fallback stays pinned to api.swarmsync.ai and fails closed everywhere else."""
from __future__ import annotations

import pytest

from capability_cards import card_for
from runtime.router_canonical import (
    exact_swarmsync_router_url,
    router_fallback_allowed,
)
from runtime.semantic_safety_judge import require_canonical_native_anthropic_config


@pytest.mark.parametrize(
    "url",
    [
        "https://api.swarmsync.ai/v1/chat/completions",
        "https://api.swarmsync.ai/v1/chat/completions/",
        "https://api.swarmsync.ai:443/v1/chat/completions",
    ],
)
def test_router_url_accepts_only_the_pinned_endpoint(url):
    assert exact_swarmsync_router_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://api.swarmsync.ai/v1/chat/completions",
        "https://api.swarmsync.ai.example.invalid/v1/chat/completions",
        "https://user:secret@api.swarmsync.ai/v1/chat/completions",
        "https://api.swarmsync.ai/v1/messages",
        "https://api.anthropic.com/v1/messages",
        "https://router.invalid/v1/chat/completions",
    ],
)
def test_router_url_rejects_anything_else(url):
    assert exact_swarmsync_router_url(url) is False
    assert router_fallback_allowed(api_url=url, api_key="redacted", declared_provider="") is False


def test_native_gate_still_rejects_the_router_host():
    with pytest.raises(RuntimeError):
        require_canonical_native_anthropic_config(
            api_url="https://api.swarmsync.ai/v1/chat/completions",
            api_key="redacted",
            declared_provider="anthropic",
        )


def test_platform_engineer_card_lists_every_advertised_tool():
    card = card_for("genesis-platform-engineer")
    assert card is not None
    tools = {item["tool"]: item for item in card["capabilities"]}
    assert set(tools) == {"file_write", "code_format", "run_code"}
    assert all("dependency_ready" in item for item in tools.values())
    assert card["analysis_only"] is False
    assert card["pricing"]["total_cents"] == 60


def test_finance_card_uses_the_half_dollar_price():
    card = card_for("genesis-fin-revenue")
    assert card is not None
    assert card["pricing"]["total_cents"] == 50
    assert card["analysis_only"] is False


def test_router_openai_body_keeps_the_split_system_prompt():
    """Anthropic puts system text on a top-level field. The router must still receive it."""
    from runtime.router_canonical import _openai_body
    from runtime.semantic_safety_judge import (
        CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION,
        CANDIDATE_MODEL,
        build_canonical_candidate_request,
    )

    request = build_canonical_candidate_request(
        slug="genesis-platform-engineer",
        messages=[
            {"role": "system", "content": "AGENT_PERSONA_MARKER"},
            {"role": "user", "content": "USER_MARKER"},
        ],
        requested_max_tokens=4096,
    )
    assert request.get("system")
    assert request.get("model") == CANDIDATE_MODEL
    body = _openai_body(request)
    system_text = "\n\n".join(
        str(message.get("content") or "")
        for message in body["messages"]
        if message.get("role") == "system"
    )
    assert "AGENT_PERSONA_MARKER" in system_text
    assert CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION in system_text
    assert any(
        message.get("role") == "user" and "USER_MARKER" in str(message.get("content") or "")
        for message in body["messages"]
    )
    assert body["model"] == "anthropic/claude-haiku-4-5-20251001"


def test_router_judge_uses_the_candidate_read_budget(monkeypatch):
    """The native judge budget is 5s. This router needs longer to return one tool call."""
    import asyncio
    import json

    from runtime.router_canonical import call_router_judge
    from runtime.semantic_safety_judge import CANDIDATE_READ_TIMEOUT_S, JUDGE_MODEL

    seen = {}

    async def fake_post(body, *, api_url, api_key, read_timeout_s, failure_reason):
        seen["timeout"] = read_timeout_s
        seen["model"] = body.get("model")
        return {
            "model": "anthropic/claude-haiku-4-5-20251001",
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "id": "judge-1",
                        "type": "function",
                        "function": {
                            "name": "submit_genesis_safety_judgment_v1",
                            "arguments": json.dumps({"decision": "allow"}),
                        },
                    }],
                },
            }],
        }

    monkeypatch.setattr("runtime.router_canonical._post_router", fake_post)
    result = asyncio.run(call_router_judge(
        {
            "model": "claude-haiku-4-5-20251001",
            "system": "policy",
            "messages": [{"role": "user", "content": "review"}],
            "tools": [{
                "name": "submit_genesis_safety_judgment_v1",
                "input_schema": {"type": "object"},
            }],
            "tool_choice": {"name": "submit_genesis_safety_judgment_v1"},
            "max_tokens": 256,
        },
        api_url="https://api.swarmsync.ai/v1/chat/completions",
        api_key="test-key",
    ))
    assert seen["timeout"] == CANDIDATE_READ_TIMEOUT_S
    assert seen["model"] == "anthropic/claude-haiku-4-5-20251001"
    assert result["model"] == JUDGE_MODEL
    assert result["stop_reason"] == "tool_use"
