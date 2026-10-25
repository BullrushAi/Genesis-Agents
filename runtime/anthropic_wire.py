"""Wire-format translation for the Anthropic Messages API.

Genesis's agent loop keeps its conversation history in the OpenAI shape: assistant turns carry
``tool_calls``, and every tool result is its own ``{"role": "tool"}`` message. The Anthropic
Messages API has no ``tool`` role — it takes ``tool_use`` content blocks on the assistant turn
and ``tool_result`` blocks inside the *next user* turn. Posting the OpenAI shape verbatim
returns ``400 invalid_request_error: messages: Unexpected role "tool"``, which kills every
tool-using conversation on its second turn.

DUPLICATION, DELIBERATE AND FLAGGED: Cato solves the identical problem in
``cato/model_policy.py::to_anthropic_messages`` (applied at its wire boundary in
``build_request_payload``). This module mirrors that approach rather than importing it — the two
repos deploy independently and share no package — so the same bug now has two implementations
that must be kept in step. If a third consumer appears, extract a shared package instead of
copying this a third time.

Design rule, same as Cato's: selection and history stay in ONE canonical shape (OpenAI), and
only the outbound provider payload is normalised, at the last point before the wire, so no
caller can bypass it.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from canonical_result_contract import (
    CANONICAL_AGENT_IDS,
    CANONICAL_RESULT_TOOL_NAME,
    CLARIFICATION_FIELDS,
    COMMON_RESULT_FIELDS,
    FINANCE_AGENT_IDS,
)

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_HOST_MARKERS = ("api.anthropic.com",)

# Gateway-style ids (what the 24 bundles carry in `model_hint`) -> real Anthropic API ids.
# Verified against GET https://api.anthropic.com/v1/models with the live key: Anthropic rejects
# the `anthropic/` vendor prefix and the undated `claude-sonnet-4-5` alias outright, so this
# translation is required, not cosmetic.
MODEL_ID_TRANSLATIONS: dict[str, str] = {
    "anthropic/claude-sonnet-4-5": "claude-sonnet-4-5-20250929",
    "anthropic/claude-haiku-4-5": "claude-haiku-4-5-20251001",
    "anthropic/claude-opus-4-5": "claude-opus-4-5-20251101",
    "anthropic/claude-sonnet-5": "claude-sonnet-5",
    "anthropic/claude-opus-5": "claude-opus-5",
    "anthropic/claude-fable-5": "claude-fable-5",
    # Undated aliases the gateway accepted but the API does not.
    "claude-sonnet-4-5": "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5": "claude-haiku-4-5-20251001",
    "claude-opus-4-5": "claude-opus-4-5-20251101",
}

# Ids confirmed present on GET /v1/models for this account. Used to decide whether an untranslated
# id is safe to pass through.
KNOWN_ANTHROPIC_MODEL_IDS: frozenset[str] = frozenset({
    "claude-opus-5", "claude-sonnet-5", "claude-fable-5",
    "claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-4-6", "claude-opus-4-6",
    "claude-opus-4-5-20251101", "claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929",
})

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"

# Provider-specific fallback chain. The OpenAI-path fallbacks (the old free-tier
# gateway ids) are gateway ids that mean nothing to Anthropic and could only ever
# produce a second 404 — a fallback that cannot work is worse than none, because it hides the
# real error behind the last one. These are real ids from the list above.
ANTHROPIC_FALLBACK_MODELS: tuple[str, ...] = (
    "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5-20251001",
)

ANTHROPIC_PRICING_VERSION = "anthropic-public-2025-11-01-v1"
# USD per million input/output tokens.  Estimates are emitted only for exact,
# version-pinned model ids and explicit provider usage; they are not invoices.
_ANTHROPIC_PRICING_USD_PER_MILLION: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-5-20250929": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-opus-4-5-20251101": (5.0, 25.0),
}


def estimate_anthropic_cost(model: str, usage: Any) -> dict[str, Any]:
    """Estimate cost from explicit usage and a versioned exact-model price map."""
    result: dict[str, Any] = {
        "estimated_cost_usd": None,
        "cost_basis": None,
        "pricing_version": ANTHROPIC_PRICING_VERSION,
        "unavailable_reason": None,
    }
    if model not in _ANTHROPIC_PRICING_USD_PER_MILLION:
        result["unavailable_reason"] = "unknown_model_pricing"
        return result
    if not isinstance(usage, dict) or "input_tokens" not in usage or "output_tokens" not in usage:
        result["unavailable_reason"] = "usage_unavailable"
        return result
    try:
        input_tokens = int(usage["input_tokens"])
        output_tokens = int(usage["output_tokens"])
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        result["unavailable_reason"] = "usage_invalid"
        return result
    input_rate, output_rate = _ANTHROPIC_PRICING_USD_PER_MILLION[model]
    result["estimated_cost_usd"] = (
        input_tokens * input_rate + output_tokens * output_rate
    ) / 1_000_000
    result["cost_basis"] = "estimated"
    return result


def is_anthropic_url(url: str) -> bool:
    return any(marker in (url or "") for marker in ANTHROPIC_HOST_MARKERS)


def translate_model_id(model: str | None) -> str:
    """Map a gateway-style model id onto a real Anthropic API id.

    Unknown ids are passed through unchanged rather than silently rewritten: a wrong-but-explicit
    404 from Anthropic naming the id is far easier to diagnose than a silent substitution that
    bills a model nobody chose.
    """
    raw = (model or "").strip()
    if not raw or raw == "auto":
        return DEFAULT_ANTHROPIC_MODEL
    if raw in MODEL_ID_TRANSLATIONS:
        return MODEL_ID_TRANSLATIONS[raw]
    if raw in KNOWN_ANTHROPIC_MODEL_IDS:
        return raw
    if raw.startswith("anthropic/"):
        stripped = raw.split("/", 1)[1]
        return MODEL_ID_TRANSLATIONS.get(stripped, stripped)
    return raw


def split_system(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Anthropic takes `system` as a TOP-LEVEL parameter, not a message role."""
    system_parts: list[str] = []
    rest: list[dict[str, Any]] = []
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                system_parts.append(content)
            continue
        rest.append(msg)
    return "\n\n".join(system_parts), rest


def to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate OpenAI-shaped turns to the Anthropic Messages shape.

    Properties this must keep (same three as Cato's):
      * Idempotent — already-Anthropic messages (list content, no ``tool_calls``) pass through.
      * Grouped — every ``tool_result`` answering one assistant turn merges into a SINGLE user
        message. Anthropic rejects a ``tool_use`` block not answered in the immediately following
        user turn, so a multi-tool turn must not become several user messages.
      * Order preserving — ``tool_result`` blocks stay in call order.
    """
    out: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    def _flush() -> None:
        if pending:
            out.append({"role": "user", "content": list(pending)})
            pending.clear()

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")

        if role == "tool":
            content = msg.get("content")
            pending.append({
                "type": "tool_result",
                "tool_use_id": str(msg.get("tool_call_id") or "unknown"),
                "content": content if isinstance(content, str) else json.dumps(content, default=str),
            })
            continue

        _flush()

        if role == "assistant" and msg.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            text = msg.get("content")
            if isinstance(text, str) and text.strip():
                blocks.append({"type": "text", "text": text})
            elif isinstance(text, list):
                blocks.extend(b for b in text if isinstance(b, dict))
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                raw_args = fn.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        parsed = json.loads(raw_args or "{}")
                    except (json.JSONDecodeError, ValueError):
                        parsed = {}
                else:
                    parsed = raw_args
                blocks.append({
                    "type": "tool_use",
                    "id": str(tc.get("id") or "unknown"),
                    "name": str(fn.get("name") or ""),
                    "input": parsed if isinstance(parsed, dict) else {},
                })
            out.append({"role": "assistant", "content": blocks})
            continue

        # An assistant turn with no content and no tool_calls is not a legal Anthropic message.
        if role == "assistant" and not msg.get("content"):
            continue
        out.append(msg)

    _flush()
    return out


def to_anthropic_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """`{type:"function", function:{name, description, parameters}}` -> `{name, description,
    input_schema}`. Already-Anthropic tool dicts pass through."""
    converted: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if "input_schema" in tool and "name" in tool:
            if tool.get("name") == CANONICAL_RESULT_TOOL_NAME:
                normalized = dict(tool)
                schema = normalized.get("input_schema")
                if isinstance(schema, dict):
                    # Anthropic accepts JSON Schema but rejects the document-level
                    # dialect marker in tool input_schema on the pinned 4.5 API.
                    normalized["input_schema"] = {
                        key: value for key, value in schema.items() if key != "$schema"
                    }
                normalized.pop("strict", None)
                normalized.pop("_genesis_agent_id", None)
                converted.append(normalized)
            else:
                converted.append(tool)
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = str(fn.get("name") or "")
        if not name:
            continue
        converted.append({
            "name": name,
            "description": str(fn.get("description") or ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return converted


_RESERVED_ROOT_COMBINATORS = frozenset({"oneOf", "allOf", "anyOf", "if", "then", "else"})
_RESERVED_UNSUPPORTED_KEYWORDS = frozenset({
    "$schema", "minimum", "maximum", "minLength", "uniqueItems", "format",
})


EXACT_FORCED_TOOL_RESPONSE_ERROR_CODES = frozenset({
    "exact_forced_tool_response_invalid",
    "exact_forced_tool_response_truncated",
    "exact_forced_tool_response_refusal",
    "exact_forced_tool_response_missing",
    "exact_forced_tool_response_text",
    "exact_forced_tool_response_multiple",
    "exact_forced_tool_response_wrong_tool",
    "exact_forced_tool_response_arguments",
    "exact_forced_tool_response_model",
    "exact_forced_tool_response_usage",
})


class ExactForcedToolResponseError(ValueError):
    """Content-free failure while consuming a forced non-executing tool response."""

    def __init__(self, code: str) -> None:
        if code not in EXACT_FORCED_TOOL_RESPONSE_ERROR_CODES:
            code = "exact_forced_tool_response_invalid"
        self.code = code
        super().__init__(code)


def exact_forced_tool_envelope_reason(error: Any) -> str | None:
    """Project only the closed content-free envelope code from an exception."""
    code = getattr(error, "code", None)
    return code if code in EXACT_FORCED_TOOL_RESPONSE_ERROR_CODES else None


@dataclass(frozen=True, slots=True)
class ExactForcedToolResponse:
    """Validated provider envelope; argument semantics remain the caller's responsibility."""

    tool_name: str
    arguments: dict[str, Any]
    actual_model: str
    input_tokens: int
    output_tokens: int


def validate_reserved_anthropic_schema(schema: Any, slug: str) -> None:
    """Fail closed on the exact post-wire Anthropic reserved-tool dialect.

    Errors contain only stable internal codes; schema content is never copied
    into exceptions or traces.
    """
    def fail(code: str) -> None:
        raise ValueError(f"reserved_schema_invalid:{code}")

    if slug not in CANONICAL_AGENT_IDS:
        fail("agent_binding")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        fail("root_type")
    if _RESERVED_ROOT_COMBINATORS & set(schema):
        fail("root_combinator")
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        fail("root_shape")
    agent = properties.get("agent_id")
    if not isinstance(agent, dict) or agent.get("enum") != [slug]:
        fail("agent_binding")
    expected_required = {*COMMON_RESULT_FIELDS, "clarification"}
    finance = slug in FINANCE_AGENT_IDS
    has_finance = "finance_proposal" in properties
    finance_required = "finance_proposal" in required
    if finance:
        expected_required.add("finance_proposal")
    if has_finance != finance or finance_required != finance or set(required) != expected_required:
        fail("domain_shape")
    if any(field in properties for field in CLARIFICATION_FIELDS):
        fail("domain_shape")
    clarification = properties.get("clarification")
    if clarification != {"type": "null"}:
        fail("domain_shape")

    optional_count = 0
    union_count = 0

    def walk(node: Any) -> None:
        nonlocal optional_count, union_count
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        if _RESERVED_UNSUPPORTED_KEYWORDS & set(node):
            fail("unsupported_keyword")
        node_type = node.get("type")
        if isinstance(node_type, list):
            if len(node_type) < 2 or len(node_type) > 16 or any(
                not isinstance(item, str) for item in node_type
            ):
                fail("union_limit")
            union_count += 1
        if "anyOf" in node:
            union_count += 1
        if node_type == "object":
            node_properties = node.get("properties")
            node_required = node.get("required")
            if (
                node.get("additionalProperties") is not False
                or not isinstance(node_properties, dict)
                or not isinstance(node_required, list)
            ):
                fail("unclosed_object")
            optional_count += len(set(node_properties) - set(node_required))
        for value in node.values():
            walk(value)

    walk(schema)
    if optional_count > 24:
        fail("optional_limit")
    if union_count > 24:
        fail("union_limit")


def build_anthropic_request(
    *, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
    max_tokens: int,
) -> dict[str, Any]:
    """Assemble the /v1/messages body. `max_tokens` is REQUIRED by this API."""
    system, conversation = split_system(messages)
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max(1, int(max_tokens)),
        "messages": to_anthropic_messages(conversation),
    }
    if system:
        body["system"] = system
    expected_reserved_slug = ""
    for source_tool in tools or []:
        if (
            isinstance(source_tool, dict)
            and source_tool.get("name") == CANONICAL_RESULT_TOOL_NAME
        ):
            marker = source_tool.get("_genesis_agent_id")
            expected_reserved_slug = marker if isinstance(marker, str) else ""
    anthropic_tools = to_anthropic_tools(tools)
    if anthropic_tools:
        reserved = [tool for tool in anthropic_tools if tool.get("name") == CANONICAL_RESULT_TOOL_NAME]
        if reserved and len(anthropic_tools) != 1:
            raise ValueError("reserved canonical result tool must be exclusive")
        if reserved:
            schema = reserved[0].get("input_schema")
            validate_reserved_anthropic_schema(schema, expected_reserved_slug)
        body["tools"] = anthropic_tools
        body["tool_choice"] = (
            {
                "type": "tool",
                "name": CANONICAL_RESULT_TOOL_NAME,
                "disable_parallel_tool_use": True,
            }
            if reserved
            else {"type": "auto"}
        )
    return body


def build_exact_forced_tool_request(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tool_name: str,
    input_schema: dict[str, Any],
    max_tokens: int,
    description: str = "",
) -> dict[str, Any]:
    """Build a generic one-tool, exact-choice Anthropic request.

    This additive helper is for non-executing structured classifiers such as the
    semantic safety judge.  The canonical result path continues to use
    :func:`build_anthropic_request` and its existing slug-specific preflight.
    """
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise ValueError("exact_forced_tool_invalid:name")
    if tool_name == CANONICAL_RESULT_TOOL_NAME:
        raise ValueError("exact_forced_tool_invalid:canonical_reserved")
    _validate_exact_forced_tool_schema(input_schema)
    body = build_anthropic_request(
        model=model,
        messages=messages,
        tools=[{
            "name": tool_name,
            "description": description,
            "input_schema": input_schema,
        }],
        max_tokens=max_tokens,
    )
    body["tool_choice"] = {
        "type": "tool",
        "name": tool_name,
        "disable_parallel_tool_use": True,
    }
    return body


def consume_exact_forced_tool_response(
    payload: Mapping[str, Any],
    *,
    tool_name: str,
) -> ExactForcedToolResponse:
    """Accept exactly one named tool-use block and no provider text.

    Exceptions contain stable codes only.  They never include tool arguments,
    provider bodies, or narrative content.
    """
    if not isinstance(payload, Mapping):
        raise ExactForcedToolResponseError("exact_forced_tool_response_invalid")
    stop_reason = payload.get("stop_reason")
    if stop_reason == "max_tokens":
        raise ExactForcedToolResponseError("exact_forced_tool_response_truncated")
    if stop_reason == "refusal":
        raise ExactForcedToolResponseError("exact_forced_tool_response_refusal")
    if stop_reason != "tool_use":
        raise ExactForcedToolResponseError("exact_forced_tool_response_missing")
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        raise ExactForcedToolResponseError("exact_forced_tool_response_invalid")
    tool_blocks = [
        block
        for block in blocks
        if isinstance(block, Mapping) and block.get("type") == "tool_use"
    ]
    text_blocks = [
        block
        for block in blocks
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]
    if text_blocks:
        raise ExactForcedToolResponseError("exact_forced_tool_response_text")
    if len(tool_blocks) == 0:
        raise ExactForcedToolResponseError("exact_forced_tool_response_missing")
    if len(tool_blocks) != 1:
        raise ExactForcedToolResponseError("exact_forced_tool_response_multiple")
    # Fail closed on unknown blocks as well; only the one tool-use block is legal.
    if len(blocks) != 1 or not isinstance(blocks[0], Mapping):
        raise ExactForcedToolResponseError("exact_forced_tool_response_invalid")
    block = tool_blocks[0]
    if block.get("name") != tool_name:
        raise ExactForcedToolResponseError("exact_forced_tool_response_wrong_tool")
    arguments = block.get("input")
    if not isinstance(arguments, Mapping):
        raise ExactForcedToolResponseError("exact_forced_tool_response_arguments")
    actual_model = payload.get("model")
    if not isinstance(actual_model, str) or not actual_model:
        raise ExactForcedToolResponseError("exact_forced_tool_response_model")
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        raise ExactForcedToolResponseError("exact_forced_tool_response_usage")
    try:
        input_tokens = _nonnegative_int(usage.get("input_tokens"))
        output_tokens = _nonnegative_int(usage.get("output_tokens"))
    except (TypeError, ValueError):
        raise ExactForcedToolResponseError("exact_forced_tool_response_usage") from None
    try:
        detached_arguments = json.loads(
            json.dumps(arguments, ensure_ascii=True, allow_nan=False)
        )
    except (TypeError, ValueError):
        raise ExactForcedToolResponseError(
            "exact_forced_tool_response_arguments"
        ) from None
    if not isinstance(detached_arguments, dict):
        raise ExactForcedToolResponseError("exact_forced_tool_response_arguments")
    return ExactForcedToolResponse(
        tool_name=tool_name,
        arguments=detached_arguments,
        actual_model=actual_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _validate_exact_forced_tool_schema(schema: Any) -> None:
    def fail(code: str) -> None:
        raise ValueError(f"exact_forced_tool_schema_invalid:{code}")

    if not isinstance(schema, dict) or schema.get("type") != "object":
        fail("root_type")
    if _RESERVED_ROOT_COMBINATORS & set(schema):
        fail("root_combinator")

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        if _RESERVED_UNSUPPORTED_KEYWORDS & set(node):
            fail("unsupported_keyword")
        if node.get("type") == "object":
            properties = node.get("properties")
            required = node.get("required")
            if (
                node.get("additionalProperties") is not False
                or not isinstance(properties, dict)
                or not isinstance(required, list)
                or set(required) != set(properties)
                or len(required) != len(set(required))
            ):
                fail("open_or_optional_object")
        for value in node.values():
            walk(value)

    walk(schema)


def _nonnegative_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError
    if value < 0:
        raise ValueError
    return value


def from_anthropic_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise an Anthropic response back into the OpenAI shape the agent loop parses.

    The loop reads `choices[0].message.tool_calls` and `usage.total_tokens`; translating on the
    way back means the loop, the trace, the budget accounting and every existing test keep
    working against one canonical shape.
    """
    blocks = payload.get("content") or []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": str(block.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(block.get("name") or ""),
                    "arguments": json.dumps(block.get("input") or {}, default=str),
                },
            })

    usage = payload.get("usage") or {}
    prompt_tokens = int(usage.get("input_tokens", 0) or 0)
    completion_tokens = int(usage.get("output_tokens", 0) or 0)

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(p for p in text_parts if p) or None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    stop_reason = payload.get("stop_reason")
    if stop_reason == "tool_use":
        finish_reason = "tool_calls"
    elif stop_reason == "max_tokens":
        finish_reason = "length"
    elif stop_reason == "refusal":
        finish_reason = "refusal"
    else:
        finish_reason = "stop"
    return {
        "id": payload.get("id"),
        "model": payload.get("model"),
        "choices": [{
            "index": 0,
            "message": message,
            # Anthropic's "tool_use" stop_reason is the OpenAI "tool_calls" finish_reason.
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
