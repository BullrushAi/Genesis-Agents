"""Credential redaction for local evaluation results and exception messages."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from eval.tests.fakes import FakeTransport, RecordingSleep
from eval.genesis_client import GenesisClient, MoneyDomainBlocked, RawResponse
from eval.redaction import REDACTED, is_sensitive_key, redact, refresh_env_secrets

MARKER = "SUPERSECRET-MARKER-9f3c1a7e-DO-NOT-LEAK"

SECRET_ENV_NAMES = (
    "GATEWAY_API_KEY",
    "AGENT_GATEWAY_SECRET",
    "LLM_API_KEY",
    "DATABASE_URL",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "RETIRED_SERVICE_API_KEY",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture
def planted_env(monkeypatch):
    """Every credential env var set to the marker."""
    for name in SECRET_ENV_NAMES:
        monkeypatch.setenv(name, MARKER)
    monkeypatch.setenv("DATABASE_URL", f"postgres://genesis:{MARKER}@db.internal:5432/genesis")
    refresh_env_secrets()
    yield
    refresh_env_secrets()


def test_transport_failure_message_is_redacted_at_construction(planted_env):
    from eval.genesis_client import TransportFailure

    exc = TransportFailure("connect", f"failed with token {MARKER}")
    assert MARKER not in str(exc)
    assert MARKER not in exc.message


# ---------------------------------------------------------------------------
# Redactor unit behaviour (matches Cato approval_policy.redact)
# ---------------------------------------------------------------------------


def test_redacts_by_key_at_any_depth():
    payload = {"a": {"b": {"c": {"headers": {"authorization": "Bearer abc123"}}}}}
    out = redact(payload)
    assert out["a"]["b"]["c"]["headers"]["authorization"] == REDACTED


@pytest.mark.parametrize(
    "key",
    [
        "api_key", "apikey", "api-key", "API_KEY", "x_api_key",
        "private_key", "session_key", "authorization", "Authorization",
        "client_secret", "AGENT_GATEWAY_SECRET", "GATEWAY_API_KEY",
        "password", "access_token", "cookie", "DATABASE_URL",
        "aws_secret_access_key", "AWS_ACCESS_KEY_ID",
        # The gateway signing key. "privkey" has no underscore before "key",
        # so the generic "_key" part does NOT catch it — it needs its own part.
        "GENESIS_GATEWAY_PRIVKEY_B64", "privkey", "gateway_privkey",
        "R2_API_TOKEN", "GENESIS_SESSION_VAULT_KEY",
    ],
)
def test_sensitive_keys_are_recognised(key):
    assert is_sensitive_key(key), key
    assert redact({key: "value-here"})[key] == REDACTED


def test_bare_key_is_left_intact():
    """Matches the reference implementation: '_key' is not a substring of 'key'."""
    assert is_sensitive_key("key") is False
    assert redact({"key": "genesis-knowledge-evidence"})["key"] == "genesis-knowledge-evidence"
    assert redact({"keyword": "market research"})["keyword"] == "market research"
    assert redact({"monkey": "banana"})["monkey"] == "banana"


@pytest.mark.parametrize(
    "text",
    [
        "sk-abcdefghijklmnop1234",
        "Bearer eyJhbGciOi.payload.signature",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "xoxb-1234567890-abcdefghij",
        "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM.SflKxwRJSM",
    ],
)
def test_credential_shaped_values_are_redacted_under_innocent_keys(text):
    out = redact({"notes": f"the value is {text} ok"})
    assert text not in out["notes"], out["notes"]


def test_lists_tuples_and_sets_are_walked():
    out = redact({"items": [{"token": "abc"}, ({"password": "p"},)]})
    assert out["items"][0]["token"] == REDACTED
    assert out["items"][1][0]["password"] == REDACTED


def test_depth_limit_does_not_explode():
    node: dict = {"authorization": "Bearer x"}
    for _ in range(60):
        node = {"n": node}
    assert redact(node) is not None


def test_non_string_values_under_sensitive_keys_are_preserved():
    out = redact({"has_api_key": True, "token_count": 12, "secret": None})
    assert out["has_api_key"] is True
    assert out["token_count"] == 12
    assert out["secret"] is None


def test_client_never_stores_the_credential_on_a_result(planted_env):
    transport = FakeTransport([RawResponse(200, '{"response":"ok"}')])
    client = GenesisClient(transport=transport, api_key=MARKER, jitter=False)
    result = asyncio.run(client.run_agent("genesis-knowledge-evidence", "task"))
    assert MARKER not in json.dumps(result.__dict__, default=str)
    # ...but it WAS sent on the wire, otherwise auth could not work.
    assert transport.calls[0]["headers"]["X-Agent-Api-Key"] == MARKER
    assert client.has_credential() is True
