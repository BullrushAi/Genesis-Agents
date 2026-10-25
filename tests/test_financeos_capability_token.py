"""Round-trip tests for the HMAC capability-token scheme, ported from
FinanceOS's own `src/intent/capability.ts`. These prove Genesis's
token-construction code implements the exact wire format FinanceOS's
`verifyCapabilityToken` expects -- independent of whether a live FinanceOS
deployment currently has a matching secret configured (it does not; see
CURRENT/BLOCKERS.md)."""
from __future__ import annotations

import time

import pytest

from runtime.financeos_capability_token import (
    CapabilityConfigError,
    capability_signing_available,
    issue_capability_token,
    verify_capability_token,
)

SECRET = "x" * 32


def test_signing_not_available_without_secret() -> None:
    assert capability_signing_available(secret="") is False
    assert capability_signing_available(secret="too-short") is False
    assert capability_signing_available(secret=SECRET) is True


def test_issue_raises_without_configured_secret() -> None:
    with pytest.raises(CapabilityConfigError):
        issue_capability_token(intent_id="genesis:read", scope="intent:read", ttl_seconds=60, secret="")


def test_issue_and_verify_round_trip() -> None:
    token = issue_capability_token(
        intent_id="genesis:data-products:read",
        scope="intent:read",
        ttl_seconds=300,
        entity_id="22222222-3333-4444-5555-666666666666",
        subject="genesis-fin-revenue",
        secret=SECRET,
    )
    result = verify_capability_token(token, required_scope="intent:read", secret=SECRET)
    assert result.valid is True
    assert result.payload["intent_id"] == "genesis:data-products:read"
    assert result.payload["entity_id"] == "22222222-3333-4444-5555-666666666666"


def test_wire_format_matches_capability_ts() -> None:
    """`<b64url(payload)>.<hmac_hex>` -- exactly two dot-separated segments,
    the second all lowercase hex, matching capability.ts's own format."""
    token = issue_capability_token(intent_id="genesis:read", scope="intent:read", ttl_seconds=60, secret=SECRET)
    parts = token.split(".")
    assert len(parts) == 2
    assert all(c in "0123456789abcdef" for c in parts[1])
    assert len(parts[1]) == 64  # sha256 hex digest length


def test_expired_token_is_denied() -> None:
    token = issue_capability_token(
        intent_id="genesis:read", scope="intent:read", ttl_seconds=1, secret=SECRET, now=time.time() - 100,
    )
    result = verify_capability_token(token, secret=SECRET)
    assert result.valid is False
    assert result.reason == "expired"


def test_wrong_secret_is_denied() -> None:
    token = issue_capability_token(intent_id="genesis:read", scope="intent:read", ttl_seconds=60, secret=SECRET)
    result = verify_capability_token(token, secret="y" * 32)
    assert result.valid is False
    assert result.reason == "bad_signature"


def test_intent_mismatch_is_denied() -> None:
    token = issue_capability_token(intent_id="genesis:read", scope="intent:read", ttl_seconds=60, secret=SECRET)
    result = verify_capability_token(token, intent_id="something-else", secret=SECRET)
    assert result.valid is False
    assert result.reason == "intent_mismatch"


def test_scope_not_granted_is_denied() -> None:
    token = issue_capability_token(intent_id="genesis:read", scope="intent:read", ttl_seconds=60, secret=SECRET)
    result = verify_capability_token(token, required_scope="intent:write", secret=SECRET)
    assert result.valid is False
    assert result.reason == "scope_not_granted"


def test_verification_denies_when_no_secret_configured() -> None:
    token = issue_capability_token(intent_id="genesis:read", scope="intent:read", ttl_seconds=60, secret=SECRET)
    result = verify_capability_token(token, secret="")
    assert result.valid is False
    assert result.reason == "secret_not_configured"


def test_malformed_token_is_denied() -> None:
    result = verify_capability_token("not-a-real-token", secret=SECRET)
    assert result.valid is False
    assert result.reason == "malformed_token"
