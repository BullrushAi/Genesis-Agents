"""HTTP client for FinanceOS's governed-layer READ surface:

    GET /health
    GET /api/v1/data-products/revenue
    GET /api/v1/data-products/revenue/:id
    GET /api/v1/lineage/:node_ref

Same shape as `accounting/financeos_proofs_client.py` (configured-check,
`_get` helper, `{"ok": ...}` return contract, injectable `aiohttp.ClientSession`
for tests) but authenticates with an HMAC capability token
(`runtime/financeos_capability_token.py`) instead of a static bearer secret --
these routes require `requireAuth({scope: 'intent:read'})`
(`E4L-FinanceOS-Official/src/api/routes/{data-products,lineage}.ts`), not the
`/internal/proofs/*` service-token scheme.

This client NEVER writes. There is no POST/PUT/PATCH/DELETE method here, on
purpose -- matching the FinanceOS routes themselves, which are read-only by
design (see the "WHAT THIS IS NOT" header comment on both route files).

Distinguishing "reachable but certified layer is empty" from "unreachable" or
"unauthenticated" is the whole point of this module -- callers (see
`runtime/financeos_governed_input.py::fetch_live_financeos_context`) must
never collapse those into one generic failure, and must never invent a
revenue figure when the list comes back empty.
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlencode, urlparse

import aiohttp

from runtime.financeos_capability_token import (
    CapabilityConfigError,
    capability_signing_available,
    issue_capability_token,
)

DEFAULT_INTENT_ID = "genesis:financeos-governed-read"
DEFAULT_SCOPE = "intent:read"
DEFAULT_TTL_SECONDS = 120


def _base_url() -> str:
    return os.environ.get("FINANCEOS_API_BASE_URL", "").strip().rstrip("/")


def _allowed_base_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def data_products_configured() -> bool:
    """True only when both a base URL AND a capability-signing secret are
    present. Mirrors `financeos_proofs_configured()`'s posture: an unconfigured
    deployment must fail closed with a named reason, never a bare exception."""
    base = _base_url()
    return len(base) > 0 and _allowed_base_url(base) and capability_signing_available()


def _capability_headers(*, entity_id: str | None, subject: str) -> dict[str, str] | None:
    """Returns None (never a header dict with a garbage token) when signing
    is unavailable -- callers must check this before making the request."""
    if not capability_signing_available():
        return None
    try:
        token = issue_capability_token(
            intent_id=DEFAULT_INTENT_ID,
            scope=DEFAULT_SCOPE,
            ttl_seconds=DEFAULT_TTL_SECONDS,
            entity_id=entity_id,
            subject=subject,
        )
    except CapabilityConfigError:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


async def get_health(session: aiohttp.ClientSession | None = None) -> dict[str, Any]:
    """`GET /health` -- unauthenticated. Confirms the deployed commit and DB
    reachability; does not touch the governed-data-product surface."""
    base = _base_url()
    if not base or not _allowed_base_url(base):
        return {"ok": False, "error": "financeos_base_url_not_configured"}
    return await _get(f"{base}/health", headers={"Accept": "application/json"}, session=session)


async def get_revenue_data_products(
    *,
    entity_id: str | None = None,
    division_id: str | None = None,
    channel_id: str | None = None,
    currency: str | None = None,
    period_start_from: str | None = None,
    period_end_to: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    subject: str = "genesis-financeos-governed-input",
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """`GET /api/v1/data-products/revenue`.

    An empty `revenue: []` on a 200 is a SUCCESSFUL, honest result -- the
    certified layer is live and reachable, there is simply nothing certified
    yet for the query. Callers must not treat `count == 0` as an error."""
    if not data_products_configured():
        return {
            "ok": False,
            "error": "financeos_data_products_not_configured",
            "reason": "set FINANCEOS_API_BASE_URL and AGENT_CAPABILITY_SIGNING_SECRET (>=32 chars)",
        }
    headers = _capability_headers(entity_id=entity_id, subject=subject)
    if headers is None:
        return {"ok": False, "error": "financeos_capability_token_unavailable"}
    query: dict[str, str] = {}
    if entity_id:
        query["entity_id"] = entity_id
    if division_id:
        query["division_id"] = division_id
    if channel_id:
        query["channel_id"] = channel_id
    if currency:
        query["currency"] = currency
    if period_start_from:
        query["period_start_from"] = period_start_from
    if period_end_to:
        query["period_end_to"] = period_end_to
    if limit is not None:
        query["limit"] = str(limit)
    if offset is not None:
        query["offset"] = str(offset)
    base = _base_url()
    url = f"{base}/api/v1/data-products/revenue"
    if query:
        url = f"{url}?{urlencode(query)}"
    return await _get(url, headers=headers, session=session)


async def get_revenue_data_product_by_id(
    data_product_id: str,
    *,
    entity_id: str | None = None,
    subject: str = "genesis-financeos-governed-input",
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """`GET /api/v1/data-products/revenue/:id`."""
    if not data_products_configured():
        return {
            "ok": False,
            "error": "financeos_data_products_not_configured",
            "reason": "set FINANCEOS_API_BASE_URL and AGENT_CAPABILITY_SIGNING_SECRET (>=32 chars)",
        }
    headers = _capability_headers(entity_id=entity_id, subject=subject)
    if headers is None:
        return {"ok": False, "error": "financeos_capability_token_unavailable"}
    base = _base_url()
    return await _get(f"{base}/api/v1/data-products/revenue/{data_product_id}", headers=headers, session=session)


async def get_lineage(
    node_ref: str,
    *,
    entity_id: str | None = None,
    max_depth: int | None = None,
    max_nodes: int | None = None,
    subject: str = "genesis-financeos-governed-input",
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """`GET /api/v1/lineage/:node_ref`."""
    if not data_products_configured():
        return {
            "ok": False,
            "error": "financeos_data_products_not_configured",
            "reason": "set FINANCEOS_API_BASE_URL and AGENT_CAPABILITY_SIGNING_SECRET (>=32 chars)",
        }
    headers = _capability_headers(entity_id=entity_id, subject=subject)
    if headers is None:
        return {"ok": False, "error": "financeos_capability_token_unavailable"}
    query: dict[str, str] = {}
    if max_depth is not None:
        query["max_depth"] = str(max_depth)
    if max_nodes is not None:
        query["max_nodes"] = str(max_nodes)
    base = _base_url()
    url = f"{base}/api/v1/lineage/{node_ref}"
    if query:
        url = f"{url}?{urlencode(query)}"
    return await _get(url, headers=headers, session=session)


async def _get(
    url: str, *, headers: dict[str, str], session: aiohttp.ClientSession | None
) -> dict[str, Any]:
    owns = session is None
    if owns:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    try:
        assert session is not None
        async with session.get(url, headers=headers) as resp:
            data = await _read_json(resp)
            # FinanceOS's own body may carry its own `error` key (e.g.
            # "unauthorized") -- preserve it under `financeos_error` before
            # `**data` is spread, so this client's own `ok`/`status`/`error`
            # classification always wins. Losing that classification to
            # theirs would blur exactly the "unreachable vs unauthorized vs
            # not found" distinction this client exists to preserve.
            if "error" in data:
                data = {**data, "financeos_error": data["error"]}
            if resp.status == 200:
                return {**data, "ok": True, "status": resp.status}
            if resp.status == 401:
                return {**data, "ok": False, "status": resp.status, "error": "financeos_unauthorized"}
            if resp.status == 403:
                return {**data, "ok": False, "status": resp.status, "error": "financeos_forbidden"}
            if resp.status == 404:
                return {**data, "ok": False, "status": resp.status, "error": "financeos_not_found"}
            return {**data, "ok": False, "status": resp.status, "error": "financeos_unexpected_status"}
    except aiohttp.ClientError as exc:
        return {"ok": False, "error": "financeos_transport_error", "reason": str(exc)}
    finally:
        if owns:
            await session.close()


async def _read_json(resp: aiohttp.ClientResponse) -> dict[str, Any]:
    text = await resp.text()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"data": parsed}
    except json.JSONDecodeError:
        return {"raw": text}
