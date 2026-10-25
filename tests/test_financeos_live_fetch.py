"""Tests for the optional live-fetch path added to
`runtime/financeos_governed_input.py`, in addition to the pre-existing
local/test `financeos_context` envelope. These prove Genesis correctly
distinguishes:
  - a live, reachable, CERTIFIED data product (`reachable_found`)
  - a live, reachable, EMPTY certified layer (`reachable_empty` -- honest
    success, never an error, never a fabricated figure)
  - an unreachable/unauthorized/unconfigured/malformed-request endpoint (real
    failures, never collapsed into "no data")
and that all three still flow through the SAME `bind_financeos_governed_context`
validation the local/test path already uses -- no second, looser gate.
"""
from __future__ import annotations

import pytest

from runtime.financeos_governed_input import (
    LIVE_FETCH_FAILURE_STATES,
    bind_financeos_governed_context,
    fetch_live_financeos_context,
)

ENTITY_ID = "22222222-3333-4444-5555-666666666666"


class FakeResp:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, resp: FakeResp):
        self._resp = resp

    def get(self, *a, **k):
        return self._resp

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_not_configured_is_distinct_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINANCEOS_API_BASE_URL", raising=False)
    monkeypatch.delenv("AGENT_CAPABILITY_SIGNING_SECRET", raising=False)
    ctx = await fetch_live_financeos_context(entity_id=ENTITY_ID)
    assert ctx == {"live_fetch_state": "not_configured"}
    bound = bind_financeos_governed_context("genesis-fin-revenue", ctx)
    assert bound.ok is True
    assert bound.certification_status == "ABSENT"
    assert "live_certified_layer_unreachable:not_configured" in bound.source_authority_flags
    assert "do_not_treat_unreachable_as_zero_or_absent_truth" in bound.source_authority_flags


@pytest.mark.asyncio
async def test_reachable_but_empty_is_honest_success_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCEOS_API_BASE_URL", "https://e4l-financeos-api.orangedune-dad71fcc.westus2.azurecontainerapps.io")
    monkeypatch.setenv("AGENT_CAPABILITY_SIGNING_SECRET", "x" * 32)
    fake = FakeSession(FakeResp(200, '{"revenue":[],"count":0}'))
    ctx = await fetch_live_financeos_context(entity_id=ENTITY_ID, session=fake)
    assert ctx == {"live_fetch_state": "reachable_empty"}
    bound = bind_financeos_governed_context("genesis-fin-revenue", ctx)
    assert bound.ok is True
    assert bound.certification_status == "ABSENT"
    assert "live_certified_layer_reachable_no_rows" in bound.source_authority_flags
    assert "empty_is_not_an_error_do_not_invent_a_figure" in bound.source_authority_flags
    # Must NOT be conflated with an unreachable/unauthorized state.
    assert not (set(bound.source_authority_flags) & {f"live_certified_layer_unreachable:{s}" for s in LIVE_FETCH_FAILURE_STATES})


@pytest.mark.asyncio
async def test_unauthorized_is_distinct_from_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCEOS_API_BASE_URL", "https://e4l-financeos-api.orangedune-dad71fcc.westus2.azurecontainerapps.io")
    monkeypatch.setenv("AGENT_CAPABILITY_SIGNING_SECRET", "x" * 32)
    fake = FakeSession(FakeResp(401, '{"error":"unauthorized","message":"nope"}'))
    ctx = await fetch_live_financeos_context(entity_id=ENTITY_ID, session=fake)
    assert ctx == {"live_fetch_state": "unauthorized"}
    bound = bind_financeos_governed_context("genesis-fin-revenue", ctx)
    assert bound.ok is True
    assert "live_certified_layer_unreachable:unauthorized" in bound.source_authority_flags
    assert "live_certified_layer_reachable_no_rows" not in bound.source_authority_flags


@pytest.mark.asyncio
async def test_bad_request_is_invalid_request_not_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Found live 2026-08-29 against exact-SHA e137e26 on the deployed Render
    service: FinanceOS's entity_id query param must be a UUID (its own zod
    schema). Passing a human-readable entity name (the same convention the
    pre-existing local/test envelope's own `entity` field uses) got FinanceOS's
    400 "Invalid query parameters" -- which must NOT be reported to the caller
    as "unreachable" (that would misleadingly imply FinanceOS itself is down)."""
    monkeypatch.setenv("FINANCEOS_API_BASE_URL", "https://e4l-financeos-api.orangedune-dad71fcc.westus2.azurecontainerapps.io")
    monkeypatch.setenv("AGENT_CAPABILITY_SIGNING_SECRET", "x" * 32)
    fake = FakeSession(FakeResp(400, '{"error":"bad_request","message":"Invalid query parameters."}'))
    ctx = await fetch_live_financeos_context(entity_id="NES Health LLC", session=fake)
    assert ctx == {"live_fetch_state": "invalid_request"}
    bound = bind_financeos_governed_context("genesis-fin-revenue", ctx)
    assert bound.ok is True
    assert "live_certified_layer_unreachable:invalid_request" in bound.source_authority_flags
    assert "live_certified_layer_reachable_no_rows" not in bound.source_authority_flags


@pytest.mark.asyncio
async def test_transport_failure_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCEOS_API_BASE_URL", "https://e4l-financeos-api.orangedune-dad71fcc.westus2.azurecontainerapps.io")
    monkeypatch.setenv("AGENT_CAPABILITY_SIGNING_SECRET", "x" * 32)

    import aiohttp

    class RaisingSession:
        def get(self, *a, **k):
            raise aiohttp.ClientConnectionError("refused")

        async def close(self):
            return None

    ctx = await fetch_live_financeos_context(entity_id=ENTITY_ID, session=RaisingSession())
    assert ctx == {"live_fetch_state": "unreachable"}


@pytest.mark.asyncio
async def test_reachable_found_maps_row_without_recomputation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCEOS_API_BASE_URL", "https://e4l-financeos-api.orangedune-dad71fcc.westus2.azurecontainerapps.io")
    monkeypatch.setenv("AGENT_CAPABILITY_SIGNING_SECRET", "x" * 32)
    body = (
        '{"revenue":[{'
        '"id":"11111111-1111-1111-1111-111111111111",'
        '"data_product_version":3,'
        '"certification_status":"CERTIFIED",'
        '"reconciliation_status":"RECONCILED",'
        '"freshness_status":"FRESH",'
        '"updated_at":"2026-08-28T00:00:00Z",'
        '"entity_id":"22222222-3333-4444-5555-666666666666",'
        '"period_start_date":"2026-07-01",'
        '"period_end_date":"2026-07-31",'
        '"lineage_summary":{"source_node_keys":["xero:journal:1"]}'
        '}],"count":1}'
    )
    fake = FakeSession(FakeResp(200, body))
    ctx = await fetch_live_financeos_context(entity_id=ENTITY_ID, session=fake)
    assert ctx["live_fetch_state"] == "reachable_found"
    assert ctx["certification_status"] == "CERTIFIED"
    assert ctx["data_product_id"] == "11111111-1111-1111-1111-111111111111"
    assert ctx["period"] == "2026-07-01/2026-07-31"
    assert ctx["lineage"] == ["xero:journal:1"]
    bound = bind_financeos_governed_context("genesis-fin-revenue", ctx)
    assert bound.ok is True
    assert bound.certification_status == "CERTIFIED"
    assert "live_certified_layer_reachable_with_data" in bound.source_authority_flags


def test_invalid_live_fetch_state_is_rejected() -> None:
    bound = bind_financeos_governed_context(
        "genesis-fin-revenue", {"live_fetch_state": "made_up_state"},
    )
    assert bound.ok is False
    assert bound.reason == "financeos_live_fetch_state_invalid"
