"""Independent QA coverage for canonical routing and AP2 smoke redaction."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import main
from canonical_profiles import _prefix_contract_fixture_result, load_canonical_eval_suite
from canonical_result_contract import CANONICAL_RESULT_TOOL_NAME, CLARIFICATION_FIELDS
from runtime.semantic_safety_judge import CANDIDATE_MODEL
from runtime.request_auth import Principal
from scripts import ap2_operator_smoke as smoke
from test_canonical_genesis_roster import CANONICAL_SLUGS


SECRET_SENTINELS = (
    "PRIVATE_QA_SENTINEL",
    "VAULT_PASSWORD_QA_SENTINEL",
    "Bearer AUTHORIZATION_QA_SENTINEL",
    "SIGNATURE_QA_SENTINEL",
)


class _Vault:
    def __init__(self) -> None:
        private = Ed25519PrivateKey.generate()
        self.private_raw = private.private_bytes_raw()
        self.public_raw = private.public_key().public_bytes_raw()

    def get(self, name: str):
        values = {
            "CATO_AP2_PRIVKEY": base64.b64encode(self.private_raw).decode("ascii"),
            "CATO_AP2_PUBKEY": base64.b64encode(self.public_raw).decode("ascii"),
        }
        return values.get(name)

    def list_keys(self):
        return ["CATO_AP2_PRIVKEY", "CATO_AP2_PUBKEY"]


class _Crypto:
    @staticmethod
    def sign(vault: _Vault, message: bytes) -> bytes:
        return Ed25519PrivateKey.from_private_bytes(vault.private_raw).sign(message)


def _dependencies(_path: Path):
    vault = _Vault()
    return lambda: vault, _Crypto


def test_all_24_canonical_profiles_execute_through_safe_run_route(monkeypatch) -> None:
    seen_system_prompts: list[str] = []
    active_slug: dict[str, str] = {}
    monkeypatch.setenv("GENESIS_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_URL", "https://api.anthropic.com/v1/messages")
    monkeypatch.setenv("LLM_API_KEY", "redacted-test-key")

    async def router(*, slug: str, messages: list[dict], **_kwargs):
        seen_system_prompts.append(messages[0]["content"])
        assert messages[1]["content"] == "bounded QA probe"
        assert slug == active_slug["slug"]
        suite = load_canonical_eval_suite(slug)
        result = next(
            row["result"] for row in suite["contract_fixtures"] if row["kind"] == "normal"
        )
        result = _prefix_contract_fixture_result(result)
        clarification = None
        if result.pop("clarification_needed"):
            clarification = {
                field: result.pop(field)
                for field in CLARIFICATION_FIELDS[1:]
            }
        else:
            for field in CLARIFICATION_FIELDS[1:]:
                result.pop(field, None)
        result["clarification"] = clarification
        return {
            "model": CANDIDATE_MODEL,
            "choices": [{"finish_reason": "tool_calls", "message": {
                "content": None,
                "tool_calls": [{"id": "result", "type": "function", "function": {
                    "name": CANONICAL_RESULT_TOOL_NAME,
                    "arguments": json.dumps(result),
                }}],
            }}],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }

    async def allow_judge(*_args, **_kwargs):
        from runtime.semantic_safety_judge import SafetyJudgeVerdict
        return SafetyJudgeVerdict(True, None, "ALLOW", "NONE", (), "a" * 64, "b" * 64, 100, "claude-haiku-4-5-20251001", 10, 2, 0.00002, "estimated", "anthropic-public-2025-11-01-v1")

    monkeypatch.setattr(main, "call_exact_anthropic_candidate", router)
    monkeypatch.setattr(main, "judge_candidate_semantics", allow_judge)
    principal = Principal(
        principal_id="qa:canonical",
        tenant_id="qa",
        client_id="qa",
        scopes=frozenset({"agent.invoke"}),
        auth_method="ap2",
        expires_at=2_000_000_000,
    )

    for slug in sorted(CANONICAL_SLUGS):
        active_slug["slug"] = slug
        response = asyncio.run(
            main.run_agent(
                slug,
                main.RunRequest(
                    prompt="bounded QA probe",
                    mode="live_test",
                    skip_internal_escrow=True,
                ),
                principal,
            )
        )
        payload = json.loads(response.response)
        assert response.agentSlug == slug
        assert payload["ok"] is True
        assert payload["slug"] == slug
        assert json.loads(payload["response"])["agent_id"] == slug

    assert len(seen_system_prompts) == 19
    assert all("Cato chooses and bounds the job" in prompt for prompt in seen_system_prompts)


def test_mismatched_restored_keypair_fails_before_signing() -> None:
    vault = _Vault()
    other = _Vault()
    vault.public_raw = other.public_raw

    class MustNotSign:
        @staticmethod
        def sign(*_args):
            raise AssertionError("signing must not run for a mismatched identity")

    with pytest.raises(smoke.SmokeFailure, match="vault_identity_invalid") as caught:
        smoke._build_request(lambda: vault, MustNotSign, "genesis-support")
    assert caught.value.category == "vault_identity_invalid"


@pytest.mark.parametrize(
    ("failure_kind", "expected_category"),
    [
        ("signing", "signing_failed"),
        ("timeout", "timeout"),
        ("network", "network_error"),
    ],
)
def test_main_failure_categories_are_clear_and_never_leak(
    monkeypatch, capsys, caplog, failure_kind: str, expected_category: str
) -> None:
    vault = _Vault()

    class BrokenCrypto:
        @staticmethod
        def sign(*_args):
            raise RuntimeError(" ".join(SECRET_SENTINELS))

    def opener(_request, timeout):
        assert timeout == 5
        if failure_kind == "timeout":
            raise TimeoutError(" ".join(SECRET_SENTINELS))
        raise OSError(" ".join(SECRET_SENTINELS))

    crypto = BrokenCrypto if failure_kind == "signing" else _Crypto
    monkeypatch.setattr(smoke, "_load_cato", lambda _path: (lambda: vault, crypto))
    if failure_kind != "signing":
        monkeypatch.setattr(smoke.urllib.request, "urlopen", opener)
    caplog.set_level(logging.DEBUG)

    assert smoke.main(["--url", "https://genesis.example", "--timeout", "5"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert captured.out == ""
    assert payload["status"] == "refused"
    assert payload["category"] == expected_category
    assert payload["action"]
    combined = captured.out + captured.err + caplog.text
    for sentinel in SECRET_SENTINELS:
        assert sentinel not in combined
