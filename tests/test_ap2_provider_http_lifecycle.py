"""The AP2 provider polls every credential every few seconds for the life of the process. Building an httpx client per
request re-creates the TLS context and a fresh connection each time (the memory-growth suspect on the 512Mi gateway), so
the provider must own ONE long-lived client, reuse it for every poll and credential refresh, and release it at shutdown."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import ap2_provider as ap  # noqa: E402
from canonical_profiles import CANONICAL_SLUGS  # noqa: E402

CERT_URL = "https://swarmsync-api-cert-staging.onrender.com"
AGENT_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SWARMSYNC_API_URL", "https://api.swarmsync.ai")
    monkeypatch.setenv("AGENT_GATEWAY_SECRET", "prod-gateway-secret-0123456789")
    monkeypatch.setenv("INTERNAL_SECRET", "prod-internal-secret-0123456789")
    monkeypatch.setenv(
        "SWARMSYNC_ENVIRONMENTS",
        json.dumps([{"name": "cert-staging", "api_url": CERT_URL,
                     "gateway_secret": "cert-gateway-secret-0123456789", "internal_secret": "cert-internal-secret-0123456789"}]),
    )


def test_provider_reuses_one_http_client_for_every_poll(monkeypatch):
    constructed: list[httpx.AsyncClient] = []
    requests: list[str] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json=[])

    def counting_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        client = real_client(*args, **kwargs)
        constructed.append(client)
        return client

    monkeypatch.setattr(ap.httpx, "AsyncClient", counting_client)

    slug = sorted(CANONICAL_SLUGS)[0]
    creds = [ap.AgentCredential("cert-staging", slug, AGENT_ID, "secret-key", frozenset({ap.SELLER}))]

    async def scenario() -> None:
        provider = ap.Ap2Provider(creds, lambda *a, **k: None, ap.RuntimeKey(Ed25519PrivateKey.generate(), True), "sha")
        for _ in range(25):
            await provider.tick()
        assert provider._http is not None and not provider._http.is_closed
        await provider.aclose()
        assert constructed[0].is_closed

    asyncio.run(scenario())

    assert len(requests) == 25, "every tick still polls its credential"
    assert len(constructed) == 1, f"expected one shared client for 25 polls, built {len(constructed)}"


def test_credential_refresh_uses_the_shared_client(monkeypatch):
    constructed: list[httpx.AsyncClient] = []
    seen: list[str] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/ap2/gateway/agent-credential"):
            return httpx.Response(201, json={"apiKey": "fresh-key"})
        return httpx.Response(200, json=[])

    def counting_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        client = real_client(*args, **kwargs)
        constructed.append(client)
        return client

    monkeypatch.setattr(ap.httpx, "AsyncClient", counting_client)

    slug = sorted(CANONICAL_SLUGS)[0]
    managed = [("cert-staging", slug, AGENT_ID, frozenset({ap.SELLER}))]

    async def scenario() -> None:
        provider = ap.Ap2Provider([], lambda *a, **k: None, ap.RuntimeKey(Ed25519PrivateKey.generate(), True), "sha", managed=managed)
        for _ in range(5):
            await provider.tick()
        await provider.aclose()

    asyncio.run(scenario())

    assert any(p.endswith("/ap2/gateway/agent-credential") for p in seen), "the managed credential was fetched"
    assert any(p.endswith("/ap2/negotiations/my") for p in seen), "the refreshed credential was used to poll"
    assert len(constructed) == 1, f"credential fetch and polls must share one client, built {len(constructed)}"
