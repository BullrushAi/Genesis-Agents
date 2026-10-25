"""Canonical candidate and judge transports with a single router fallback.

Precedence is fixed:
1. Native Anthropic, exact hostname binding, unchanged.
2. The SwarmSync router at api.swarmsync.ai, one request, same forced tool.
3. Fail closed. The error carries a stage and never includes a response body.
"""
from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlsplit

from canonical_result_contract import CANONICAL_RESULT_TOOL_NAME
from runtime.semantic_safety_judge import (
    CANDIDATE_MODEL,
    CANDIDATE_LOGICAL_MODEL,
    CANDIDATE_READ_TIMEOUT_S,
    JUDGE_MODEL,
    JUDGE_LOGICAL_MODEL,
    REASON_CANDIDATE_TRANSPORT_FAILED,
    REASON_TRANSPORT_FAILED,
    TRANSPORT_CONNECT_WRITE_POOL_TIMEOUT_S,
    ExactAnthropicTransportError,
    build_canonical_candidate_request,
)

SWARMSYNC_ROUTER_ORIGIN = "api.swarmsync.ai"
TRANSPORT_NATIVE = "anthropic-native"
TRANSPORT_ROUTER = "swarmsync-router"

# Router model ids that are the same pinned Haiku the native path requires.
# SwarmSync accepts this id and rejects the bare Anthropic id for the same Haiku.
ROUTER_HAIKU_MODEL = "anthropic/claude-haiku-4-5-20251001"
_HAIKU_MODEL_ALIASES = frozenset({
    CANDIDATE_MODEL,
    CANDIDATE_LOGICAL_MODEL,
    JUDGE_MODEL,
    JUDGE_LOGICAL_MODEL,
    "anthropic/claude-haiku-4-5",
    ROUTER_HAIKU_MODEL,
    "claude-haiku-4-5",
    "claude-haiku-4.5",
})


class CanonicalTransportError(ExactAnthropicTransportError):
    """Fail-closed canonical transport error. The message is a reason code only."""


def exact_swarmsync_router_url(api_url: str) -> bool:
    """True only for the pinned SwarmSync chat-completions endpoint."""
    parsed = urlsplit(api_url or "")
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == SWARMSYNC_ROUTER_ORIGIN
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and not parsed.query
        and not parsed.fragment
        and parsed.path.rstrip("/") == "/v1/chat/completions"
    )


def router_fallback_allowed(
    *, api_url: str, api_key: str, declared_provider: str | None,
) -> bool:
    """Router fallback is allowed only for the pinned host, a key, and Anthropic-or-unset."""
    declared = (declared_provider or "").strip().lower()
    return (
        declared in ("", "anthropic")
        and exact_swarmsync_router_url(api_url)
        and isinstance(api_key, str)
        and bool(api_key.strip())
    )


def _status_class(status_code: int) -> str:
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        return f"{status_code // 100}xx"
    return "unknown"


def _system_text(system: Any) -> str:
    """Anthropic keeps system text off the message list. Chat-completions does not."""
    if isinstance(system, str):
        return system if system.strip() else ""
    if not isinstance(system, list):
        return ""
    parts: list[str] = []
    for block in system:
        if isinstance(block, str) and block.strip():
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
    return "\n\n".join(parts)


def _openai_body(anthropic_request: dict[str, Any]) -> dict[str, Any]:
    tools = []
    for tool in anthropic_request.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": tool.get("name"),
                "description": tool.get("description") or "",
                "parameters": tool.get("input_schema") or {"type": "object"},
            },
        })
    choice = anthropic_request.get("tool_choice") or {}
    tool_name = choice.get("name")
    messages: list[dict[str, Any]] = []
    system_text = _system_text(anthropic_request.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})
    for message in anthropic_request.get("messages") or []:
        if isinstance(message, dict):
            messages.append(message)
    requested_model = anthropic_request.get("model")
    if isinstance(requested_model, str) and requested_model.strip() in _HAIKU_MODEL_ALIASES:
        router_model = ROUTER_HAIKU_MODEL
    else:
        router_model = requested_model or CANDIDATE_MODEL
    body: dict[str, Any] = {
        "model": router_model,
        "messages": messages,
        "temperature": anthropic_request.get("temperature", 0),
        "max_tokens": anthropic_request.get("max_tokens"),
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = {
            "type": "function",
            "function": {"name": tool_name or tools[0]["function"]["name"]},
        }
    return body


async def _post_router(
    body: dict[str, Any], *, api_url: str, api_key: str, read_timeout_s: float, failure_reason: str,
) -> dict[str, Any]:
    """One router POST. Failures stay content-free."""
    import httpx

    if not exact_swarmsync_router_url(api_url):
        raise CanonicalTransportError(
            failure_reason, stage="config_rejected", elapsed_ms=0.0,
        )
    timeout = httpx.Timeout(
        TRANSPORT_CONNECT_WRITE_POOL_TIMEOUT_S,
        connect=TRANSPORT_CONNECT_WRITE_POOL_TIMEOUT_S,
        read=read_timeout_s,
        write=TRANSPORT_CONNECT_WRITE_POOL_TIMEOUT_S,
        pool=TRANSPORT_CONNECT_WRITE_POOL_TIMEOUT_S,
    )
    started = time.monotonic()

    def failed(stage: str, *, status_class: str | None = None) -> CanonicalTransportError:
        return CanonicalTransportError(
            failure_reason,
            stage=stage,
            elapsed_ms=round((time.monotonic() - started) * 1000, 3),
            http_status_class=status_class,
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(api_url, headers=headers, json=body)
    except httpx.ConnectTimeout:
        raise failed("connect_timeout") from None
    except httpx.ReadTimeout:
        raise failed("read_timeout") from None
    except httpx.WriteTimeout:
        raise failed("write_timeout") from None
    except httpx.PoolTimeout:
        raise failed("pool_timeout") from None
    except httpx.HTTPError:
        raise failed("network_error") from None
    if response.status_code not in (200, 201):
        raise failed("http_status_error", status_class=_status_class(response.status_code))
    try:
        payload = response.json()
    except (TypeError, ValueError):
        raise failed("response_json_invalid") from None
    if not isinstance(payload, dict):
        raise failed("response_shape_invalid")
    return payload


def _normalize_haiku_model(payload: dict[str, Any], *, failure_reason: str) -> str:
    reported = payload.get("model")
    if not isinstance(reported, str) or reported.strip() not in _HAIKU_MODEL_ALIASES:
        raise CanonicalTransportError(
            failure_reason, stage="response_model_rejected", elapsed_ms=0.0,
        )
    return CANDIDATE_MODEL


def _openai_tool_call(
    payload: dict[str, Any], *, expected_name: str, failure_reason: str,
) -> dict[str, Any]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise CanonicalTransportError(failure_reason, stage="response_shape_invalid", elapsed_ms=0.0)
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise CanonicalTransportError(failure_reason, stage="response_shape_invalid", elapsed_ms=0.0)
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        raise CanonicalTransportError(failure_reason, stage="response_shape_invalid", elapsed_ms=0.0)
    function = calls[0].get("function")
    if not isinstance(function, dict) or function.get("name") != expected_name:
        raise CanonicalTransportError(failure_reason, stage="response_shape_invalid", elapsed_ms=0.0)
    return calls[0]


async def call_router_canonical_candidate(
    *, api_url: str, api_key: str, slug: str, messages: list[dict[str, Any]], max_tokens: int,
) -> dict[str, Any]:
    """One forced canonical-result call through the SwarmSync router."""
    anthropic_request = build_canonical_candidate_request(
        slug=slug, messages=messages, requested_max_tokens=max_tokens,
    )
    # The schema helper stamps a local-only slug. It must not leave the process.
    for tool in anthropic_request.get("tools") or []:
        if isinstance(tool, dict):
            tool.pop("_genesis_agent_id", None)
    raw = await _post_router(
        _openai_body(anthropic_request),
        api_url=api_url,
        api_key=api_key,
        read_timeout_s=CANDIDATE_READ_TIMEOUT_S,
        failure_reason=REASON_CANDIDATE_TRANSPORT_FAILED,
    )
    model = _normalize_haiku_model(raw, failure_reason=REASON_CANDIDATE_TRANSPORT_FAILED)
    call = _openai_tool_call(
        raw,
        expected_name=CANONICAL_RESULT_TOOL_NAME,
        failure_reason=REASON_CANDIDATE_TRANSPORT_FAILED,
    )
    arguments = call.get("function", {}).get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if isinstance(arguments, dict) else {}, separators=(",", ":"))
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    return {
        "id": raw.get("id"),
        "model": model,
        "transport": TRANSPORT_ROUTER,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call.get("id") or "canonical-result",
                    "type": "function",
                    "function": {
                        "name": CANONICAL_RESULT_TOOL_NAME,
                        "arguments": arguments,
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": usage,
    }


def _anthropic_judge_payload(raw: dict[str, Any], *, tool_name: str) -> dict[str, Any]:
    model = raw.get("model")
    normalized = model if isinstance(model, str) and model.strip() in _HAIKU_MODEL_ALIASES else model
    if normalized in _HAIKU_MODEL_ALIASES:
        normalized = JUDGE_MODEL
    call = _openai_tool_call(raw, expected_name=tool_name, failure_reason=REASON_TRANSPORT_FAILED)
    arguments = call.get("function", {}).get("arguments")
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (TypeError, ValueError):
        raise CanonicalTransportError(
            REASON_TRANSPORT_FAILED, stage="response_json_invalid", elapsed_ms=0.0,
        ) from None
    if not isinstance(parsed, dict):
        raise CanonicalTransportError(
            REASON_TRANSPORT_FAILED, stage="response_shape_invalid", elapsed_ms=0.0,
        )
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    return {
        "model": normalized,
        "stop_reason": "tool_use",
        "content": [{
            "type": "tool_use",
            "id": call.get("id") or "judge",
            "name": tool_name,
            "input": parsed,
        }],
        "usage": {
            "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
            "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
        },
    }


async def call_router_judge(
    request: dict[str, Any], *, api_url: str, api_key: str,
) -> dict[str, Any]:
    """One judge call through the router, returned in the Anthropic shape the judge consumes."""
    tool_name = ""
    choice = request.get("tool_choice") or {}
    if isinstance(choice, dict):
        tool_name = str(choice.get("name") or "")
    raw = await _post_router(
        _openai_body(request),
        api_url=api_url,
        api_key=api_key,
        read_timeout_s=CANDIDATE_READ_TIMEOUT_S,
        failure_reason=REASON_TRANSPORT_FAILED,
    )
    return _anthropic_judge_payload(raw, tool_name=tool_name)


def config_rejected(reason: str) -> CanonicalTransportError:
    return CanonicalTransportError(reason, stage="config_rejected", elapsed_ms=0.0)
