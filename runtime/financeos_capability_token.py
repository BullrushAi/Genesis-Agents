"""HMAC capability-token client for FinanceOS's governed-layer read API.

Port of `E4L-FinanceOS-Official/src/intent/capability.ts` (that repo's own
authentication scheme) so Genesis can mint a token FinanceOS's
`verifyCapabilityToken` will accept, WITHOUT importing TypeScript. This module
only ever ISSUES tokens (Genesis is a caller, never FinanceOS's own issuer)
plus a local `verify_capability_token` used strictly for this repo's own
round-trip tests -- it is not a second authority over FinanceOS state.

Wire format, unchanged from capability.ts:
    <base64url(json(payload))>.<hex(hmac_sha256(encoded_payload, secret))>

payload = {
    intent_id: str,
    scope: str | list[str],
    exp: int (unix seconds),
    nonce: str,
    iat: int (unix seconds),
    entity_id?: str,
    subject?: str,
}

DEFAULT-DENY, same as the TypeScript original: with no configured secret,
`capability_signing_available()` is False and `issue_capability_token` raises
rather than emitting an unsigned/empty-keyed token.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets as _secrets
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

MIN_SECRET_LENGTH = 32
ENV_VAR = "AGENT_CAPABILITY_SIGNING_SECRET"


class CapabilityConfigError(ValueError):
    """Raised when issuing a token without a configured signing secret."""


def _resolve_secret(secret: str | None) -> str | None:
    """Mirror capability.ts's `resolveSecret`: absence is absence, never ''."""
    candidate = secret if secret is not None else os.environ.get(ENV_VAR, "")
    candidate = candidate.strip()
    return candidate if len(candidate) >= MIN_SECRET_LENGTH else None


def capability_signing_available(secret: str | None = None) -> bool:
    return _resolve_secret(secret) is not None


def _b64url_encode(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _b64url_decode(raw: str) -> str | None:
    try:
        padded = raw + "=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return None


def _sign(encoded_payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), encoded_payload.encode("utf-8"), hashlib.sha256).hexdigest()


def new_nonce() -> str:
    return _secrets.token_hex(16)


@dataclass(frozen=True)
class CapabilityVerification:
    valid: bool
    reason: str | None = None
    message: str | None = None
    payload: Mapping[str, Any] | None = None


def issue_capability_token(
    *,
    intent_id: str,
    scope: str | Sequence[str],
    ttl_seconds: int,
    entity_id: str | None = None,
    subject: str | None = None,
    nonce: str | None = None,
    now: float | None = None,
    secret: str | None = None,
) -> str:
    """Mint a token. Raises `CapabilityConfigError` when unsigned issuance
    would otherwise be possible -- matching `issueCapabilityToken`'s own
    "throws rather than issuing an unsigned credential" posture."""
    resolved = _resolve_secret(secret)
    if resolved is None:
        raise CapabilityConfigError(
            f"{ENV_VAR} is not configured (must be >= {MIN_SECRET_LENGTH} chars). "
            "Capability tokens cannot be issued."
        )
    if not intent_id or not intent_id.strip():
        raise CapabilityConfigError("A capability token must be bound to an intent id.")
    if ttl_seconds <= 0:
        raise CapabilityConfigError("A capability token must have a positive ttl_seconds.")

    iat = int(now if now is not None else time.time())
    payload: dict[str, Any] = {
        "intent_id": intent_id,
        "scope": list(scope) if isinstance(scope, (list, tuple)) else scope,
        "exp": iat + int(ttl_seconds),
        "nonce": nonce or new_nonce(),
        "iat": iat,
    }
    if entity_id is not None:
        payload["entity_id"] = entity_id
    if subject is not None:
        payload["subject"] = subject

    encoded = _b64url_encode(json.dumps(payload, separators=(",", ":")))
    return f"{encoded}.{_sign(encoded, resolved)}"


def verify_capability_token(
    token: str,
    *,
    intent_id: str | None = None,
    required_scope: str | None = None,
    clock_skew_seconds: int = 0,
    now: float | None = None,
    secret: str | None = None,
) -> CapabilityVerification:
    """Local verifier for this repo's own round-trip tests. Never a second
    authority over FinanceOS state -- FinanceOS's own `verifyCapabilityToken`
    is what actually gates `/api/v1/**`; this exists so Genesis can prove its
    own token-construction code is correct without a live FinanceOS call."""
    resolved = _resolve_secret(secret)
    if resolved is None:
        return CapabilityVerification(False, "secret_not_configured", f"{ENV_VAR} is not configured.")

    if not token:
        return CapabilityVerification(False, "malformed_token", "Empty capability token.")
    parts = token.split(".")
    if len(parts) != 2:
        return CapabilityVerification(False, "malformed_token", "Expected <b64url(payload)>.<hmac_hex>.")
    encoded, signature = parts
    if not encoded or not all(c in "0123456789abcdef" for c in signature):
        return CapabilityVerification(False, "malformed_token", "Malformed capability token segments.")

    if not hmac.compare_digest(signature, _sign(encoded, resolved)):
        return CapabilityVerification(False, "bad_signature", "Capability signature does not verify.")

    json_str = _b64url_decode(encoded)
    if json_str is None:
        return CapabilityVerification(False, "malformed_payload", "Capability payload is not valid base64url.")
    try:
        payload = json.loads(json_str)
    except Exception:
        return CapabilityVerification(False, "malformed_payload", "Capability payload is not valid JSON.")
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("intent_id"), str)
        or not payload.get("intent_id")
        or not isinstance(payload.get("nonce"), str)
        or not payload.get("nonce")
        or not isinstance(payload.get("exp"), (int, float))
    ):
        return CapabilityVerification(
            False, "malformed_payload", "Capability payload must carry intent_id, scope, exp and nonce."
        )
    scope = payload.get("scope")
    if not (isinstance(scope, str) or (isinstance(scope, list) and all(isinstance(s, str) for s in scope))):
        return CapabilityVerification(False, "malformed_payload", "scope must be a string or list of strings.")

    now_seconds = now if now is not None else time.time()
    if payload["exp"] <= now_seconds - clock_skew_seconds:
        return CapabilityVerification(False, "expired", f"Capability expired at {payload['exp']}.")

    if intent_id is not None and payload["intent_id"] != intent_id:
        return CapabilityVerification(
            False, "intent_mismatch", f"Capability is bound to intent {payload['intent_id']}, not {intent_id}."
        )

    if required_scope is not None:
        granted = scope == required_scope if isinstance(scope, str) else required_scope in scope
        if not granted:
            return CapabilityVerification(False, "scope_not_granted", f"Capability does not grant scope {required_scope}.")

    return CapabilityVerification(True, payload=payload)
