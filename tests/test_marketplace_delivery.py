"""Marketplace negotiate delivers accepted work back to the calling SwarmSync environment."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx

import ap2_delivery
import main  # noqa: E402

PROD_URL = "https://api.swarmsync.ai"
CERT_URL = "https://swarmsync-api-cert-staging.onrender.com"
PROD_GW = "prod-gateway-secret-0123456789"
CERT_GW = "cert-gateway-secret-0123456789"
CERT_INT = "cert-internal-secret-0123456789"
PROD_INT = "prod-internal-secret-0123456789"
API_KEY = "genesis-api-key-for-direct-callers"
MARKET_SLUG = "csv-data-cleaner"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SWARMSYNC_API_URL", PROD_URL)
    monkeypatch.setenv("AGENT_GATEWAY_SECRET", PROD_GW)
    monkeypatch.setenv("INTERNAL_SECRET", PROD_INT)
    monkeypatch.setenv("GATEWAY_API_KEY", API_KEY)
    monkeypatch.setenv(
        "SWARMSYNC_ENVIRONMENTS",
        json.dumps([
            {"name": "cert-staging", "api_url": CERT_URL, "gateway_secret": CERT_GW, "internal_secret": CERT_INT}
        ]),
    )
    deliveries: list[dict] = []

    async def fake_eval(slug, request):
        return {"status": "ACCEPTED", "price": request.budget, "notes": "ok"}

    async def fake_work(**kwargs):
        return ap2_delivery.WorkResult(
            content="name,email\nJane Smith,jane@example.com\n",
            sources=["inline CSV data"],
            content_hash="abc123",
            elapsed_seconds=0.01,
            metadata={"rows": 2, "columns": 2},
        )

    async def fake_deliver(**kwargs):
        deliveries.append(kwargs)
        return ap2_delivery.DeliveryResult(success=True, delivery_id="delivery-1", response_code=201)

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(main, "evaluate_negotiation", fake_eval)
    monkeypatch.setattr(main.httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(ap2_delivery, "execute_agent_work", fake_work)
    monkeypatch.setattr(ap2_delivery, "deliver_to_swarmsync", fake_deliver)
    monkeypatch.setattr(ap2_delivery, "is_already_delivered", lambda negotiation_id: False)
    monkeypatch.setattr(ap2_delivery, "mark_delivered", lambda **kwargs: True)
    return TestClient(main.app), deliveries


def _body(callback_url: str, budget: float = 1.0) -> dict:
    return {
        "event": "negotiation.requested",
        "negotiation_id": "neg-market-1",
        "requester_agent_id": "req-1",
        "requester_agent_name": "Buyer",
        "responder_agent_id": "8e46cc30-fcea-4de5-8305-a744c7ec55df",
        "requested_service": "Clean this CSV",
        "budget": budget,
        "requirements": {"csv_data": "name,email\n  Jane Smith , jane@example.com \n"},
        "callback_url": callback_url,
    }


def test_marketplace_slug_can_negotiate_and_delivers_to_the_calling_environment(client):
    c, deliveries = client
    response = c.post(
        f"/agents/{MARKET_SLUG}/negotiate",
        json=_body(f"{CERT_URL}/ap2/gateway/respond"),
        headers={"x-agent-gateway-secret": CERT_GW},
    )
    assert response.status_code == 202
    assert len(deliveries) == 1
    assert deliveries[0]["api_url"] == CERT_URL
    assert deliveries[0]["gateway_secret"] == CERT_GW
    assert deliveries[0]["is_success"] is True


def test_a_countered_price_is_not_delivered(client, monkeypatch):
    c, deliveries = client

    async def counter(slug, request):
        return {"status": "ACCEPTED", "price": request.budget + 5, "notes": "too low"}

    monkeypatch.setattr(main, "evaluate_negotiation", counter)
    response = c.post(
        f"/agents/{MARKET_SLUG}/negotiate",
        json=_body(f"{CERT_URL}/ap2/gateway/respond"),
        headers={"x-agent-gateway-secret": CERT_GW},
    )
    assert response.status_code == 202
    assert deliveries == []


def test_callback_to_another_environment_is_still_refused(client):
    c, deliveries = client
    response = c.post(
        f"/agents/{MARKET_SLUG}/negotiate",
        json=_body(f"{PROD_URL}/ap2/gateway/respond"),
        headers={"x-agent-gateway-secret": CERT_GW},
    )
    assert response.status_code == 400
    assert deliveries == []


def test_canonical_accept_does_not_use_marketplace_delivery(client):
    c, deliveries = client
    slug = sorted(__import__("canonical_profiles").CANONICAL_SLUGS)[0]
    response = c.post(
        f"/agents/{slug}/negotiate",
        json=_body(f"{CERT_URL}/ap2/gateway/respond"),
        headers={"x-agent-gateway-secret": CERT_GW},
    )
    assert response.status_code == 202
    assert deliveries == []


def test_private_csv_url_is_refused():
    with pytest.raises(ValueError, match="public https URL"):
        ap2_delivery._assert_public_https_url("http://127.0.0.1/data.csv")
    with pytest.raises(ValueError, match="public https URL"):
        ap2_delivery._assert_public_https_url("https://10.0.0.8/data.csv")
