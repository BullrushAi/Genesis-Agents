"""Tests for the FinanceOS governed-layer read client (data-products/lineage)."""
from __future__ import annotations

import pytest

from accounting.financeos_data_products_client import (
    data_products_configured,
    get_health,
    get_lineage,
    get_revenue_data_product_by_id,
    get_revenue_data_products,
)

BASE_URL = "https://e4l-financeos-api.orangedune-dad71fcc.westus2.azurecontainerapps.io"
SECRET = "x" * 32


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
        self.last_url: str | None = None
        self.last_headers: dict | None = None

    def get(self, url, headers=None, **kwargs):
        self.last_url = url
        self.last_headers = headers
        return self._resp

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_not_configured_without_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINANCEOS_API_BASE_URL", raising=False)
    monkeypatch.delenv("AGENT_CAPABILITY_SIGNING_SECRET", raising=False)
    assert data_products_configured() is False
    result = await get_revenue_data_products()
    assert result["ok"] is False
    assert result["error"] == "financeos_data_products_not_configured"


@pytest.mark.asyncio
async def test_not_configured_without_secret_even_with_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCEOS_API_BASE_URL", BASE_URL)
    monkeypatch.delenv("AGENT_CAPABILITY_SIGNING_SECRET", raising=False)
    assert data_products_configured() is False
    result = await get_revenue_data_products()
    assert result["ok"] is False
    assert result["error"] == "financeos_data_products_not_configured"


@pytest.mark.asyncio
async def test_health_does_not_require_capability_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/health` is unauthenticated -- must work even with no signing secret."""
    monkeypatch.setenv("FINANCEOS_API_BASE_URL", BASE_URL)
    monkeypatch.delenv("AGENT_CAPABILITY_SIGNING_SECRET", raising=False)
    fake = FakeSession(FakeResp(200, '{"status":"ok","db":true,"commit":"13fe6c1"}'))
    result = await get_health(session=fake)
    assert result["ok"] is True
    assert result["commit"] == "13fe6c1"


@pytest.mark.asyncio
async def test_revenue_list_success_with_empty_certified_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The honest live state: 200 + empty list is success, not an error."""
    monkeypatch.setenv("FINANCEOS_API_BASE_URL", BASE_URL)
    monkeypatch.setenv("AGENT_CAPABILITY_SIGNING_SECRET", SECRET)
    fake = FakeSession(FakeResp(200, '{"revenue":[],"count":0}'))
    result = await get_revenue_data_products(entity_id="22222222-3333-4444-5555-666666666666", session=fake)
    assert result["ok"] is True
    assert result["revenue"] == []
    assert result["count"] == 0
    assert fake.last_headers is not None
    assert fake.last_headers["Authorization"].startswith("Bearer ")


@pytest.mark.asyncio
async def test_revenue_list_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCEOS_API_BASE_URL", BASE_URL)
    monkeypatch.setenv("AGENT_CAPABILITY_SIGNING_SECRET", SECRET)
    fake = FakeSession(FakeResp(401, '{"error":"unauthorized","message":"That capability token is not valid for this request."}'))
    result = await get_revenue_data_products(session=fake)
    assert result["ok"] is False
    assert result["status"] == 401
    assert result["error"] == "financeos_unauthorized"


@pytest.mark.asyncio
async def test_revenue_by_id_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCEOS_API_BASE_URL", BASE_URL)
    monkeypatch.setenv("AGENT_CAPABILITY_SIGNING_SECRET", SECRET)
    fake = FakeSession(FakeResp(404, '{"error":"not_found"}'))
    result = await get_revenue_data_product_by_id("99999999-9999-9999-9999-999999999999", session=fake)
    assert result["ok"] is False
    assert result["status"] == 404


@pytest.mark.asyncio
async def test_lineage_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCEOS_API_BASE_URL", BASE_URL)
    monkeypatch.setenv("AGENT_CAPABILITY_SIGNING_SECRET", SECRET)
    fake = FakeSession(FakeResp(200, '{"trace":{"complete":true,"steps":[]}}'))
    result = await get_lineage("certified_snapshots:11111111-1111-1111-1111-111111111111", session=fake)
    assert result["ok"] is True
    assert result["trace"]["complete"] is True


@pytest.mark.asyncio
async def test_transport_error_is_distinct_from_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCEOS_API_BASE_URL", BASE_URL)
    monkeypatch.setenv("AGENT_CAPABILITY_SIGNING_SECRET", SECRET)

    import aiohttp

    class RaisingSession:
        def get(self, *a, **k):
            raise aiohttp.ClientConnectionError("connection refused")

        async def close(self):
            return None

    result = await get_revenue_data_products(session=RaisingSession())
    assert result["ok"] is False
    assert result["error"] == "financeos_transport_error"
