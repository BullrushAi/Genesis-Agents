"""Exact-route OpenAI-compatible caller for local evaluation contract experiments."""
from __future__ import annotations

import copy
from typing import Any

import httpx

from eval.canonical.contracts import (
    GENESIS_OUTPUT_CONTRACT,
    JUDGE_RESULTS_CONTRACT,
    SEMANTIC_JUDGE_CONTRACT,
)
from canonical_result_contract import CANONICAL_RESULT_TOOL_NAME
from eval.canonical.model_catalog import ANTHROPIC_LOGICAL_ROUTES, anthropic_route_fingerprint


SUBMIT_TOOL_NAMES = frozenset(
    {
        GENESIS_OUTPUT_CONTRACT.tool_name,
        JUDGE_RESULTS_CONTRACT.tool_name,
        SEMANTIC_JUDGE_CONTRACT.tool_name,
        CANONICAL_RESULT_TOOL_NAME,
    }
)


class ContractModelCaller:
    """Call one exact model without AgentRuntime fallback or response persistence."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        *,
        provider: str = "openai",
        timeout_seconds: float = 120.0,
    ) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self._provider = provider
        self._timeout_seconds = timeout_seconds

    async def __call__(
        self,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        from eval.canonical.experiments import ExperimentContractError

        if not model or model == "auto":
            raise ExperimentContractError("exact_model_required")
        if self._provider == "anthropic":
            return await self._call_anthropic(model, messages, max_tokens, tools)
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            names = [
                tool.get("function", {}).get("name")
                for tool in tools
                if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
            ]
            submit_names = [name for name in names if name in SUBMIT_TOOL_NAMES]
            if submit_names:
                if len(tools) != 1 or len(submit_names) != 1:
                    raise ExperimentContractError("submit_contract_must_be_only_tool")
                body["tool_choice"] = {"type": "function", "function": {"name": submit_names[0]}}
                body["parallel_tool_calls"] = False
            else:
                body["tool_choice"] = "auto"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                    json=body,
                )
        except httpx.TimeoutException as exc:
            raise ExperimentContractError("provider_timeout", transient=True) from exc
        except httpx.HTTPError as exc:
            raise ExperimentContractError("provider_transport_error") from exc
        if response.status_code == 429 or 500 <= response.status_code <= 599:
            raise ExperimentContractError(f"provider_http_{response.status_code}", transient=True)
        if response.status_code >= 400:
            raise ExperimentContractError(f"provider_http_{response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExperimentContractError("provider_invalid_json") from exc
        if not isinstance(payload, dict):
            raise ExperimentContractError("provider_invalid_envelope")
        return payload

    async def _call_anthropic(
        self,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        from eval.canonical.experiments import ExperimentContractError
        from runtime.anthropic_wire import (
            ANTHROPIC_VERSION,
            from_anthropic_response,
            split_system,
            to_anthropic_messages,
            to_anthropic_tools,
        )

        native_model = ANTHROPIC_LOGICAL_ROUTES.get(model)
        if native_model is None:
            raise ExperimentContractError("exact_model_required")
        system, conversation = split_system(messages)
        body: dict[str, Any] = {
            "model": native_model,
            "messages": to_anthropic_messages(conversation),
            "max_tokens": max_tokens,
        }
        if system:
            body["system"] = system
        converted = to_anthropic_tools(tools)
        if converted:
            # Anthropic's strict-tool schema dialect rejects the JSON Schema
            # meta-schema declaration. Local validation still uses the complete
            # canonical Draft 2020-12 schema after extraction.
            converted = copy.deepcopy(converted)
            for tool in converted:
                schema = tool.get("input_schema")
                if isinstance(schema, dict):
                    schema.pop("$schema", None)
            names = [tool.get("name") for tool in converted]
            submit_names = [name for name in names if name in SUBMIT_TOOL_NAMES]
            if submit_names:
                if len(converted) != 1 or len(submit_names) != 1:
                    raise ExperimentContractError("submit_contract_must_be_only_tool")
                # The exact 4.5 routes available to this deployment reject the
                # provider-side strict property with invalid_request_error.
                # Keep the named tool forced and validate its arguments against
                # the full canonical schema locally; invalid arguments remain a
                # deterministic, zero-retry contract failure.
                body["tool_choice"] = {
                    "type": "tool",
                    "name": submit_names[0],
                    "disable_parallel_tool_use": True,
                }
            else:
                body["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
            body["tools"] = converted
        url = self._endpoint.rstrip("/")
        if not url.endswith("/v1/messages"):
            url += "/v1/messages"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    url,
                    headers={
                        "x-api-key": self._api_key,
                        "anthropic-version": ANTHROPIC_VERSION,
                        "content-type": "application/json",
                    },
                    json=body,
                )
        except httpx.TimeoutException as exc:
            raise ExperimentContractError("provider_timeout", transient=True) from exc
        except httpx.HTTPError as exc:
            raise ExperimentContractError("provider_transport_error") from exc
        if response.status_code == 429 or 500 <= response.status_code <= 599:
            raise ExperimentContractError(f"provider_http_{response.status_code}", transient=True)
        if response.status_code >= 400:
            raise ExperimentContractError(f"provider_http_{response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExperimentContractError("provider_invalid_json") from exc
        if not isinstance(payload, dict):
            raise ExperimentContractError("provider_invalid_envelope")
        actual_native = payload.get("model")
        if actual_native != native_model:
            raise ExperimentContractError("actual_model_mismatch")
        normalized = from_anthropic_response(payload)
        normalized["provider_model"] = actual_native
        normalized["provider"] = "anthropic"
        normalized["route_fingerprint"] = anthropic_route_fingerprint()
        normalized["provider_strict_schema"] = False
        normalized["local_strict_schema"] = True
        normalized["model"] = model
        stop_reason = payload.get("stop_reason")
        if stop_reason == "max_tokens":
            normalized["choices"][0]["finish_reason"] = "length"
        elif stop_reason in {"refusal", "content_filter"}:
            normalized["choices"][0]["finish_reason"] = stop_reason
        return normalized
