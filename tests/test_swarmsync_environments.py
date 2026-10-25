"""One Genesis gateway, several SwarmSync environments.

Genesis is an independent provider gateway: production SwarmSync and certification staging must both be able
to transact with the *same* process. Every inbound transaction is authenticated by the calling environment's
own secret, and everything sent back (callback URL, secret) is derived from that environment alone.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402
import swarmsync_environments as envs  # noqa: E402
from verification_models import (  # noqa: E402
    VerificationContext,
    VerificationResult,
    VerificationSpec,
    VerifyRequest,
)

PROD_URL = "https://api.swarmsync.ai"
CERT_URL = "https://swarmsync-api-cert-staging.onrender.com"
PROD_GW, PROD_INT = "prod-gateway-secret-0123456789", "prod-internal-secret-0123456789"
CERT_GW, CERT_INT = "cert-gateway-secret-0123456789", "cert-internal-secret-0123456789"
API_KEY = "genesis-api-key-for-direct-callers"
SLUG = sorted(main.__dict__["CANONICAL_SLUGS"] if "CANONICAL_SLUGS" in main.__dict__ else __import__("canonical_profiles").CANONICAL_SLUGS)[0]


@pytest.fixture(autouse=True)
def two_environments(monkeypatch):
    monkeypatch.setenv("SWARMSYNC_API_URL", PROD_URL)
    monkeypatch.setenv("AGENT_GATEWAY_SECRET", PROD_GW)
    monkeypatch.setenv("INTERNAL_SECRET", PROD_INT)
    monkeypatch.setenv("GATEWAY_API_KEY", API_KEY)
    monkeypatch.setenv(
        "SWARMSYNC_ENVIRONMENTS",
        json.dumps([{"name": "cert-staging", "api_url": CERT_URL, "gateway_secret": CERT_GW, "internal_secret": CERT_INT}]),
    )


class TestRegistry:
    def test_both_environments_are_trusted_and_distinct(self):
        names = {e.name: e for e in envs.environments()}
        assert set(names) == {"default", "cert-staging"}
        assert names["default"].api_url == PROD_URL
        assert names["cert-staging"].api_url == CERT_URL

    def test_secret_identifies_the_environment(self):
        assert envs.resolve_by_gateway_secret(PROD_GW).name == "default"
        assert envs.resolve_by_gateway_secret(CERT_GW).name == "cert-staging"
        assert envs.resolve_by_internal_secret(CERT_INT).name == "cert-staging"
        # the two secret kinds are not interchangeable
        assert envs.resolve_by_gateway_secret(CERT_INT) is None
        assert envs.resolve_by_internal_secret(CERT_GW) is None

    @pytest.mark.parametrize("presented", [None, "", "nope", PROD_GW + "x", PROD_GW[:-1]])
    def test_unknown_or_empty_secret_matches_nothing(self, presented):
        assert envs.resolve_by_gateway_secret(presented) is None
        assert envs.resolve_by_internal_secret(presented) is None

    def test_unset_default_secret_never_matches_empty_header(self, monkeypatch):
        monkeypatch.delenv("AGENT_GATEWAY_SECRET")
        monkeypatch.delenv("INTERNAL_SECRET")
        assert envs.resolve_by_gateway_secret("") is None
        assert envs.resolve_by_internal_secret("") is None
        assert envs.resolve_by_gateway_secret(CERT_GW).name == "cert-staging"

    @pytest.mark.parametrize(
        "entry",
        [
            {"name": "default", "api_url": CERT_URL, "gateway_secret": CERT_GW, "internal_secret": CERT_INT},
            {"name": "x", "api_url": "http://not-local.example.com", "gateway_secret": CERT_GW, "internal_secret": CERT_INT},
            {"name": "x", "api_url": CERT_URL + "/some/path", "gateway_secret": CERT_GW, "internal_secret": CERT_INT},
            {"name": "x", "api_url": PROD_URL, "gateway_secret": CERT_GW, "internal_secret": CERT_INT},  # duplicate origin
            {"name": "x", "api_url": CERT_URL, "gateway_secret": "short", "internal_secret": CERT_INT},
            {"name": "x", "api_url": CERT_URL, "gateway_secret": PROD_GW, "internal_secret": CERT_INT},  # ambiguous identity
            {"name": "x", "api_url": CERT_URL, "gateway_secret": CERT_GW, "internal_secret": PROD_INT},  # ambiguous identity
            "not-an-object",
        ],
    )
    def test_invalid_extra_entry_is_skipped_and_never_weakens_default(self, monkeypatch, entry):
        monkeypatch.setenv("SWARMSYNC_ENVIRONMENTS", json.dumps([entry]))
        assert [e.name for e in envs.environments()] == ["default"]
        assert envs.resolve_by_gateway_secret(PROD_GW).name == "default"

    def test_malformed_json_keeps_only_default(self, monkeypatch):
        monkeypatch.setenv("SWARMSYNC_ENVIRONMENTS", "{not json")
        assert [e.name for e in envs.environments()] == ["default"]

    def test_no_extra_config_is_exactly_the_legacy_behaviour(self, monkeypatch):
        monkeypatch.delenv("SWARMSYNC_ENVIRONMENTS")
        assert [e.name for e in envs.environments()] == ["default"]

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://API.SwarmSync.ai/ap2/gateway/respond", "https://api.swarmsync.ai"),
            ("https://api.swarmsync.ai:443/x", "https://api.swarmsync.ai"),
            ("https://api.swarmsync.ai:8443/x", "https://api.swarmsync.ai:8443"),
            ("http://localhost:3000/x", "http://localhost:3000"),
            ("http://api.swarmsync.ai/x", None),
            ("https://api.swarmsync.ai@evil.example.com/x", None),
            ("https://user:pw@api.swarmsync.ai/x", None),
            ("ftp://api.swarmsync.ai/x", None),
            ("not a url", None),
            ("", None),
        ],
    )
    def test_origin_of(self, url, expected):
        assert envs.origin_of(url) == expected


class TestCallbackBinding:
    def test_callback_must_target_the_authenticated_environment(self):
        cert = envs.resolve_by_gateway_secret(CERT_GW)
        assert envs.assert_callback_allowed(f"{CERT_URL}/ap2/gateway/respond", cert) is cert
        with pytest.raises(ValueError):
            envs.assert_callback_allowed(f"{PROD_URL}/ap2/gateway/respond", cert)

    @pytest.mark.parametrize(
        "bad",
        [
            "https://api.swarmsync.ai.evil.example.com/ap2/gateway/respond",
            "https://evil.example.com/ap2/gateway/respond",
            "https://api.swarmsync.ai@evil.example.com/ap2/gateway/respond",
            "http://api.swarmsync.ai/ap2/gateway/respond",
            "https://api.swarmsync.ai:9999/ap2/gateway/respond",
        ],
    )
    def test_lookalike_and_credentialed_origins_are_refused(self, bad):
        with pytest.raises(ValueError):
            envs.assert_callback_allowed(bad, envs.resolve_by_gateway_secret(PROD_GW))
        with pytest.raises(ValueError):
            envs.assert_callback_allowed(bad, None)

    def test_without_an_authenticated_environment_the_origin_picks_it(self):
        assert envs.assert_callback_allowed(f"{CERT_URL}/x", None).name == "cert-staging"
        assert envs.assert_callback_allowed(f"{PROD_URL}/x", None).name == "default"

    def test_job_callbacks_carry_only_the_destination_environments_secret(self):
        assert envs.internal_secret_for_callback(f"{CERT_URL}/internal/job-callback") == CERT_INT
        assert envs.internal_secret_for_callback(f"{PROD_URL}/internal/job-callback") == PROD_INT
        assert envs.internal_secret_for_callback("https://elsewhere.example.com/hook") == ""


class _Posts:
    def __init__(self):
        self.calls: list[dict] = []


def _fake_async_client(posts: _Posts, status: int = 200):
    class _Resp:
        status_code = status
        text = "ok"

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None, **k):
            posts.calls.append({"url": url, "json": json, "headers": dict(headers or {})})
            return _Resp()

    return FakeClient


def _negotiate_body(callback_url: str) -> dict:
    return {
        "event": "negotiation.requested",
        "negotiation_id": "neg-1",
        "requester_agent_id": "req-1",
        "requester_agent_name": "Cato",
        "responder_agent_id": "resp-1",
        "requested_service": "audit",
        "budget": 0.05,
        "callback_url": callback_url,
    }


@pytest.fixture
def client(monkeypatch):
    posts = _Posts()
    monkeypatch.setattr(main.httpx, "AsyncClient", _fake_async_client(posts))

    async def accept(slug, request):
        return {"status": "ACCEPTED", "price": request.budget, "notes": "ok"}

    monkeypatch.setattr(main, "evaluate_negotiation", accept)
    return TestClient(main.app), posts


class TestNegotiateIsEnvironmentAgnostic:
    def test_cert_staging_and_production_transact_with_the_same_process(self, client):
        c, posts = client
        r1 = c.post(f"/agents/{SLUG}/negotiate", json=_negotiate_body(f"{CERT_URL}/ap2/gateway/respond"),
                    headers={"x-agent-gateway-secret": CERT_GW})
        r2 = c.post(f"/agents/{SLUG}/negotiate", json=_negotiate_body(f"{PROD_URL}/ap2/gateway/respond"),
                    headers={"x-agent-gateway-secret": PROD_GW})
        assert r1.status_code == 202 and r2.status_code == 202
        by_url = {p["url"]: p for p in posts.calls}
        assert by_url[f"{CERT_URL}/ap2/gateway/respond"]["headers"]["x-agent-gateway-secret"] == CERT_GW
        assert by_url[f"{PROD_URL}/ap2/gateway/respond"]["headers"]["x-agent-gateway-secret"] == PROD_GW

    def test_a_secret_is_never_sent_to_the_other_environments_origin(self, client):
        c, posts = client
        r = c.post(f"/agents/{SLUG}/negotiate", json=_negotiate_body(f"{PROD_URL}/ap2/gateway/respond"),
                   headers={"x-agent-gateway-secret": CERT_GW})
        assert r.status_code == 400 and r.json()["detail"] == "untrusted_callback_origin"
        r = c.post(f"/agents/{SLUG}/negotiate", json=_negotiate_body(f"{CERT_URL}/ap2/gateway/respond"),
                   headers={"x-agent-gateway-secret": PROD_GW})
        assert r.status_code == 400
        assert posts.calls == []

    def test_unregistered_callback_origin_is_refused_for_any_caller(self, client):
        c, posts = client
        for headers in ({"x-agent-gateway-secret": PROD_GW}, {"x-agent-api-key": API_KEY}):
            r = c.post(f"/agents/{SLUG}/negotiate", json=_negotiate_body("https://evil.example.com/steal"), headers=headers)
            assert r.status_code == 400
        assert posts.calls == []

    def test_api_key_caller_gets_the_environment_that_owns_the_callback_origin(self, client):
        c, posts = client
        r = c.post(f"/agents/{SLUG}/negotiate", json=_negotiate_body(f"{CERT_URL}/ap2/gateway/respond"),
                   headers={"x-agent-api-key": API_KEY})
        assert r.status_code == 202
        assert posts.calls[0]["headers"]["x-agent-gateway-secret"] == CERT_GW

    def test_unauthenticated_and_unknown_secrets_are_still_rejected(self, client):
        c, posts = client
        assert c.post(f"/agents/{SLUG}/negotiate", json=_negotiate_body(f"{CERT_URL}/x")).status_code == 401
        assert c.post(f"/agents/{SLUG}/negotiate", json=_negotiate_body(f"{CERT_URL}/x"),
                      headers={"x-agent-gateway-secret": "wrong"}).status_code == 401
        assert posts.calls == []


def _verify_body(verification_id: str) -> dict:
    return VerifyRequest(
        spec=VerificationSpec(inline_content="audit done"),
        context=VerificationContext(escrow_ref="esc-1", verification_id=verification_id),
    ).model_dump()


class TestVerificationCallbacks:
    @pytest.fixture(autouse=True)
    def durable_job_store(self, migrated_postgres_url, monkeypatch):
        """/verify jobs are durable now (genesis_verification_jobs): these tests run against real PostgreSQL."""
        monkeypatch.setenv("GENESIS_JOB_DATABASE_URL", migrated_postgres_url)
        monkeypatch.setenv("GENESIS_STORE_BACKEND", "postgres")
        main.reset_in_memory_state_for_tests()
        # TestClient's per-request event loop dies with the request, which would cancel the detached verification
        # task: run it to completion in its own loop instead (the real server keeps the task on the server loop).
        import asyncio
        import threading

        def run_inline(coro):
            worker = threading.Thread(target=asyncio.run, args=(coro,))
            worker.start()
            worker.join()

        monkeypatch.setattr(main, "_spawn_verification_task", run_inline)
        from runtime import pg_store

        with pg_store.transaction() as cur:  # these tests reuse fixed verification ids
            cur.execute("TRUNCATE genesis_verification_jobs")

    def _stub_verifier(self, monkeypatch):
        async def fake_run_verification(session_id, spec, context):
            return VerificationResult(passed=True, proof_hash="ab" * 32, conduit_session_sig="cd" * 32, extracted_content="x")

        monkeypatch.setattr(main, "run_verification", fake_run_verification)

    def test_each_environments_verify_job_calls_back_to_itself_with_its_own_secret(self, client, monkeypatch):
        c, posts = client
        self._stub_verifier(monkeypatch)
        a = c.post("/verify", json=_verify_body("ver-cert"), headers={"X-Internal-Secret": CERT_INT})
        b = c.post("/verify", json=_verify_body("ver-prod"), headers={"X-Internal-Secret": PROD_INT})
        assert a.status_code == 202 and b.status_code == 202
        by_url = {p["url"]: p for p in posts.calls}
        assert by_url[f"{CERT_URL}/conduit/verifications/ver-cert/callback"]["headers"]["X-Internal-Secret"] == CERT_INT
        assert by_url[f"{PROD_URL}/conduit/verifications/ver-prod/callback"]["headers"]["X-Internal-Secret"] == PROD_INT
        assert len(posts.calls) == 2
        assert all(p["json"]["passed"] is True for p in posts.calls)

    def test_failed_job_error_callback_also_stays_in_its_environment(self, client, monkeypatch):
        c, posts = client

        async def boom(session_id, spec, context):
            raise RuntimeError("browser exploded")

        monkeypatch.setattr(main, "run_verification", boom)
        r = c.post("/verify", json=_verify_body("ver-cert-err"), headers={"X-Internal-Secret": CERT_INT})
        assert r.status_code == 202
        assert [p["url"] for p in posts.calls] == [f"{CERT_URL}/conduit/verifications/ver-cert-err/callback"]
        assert posts.calls[0]["headers"]["X-Internal-Secret"] == CERT_INT
        assert posts.calls[0]["json"]["passed"] is False

    def test_wrong_or_missing_internal_secret_is_rejected_and_nothing_is_sent(self, client):
        c, posts = client
        assert c.post("/verify", json=_verify_body("v"), headers={"X-Internal-Secret": CERT_GW}).status_code == 401
        assert c.post("/verify", json=_verify_body("v")).status_code == 401
        assert posts.calls == []

    def test_a_job_is_visible_only_to_the_environment_that_created_it(self, client, monkeypatch):
        c, _ = client
        self._stub_verifier(monkeypatch)
        job_id = c.post("/verify", json=_verify_body("ver-iso"), headers={"X-Internal-Secret": CERT_INT}).json()["jobId"]
        assert c.get(f"/verify/{job_id}", headers={"X-Internal-Secret": CERT_INT}).status_code == 200
        assert c.get(f"/verify/{job_id}", headers={"X-Internal-Secret": PROD_INT}).status_code == 404

    def test_legacy_single_environment_deployment_is_unchanged(self, client, monkeypatch):
        c, posts = client
        monkeypatch.delenv("SWARMSYNC_ENVIRONMENTS")
        self._stub_verifier(monkeypatch)
        assert c.post("/verify", json=_verify_body("ver-legacy"), headers={"X-Internal-Secret": PROD_INT}).status_code == 202
        assert [p["url"] for p in posts.calls] == [f"{PROD_URL}/conduit/verifications/ver-legacy/callback"]
        assert c.post("/verify", json=_verify_body("v"), headers={"X-Internal-Secret": CERT_INT}).status_code == 401


class TestGatewayGuardAcceptsEveryEnvironment:
    @pytest.mark.asyncio
    async def test_dependency_accepts_each_environments_gateway_secret(self):
        await main.verify_gateway_key(x_agent_api_key=None, x_agent_gateway_secret=PROD_GW)
        await main.verify_gateway_key(x_agent_api_key=None, x_agent_gateway_secret=CERT_GW)
        with pytest.raises(main.HTTPException) as exc:
            await main.verify_gateway_key(x_agent_api_key=None, x_agent_gateway_secret=CERT_INT)
        assert exc.value.status_code == 401


class TestAsyncJobCallbacks:
    @pytest.mark.asyncio
    async def test_job_completion_callback_carries_only_its_destination_environments_secret(self, monkeypatch):
        import worker

        posts = _Posts()
        monkeypatch.setattr(worker.httpx, "AsyncClient", _fake_async_client(posts))
        assert await worker.fire_callback(f"{CERT_URL}/internal/job-callback", job_id="j1", status="DELIVERED")
        assert await worker.fire_callback(f"{PROD_URL}/internal/job-callback", job_id="j2", status="DELIVERED")
        assert await worker.fire_callback("https://hooks.example.com/job", job_id="j3", status="DELIVERED")
        sent = {p["url"]: p["headers"].get("X-Internal-Secret") for p in posts.calls}
        assert sent[f"{CERT_URL}/internal/job-callback"] == CERT_INT
        assert sent[f"{PROD_URL}/internal/job-callback"] == PROD_INT
        assert sent["https://hooks.example.com/job"] is None  # an unregistered origin never receives a secret
