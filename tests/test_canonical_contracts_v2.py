from __future__ import annotations

import asyncio
import json

import pytest
import httpx

from eval.canonical.contracts import (
    GENESIS_OUTPUT_CONTRACT,
    JUDGE_QUALITIES,
    JUDGE_RESULTS_CONTRACT,
    ContractViolation,
    extract_forced_tool_result,
)
from eval.canonical.contract_caller import ContractModelCaller
from eval.canonical.experiments import ExperimentContractError


def genesis_payload() -> dict:
    return {
        "agent_id": "genesis-automation-architect",
        "task_id": "case-1",
        "scope": "bounded",
        "status": "HOLD",
        "summary": "ANALYSIS: Evidence bounded.",
        "findings": [],
        "proposals": [],
        "evidence_refs": ["E-1"],
        "assumptions": [],
        "conflicts": [],
        "unknowns": [],
        "confidence": 0.8,
        "risk_flags": [],
        "recommended_next_actions": [],
        "requested_human_decisions": [],
        "tool_calls_summary": [],
    }


def judge_payload() -> dict:
    return {
        "results": [
            {"name": name, "score": 0.95, "label": "PASS", "explanation": "Evidence bounded."}
            for name in JUDGE_QUALITIES
        ]
    }


def response(payload: dict, contract=GENESIS_OUTPUT_CONTRACT, model="exact/model") -> dict:
    return {
        "model": model,
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": "ignored prose",
                    "tool_calls": [
                        {"function": {"name": contract.tool_name, "arguments": json.dumps(payload)}}
                    ],
                },
            }
        ],
    }


def test_contracts_accept_canonical_valid_fixtures() -> None:
    assert GENESIS_OUTPUT_CONTRACT.validate(genesis_payload())["status"] == "HOLD"
    assert len(JUDGE_RESULTS_CONTRACT.validate(judge_payload())["results"]) == 8
    assert GENESIS_OUTPUT_CONTRACT.fingerprint != JUDGE_RESULTS_CONTRACT.fingerprint


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("scope"),
        lambda value: value.update(extra="forbidden"),
        lambda value: value.update(status="UNKNOWN"),
        lambda value: value.update(confidence=1.1),
    ],
)
def test_genesis_contract_rejects_missing_extra_enum_and_range(mutation) -> None:
    payload = genesis_payload()
    mutation(payload)
    with pytest.raises(ContractViolation, match="submit_arguments_invalid"):
        GENESIS_OUTPUT_CONTRACT.validate(payload)


def test_judge_contract_rejects_duplicate_and_incomplete_quality_sets() -> None:
    duplicate = judge_payload()
    duplicate["results"][-1]["name"] = duplicate["results"][0]["name"]
    incomplete = judge_payload()
    incomplete["results"].pop()
    for payload in (duplicate, incomplete):
        with pytest.raises(ContractViolation, match="submit_arguments_invalid"):
            JUDGE_RESULTS_CONTRACT.validate(payload)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda value: value["choices"][0]["message"].pop("tool_calls"), "expected_submit_tool_missing"),
        (lambda value: value["choices"][0]["message"]["tool_calls"][0]["function"].update(name="wrong"), "unexpected_submit_tool"),
        (lambda value: value["choices"][0]["message"]["tool_calls"].append({"function": {"name": "wrong", "arguments": "{}"}}), "unexpected_submit_tool"),
        (lambda value: value["choices"][0]["message"]["tool_calls"][0]["function"].update(arguments="not-json"), "submit_arguments_invalid"),
        (lambda value: value.update(model="fallback/model"), "actual_model_mismatch"),
    ],
)
def test_only_one_exact_forced_tool_call_on_the_exact_model_succeeds(mutate, code) -> None:
    provider_response = response(genesis_payload())
    mutate(provider_response)
    with pytest.raises(ContractViolation, match=code):
        extract_forced_tool_result(provider_response, GENESIS_OUTPUT_CONTRACT, requested_model="exact/model")


def test_free_text_json_is_never_a_success_path() -> None:
    provider_response = {"model": "exact/model", "choices": [{"message": {"content": json.dumps(genesis_payload())}}]}
    with pytest.raises(ContractViolation, match="expected_submit_tool_missing"):
        extract_forced_tool_result(provider_response, GENESIS_OUTPUT_CONTRACT, requested_model="exact/model")


def test_provider_tool_is_strict_and_side_effect_free_by_construction() -> None:
    tool = GENESIS_OUTPUT_CONTRACT.provider_tool()
    assert tool["function"]["name"] == "submit_genesis_output_v5"
    assert tool["function"]["strict"] is True
    assert tool["function"]["parameters"] is GENESIS_OUTPUT_CONTRACT.json_schema


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, captured: list, response: _FakeResponse) -> None:
        self._captured = captured
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, endpoint, *, headers, json):
        self._captured.append({"endpoint": endpoint, "headers": headers, "body": json})
        return self._response


def anthropic_response(payload: dict, *, model="claude-sonnet-4-5-20250929", stop_reason="tool_use") -> dict:
    return {
        "id": "msg_safe",
        "model": model,
        "stop_reason": stop_reason,
        "content": [
            {
                "type": "tool_use",
                "id": "tool_safe",
                "name": GENESIS_OUTPUT_CONTRACT.tool_name,
                "input": payload,
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def test_exact_caller_forces_named_submit_tool_and_never_adds_fallback(monkeypatch) -> None:
    captured = []
    provider_response = _FakeResponse(200, response(genesis_payload()))
    monkeypatch.setattr(
        "eval.canonical.contract_caller.httpx.AsyncClient",
        lambda **_kwargs: _FakeClient(captured, provider_response),
    )
    caller = ContractModelCaller("https://router.invalid/v1/chat/completions", "not-a-real-key")
    result = asyncio.run(
        caller(
            "exact/model",
            [{"role": "user", "content": "safe"}],
            300,
            [GENESIS_OUTPUT_CONTRACT.provider_tool()],
        )
    )
    assert result["model"] == "exact/model"
    assert len(captured) == 1
    body = captured[0]["body"]
    assert body["model"] == "exact/model"
    assert body["tool_choice"] == {"type": "function", "function": {"name": "submit_genesis_output_v5"}}
    assert body["parallel_tool_calls"] is False
    assert "fallback" not in json.dumps(body).lower()
    assert "not-a-real-key" not in json.dumps(body)


def test_anthropic_caller_forces_named_tool_and_verifies_exact_native_model(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(
        "eval.canonical.contract_caller.httpx.AsyncClient",
        lambda **_kwargs: _FakeClient(captured, _FakeResponse(200, anthropic_response(genesis_payload()))),
    )
    caller = ContractModelCaller(
        "https://api.anthropic.com", "not-a-real-key", provider="anthropic"
    )
    result = asyncio.run(
        caller(
            "anthropic/claude-sonnet-4.5",
            [{"role": "system", "content": "safe"}, {"role": "user", "content": "safe"}],
            300,
            [GENESIS_OUTPUT_CONTRACT.provider_tool()],
        )
    )
    assert result["model"] == "anthropic/claude-sonnet-4.5"
    assert result["provider_model"] == "claude-sonnet-4-5-20250929"
    assert result["route_fingerprint"]
    body = captured[0]["body"]
    assert body["model"] == "claude-sonnet-4-5-20250929"
    assert body["tool_choice"] == {
        "type": "tool",
        "name": "submit_genesis_output_v5",
        "disable_parallel_tool_use": True,
    }
    assert "strict" not in body["tools"][0]
    assert "$schema" not in body["tools"][0]["input_schema"]
    assert "$schema" in GENESIS_OUTPUT_CONTRACT.json_schema
    assert captured[0]["endpoint"] == "https://api.anthropic.com/v1/messages"
    assert captured[0]["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in captured[0]["headers"]
    assert "not-a-real-key" not in json.dumps(body)
    assert result["provider_strict_schema"] is False
    assert result["local_strict_schema"] is True


def test_anthropic_45_omits_provider_strict_that_live_api_rejects_but_keeps_local_validation(
    monkeypatch,
) -> None:
    captured = []
    invalid = anthropic_response({"status": "HOLD"})
    monkeypatch.setattr(
        "eval.canonical.contract_caller.httpx.AsyncClient",
        lambda **_kwargs: _FakeClient(captured, _FakeResponse(200, invalid)),
    )
    caller = ContractModelCaller(
        "https://api.anthropic.com", "not-a-real-key", provider="anthropic"
    )
    normalized = asyncio.run(
        caller(
            "anthropic/claude-sonnet-4.5",
            [{"role": "user", "content": "safe"}],
            300,
            [GENESIS_OUTPUT_CONTRACT.provider_tool()],
        )
    )
    assert "strict" not in captured[0]["body"]["tools"][0]
    with pytest.raises(ContractViolation, match="submit_arguments_invalid"):
        extract_forced_tool_result(
            normalized,
            GENESIS_OUTPUT_CONTRACT,
            requested_model="anthropic/claude-sonnet-4.5",
        )


@pytest.mark.parametrize(
    ("payload_mutation", "code"),
    [
        (lambda value: value.update(model="claude-haiku-4-5-20251001"), "actual_model_mismatch"),
        (lambda value: value.update(stop_reason="max_tokens"), "finish_reason_length"),
        (lambda value: value.update(stop_reason="refusal"), "provider_refusal"),
        (lambda value: value["content"][0].update(name="wrong"), "unexpected_submit_tool"),
        (lambda value: value["content"][0].update(input={"status": "HOLD"}), "submit_arguments_invalid"),
    ],
)
def test_anthropic_model_truncation_refusal_tool_and_schema_fail_deterministically(
    monkeypatch, payload_mutation, code
) -> None:
    payload = anthropic_response(genesis_payload())
    payload_mutation(payload)
    monkeypatch.setattr(
        "eval.canonical.contract_caller.httpx.AsyncClient",
        lambda **_kwargs: _FakeClient([], _FakeResponse(200, payload)),
    )
    caller = ContractModelCaller(
        "https://api.anthropic.com", "not-a-real-key", provider="anthropic"
    )
    with pytest.raises((ExperimentContractError, ContractViolation), match=code) as caught:
        response_value = asyncio.run(
            caller(
                "anthropic/claude-sonnet-4.5",
                [{"role": "user", "content": "safe"}],
                300,
                [GENESIS_OUTPUT_CONTRACT.provider_tool()],
            )
        )
        extract_forced_tool_result(
            response_value,
            GENESIS_OUTPUT_CONTRACT,
            requested_model="anthropic/claude-sonnet-4.5",
        )
    if isinstance(caught.value, ExperimentContractError):
        assert caught.value.transient is False
    assert "not-a-real-key" not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "transient"),
    [(400, False), (404, False), (429, True), (500, True), (503, True)],
)
def test_exact_caller_classifies_only_429_and_5xx_as_retryable(monkeypatch, status, transient) -> None:
    monkeypatch.setattr(
        "eval.canonical.contract_caller.httpx.AsyncClient",
        lambda **_kwargs: _FakeClient([], _FakeResponse(status, {"provider_detail": "discarded"})),
    )
    caller = ContractModelCaller("https://router.invalid/v1/chat/completions", "not-a-real-key")
    with pytest.raises(ExperimentContractError) as caught:
        asyncio.run(caller("exact/model", [], 10, [GENESIS_OUTPUT_CONTRACT.provider_tool()]))
    assert caught.value.transient is transient


class _TimeoutClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *_args, **_kwargs):
        raise httpx.ReadTimeout("redacted timeout")


def test_exact_caller_classifies_timeout_as_retryable_without_provider_prose(monkeypatch) -> None:
    monkeypatch.setattr("eval.canonical.contract_caller.httpx.AsyncClient", lambda **_kwargs: _TimeoutClient())
    caller = ContractModelCaller("https://router.invalid/v1/chat/completions", "not-a-real-key")
    with pytest.raises(ExperimentContractError) as caught:
        asyncio.run(caller("exact/model", [], 10, [GENESIS_OUTPUT_CONTRACT.provider_tool()]))
    assert caught.value.code == "provider_timeout"
    assert caught.value.transient is True
    assert "redacted timeout" not in str(caught.value)
