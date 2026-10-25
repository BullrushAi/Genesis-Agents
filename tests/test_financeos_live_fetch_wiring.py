"""Tests for main.py's `_resolve_financeos_governed_context` -- the exact
branch `/agents/{slug}/run` calls for finance specialists. Proves:
  - the pre-existing local/test `financeos_context` path is untouched when
    `financeos_live_fetch` is also absent (backward compatibility);
  - an explicit `financeos_context` always wins over `financeos_live_fetch`
    (no silent merge of two envelopes);
  - `financeos_live_fetch` alone drives a real (mocked-transport) live call
    and the result reaches the same governed binder.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import main


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


def _body(**kwargs):
    defaults = {"financeos_context": None, "financeos_live_fetch": None, "input": None, "task": None}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_no_envelope_and_no_live_fetch_is_pre_existing_behavior() -> None:
    governed = await main._resolve_financeos_governed_context("genesis-fin-revenue", _body())
    assert governed.ok is True
    assert governed.certification_status == "ABSENT"
    assert governed.context == {}


@pytest.mark.asyncio
async def test_explicit_local_context_still_works_unchanged() -> None:
    governed = await main._resolve_financeos_governed_context(
        "genesis-fin-revenue",
        _body(financeos_context={"entity": "NES Health LLC", "certification_status": "CERTIFIED"}),
    )
    assert governed.ok is True
    assert governed.context["entity"] == "NES Health LLC"
    assert governed.certification_status == "CERTIFIED"


@pytest.mark.asyncio
async def test_explicit_local_context_wins_over_live_fetch_no_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    """financeos_live_fetch must be completely ignored when financeos_context
    is also present -- proves this is precedence, not a merge."""
    async def must_not_be_called(**kwargs):
        raise AssertionError("fetch_live_financeos_context should not run when financeos_context is set")

    monkeypatch.setattr(main, "fetch_live_financeos_context", must_not_be_called)
    governed = await main._resolve_financeos_governed_context(
        "genesis-fin-revenue",
        _body(
            financeos_context={"entity": "local-wins"},
            financeos_live_fetch={"entity_id": "22222222-3333-4444-5555-666666666666"},
        ),
    )
    assert governed.ok is True
    assert governed.context["entity"] == "local-wins"


@pytest.mark.asyncio
async def test_live_fetch_alone_drives_a_real_call_reachable_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "FINANCEOS_API_BASE_URL",
        "https://e4l-financeos-api.orangedune-dad71fcc.westus2.azurecontainerapps.io",
    )
    monkeypatch.setenv("AGENT_CAPABILITY_SIGNING_SECRET", "x" * 32)

    import accounting.financeos_data_products_client as client

    fake = FakeSession(FakeResp(200, '{"revenue":[],"count":0}'))
    fake_aiohttp = SimpleNamespace(ClientSession=lambda **k: fake, ClientTimeout=lambda **k: None)
    monkeypatch.setattr(client, "aiohttp", fake_aiohttp)

    governed = await main._resolve_financeos_governed_context(
        "genesis-fin-revenue",
        _body(financeos_live_fetch={"entity_id": "22222222-3333-4444-5555-666666666666"}),
    )
    assert governed.ok is True
    assert governed.certification_status == "ABSENT"
    assert "live_certified_layer_reachable_no_rows" in governed.source_authority_flags
    assert "empty_is_not_an_error_do_not_invent_a_figure" in governed.source_authority_flags


@pytest.mark.asyncio
async def test_live_fetch_ignored_for_master_os_slugs() -> None:
    """financeos_live_fetch is only meaningful for FINANCE_AGENT_IDS -- the
    route itself gates on `canonical_slug in FINANCE_AGENT_IDS` before ever
    calling this resolver, so a MasterOS slug never reaches it. This test
    documents that boundary rather than re-deriving it."""
    from canonical_result_contract import FINANCE_AGENT_IDS

    assert "genesis-knowledge-evidence" not in FINANCE_AGENT_IDS
    assert "genesis-fin-revenue" in FINANCE_AGENT_IDS


@pytest.mark.asyncio
async def test_empty_live_fetch_dict_is_treated_as_absent() -> None:
    governed = await main._resolve_financeos_governed_context(
        "genesis-fin-revenue", _body(financeos_live_fetch={}),
    )
    assert governed.ok is True
    assert governed.context == {}
