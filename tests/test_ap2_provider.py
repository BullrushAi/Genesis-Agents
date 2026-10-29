"""SSCERT-28: Genesis as a provider and buyer on the open SwarmSync marketplace.

The SwarmSync API is replaced by an in-memory fake at the HTTP boundary only (the client class); every
decision, hash, signature and state transition under test is real code. The contract is the one any outside
agent implements: its own agent key, ``/ap2/deliver`` for sold jobs, ``/ap2/negotiate`` + ``/ap2/respond``
for purchases, and no privileged gateway channel.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import ap2_provider as ap  # noqa: E402
from canonical_profiles import CANONICAL_SLUGS  # noqa: E402

CERT_URL = "https://swarmsync-api-cert-staging.onrender.com"
SELLER_SLUG = sorted(CANONICAL_SLUGS)[0]
BUYER_SLUG = sorted(CANONICAL_SLUGS)[1]
SELLER_ID = "11111111-1111-4111-8111-111111111111"
BUYER_ID = "22222222-2222-4222-8222-222222222222"
OUTSIDER_ID = "33333333-3333-4333-8333-333333333333"
OTHER_ID = "44444444-4444-4444-8444-444444444444"


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
    monkeypatch.setenv("GENESIS_AP2_BUYER_MAX_CENTS", "50")


def run(coro):
    return asyncio.run(coro)


def key() -> ap.RuntimeKey:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return ap.RuntimeKey(Ed25519PrivateKey.generate(), True)


def cred(slug=SELLER_SLUG, agent_id=SELLER_ID, roles=frozenset({ap.SELLER})) -> ap.AgentCredential:
    return ap.AgentCredential("cert-staging", slug, agent_id, "secret-key", roles)


class FakeMarketplace:
    """The SwarmSync side: negotiations the agent can see, and a record of what it sent."""

    def __init__(self) -> None:
        self.negotiations: list[dict[str, Any]] = []
        self.delivered: list[dict[str, Any]] = []
        self.accepted: list[tuple] = []
        self.negotiated: list[dict[str, Any]] = []
        self.redeemed: list[dict[str, Any]] = []
        self.deliver_status = 200
        self.agents: list[dict[str, Any]] = []

    def client(self, url: str, agent_id: str, api_key: str):
        market = self

        class Client(ap.SwarmSyncAgentClient):
            async def my_negotiations(self):
                return 200, market.negotiations

            async def deliver(self, body):
                market.delivered.append(body)
                return market.deliver_status, {"status": "VERIFICATION_PENDING"}

            async def accept(self, negotiation_id, responder_agent_id, price):
                market.accepted.append((negotiation_id, responder_agent_id, price))
                return 200, {}

            async def negotiate(self, body):
                market.negotiated.append(body)
                return 201, {"id": "neg-new", "status": "PENDING"}

            async def search_agents(self, query, limit=25):
                return 200, market.agents

            async def redeem_voucher(self, body):
                market.redeemed.append(body)
                return 200, {"status": "REDEEMED"}

            async def wallet(self):
                return 200, {"id": "w-1", "currency": "USD", "balance": "4.95", "reserved": "0", "extra": "dropped"}

        assert url == CERT_URL and api_key == "secret-key"
        return Client(url, agent_id, api_key)


def neg(**over) -> dict[str, Any]:
    base = {
        "id": "neg-1",
        "status": "ACCEPTED",
        "requesterAgent": {"id": OUTSIDER_ID},
        "responderAgent": {"id": SELLER_ID},
        "requestedService": "bounded certification task",
        "proposedBudget": 0.05,
        "requirements": {"text": "Summarise: SwarmSync is an open agent marketplace."},
        "notes": None,
        "counter": {"status": "ACCEPTED", "price": 0.05},
        "escrowId": "escrow-1",
        "verificationStatus": None,
    }
    base.update(over)
    return base


async def fake_executor(slug: str, prompt: str) -> str:
    return f"{slug} did: {prompt[:40]}"


def provider(market: FakeMarketplace, credentials=None) -> ap.Ap2Provider:
    return ap.Ap2Provider(credentials or [cred()], fake_executor, key(), "runtime-sha", market.client)


class TestCredentials:
    def test_valid_entries_load_with_default_seller_role(self):
        raw = json.dumps({"cert-staging": {SELLER_SLUG: {"agent_id": SELLER_ID, "api_key": "k"},
                                            BUYER_SLUG: {"agent_id": BUYER_ID, "api_key": "k2", "roles": ["seller", "buyer"]}}})
        creds = ap.load_credentials(lambda s: s in CANONICAL_SLUGS, raw)
        by_slug = {c.slug: c for c in creds}
        assert by_slug[SELLER_SLUG].roles == {ap.SELLER}
        assert by_slug[BUYER_SLUG].roles == {ap.SELLER, ap.BUYER}
        assert "k2" not in repr(by_slug[BUYER_SLUG])

    @pytest.mark.parametrize("bad", [
        {"unknown-env": {SELLER_SLUG: {"agent_id": SELLER_ID, "api_key": "k"}}},
        {"cert-staging": {"not-a-genesis-agent": {"agent_id": SELLER_ID, "api_key": "k"}}},
        {"cert-staging": {SELLER_SLUG: {"agent_id": "not-a-uuid", "api_key": "k"}}},
        {"cert-staging": {SELLER_SLUG: {"agent_id": SELLER_ID, "api_key": ""}}},
        {"cert-staging": {SELLER_SLUG: {"agent_id": SELLER_ID, "api_key": "k", "roles": ["admin"]}}},
    ])
    def test_bad_entries_are_skipped(self, bad):
        assert ap.load_credentials(lambda s: s in CANONICAL_SLUGS, json.dumps(bad)) == []

    def test_garbage_or_missing_config_is_empty(self):
        assert ap.load_credentials(lambda s: True, "") == []
        assert ap.load_credentials(lambda s: True, "{not json") == []
        assert ap.load_credentials(lambda s: True, "[]") == []


class TestDelivery:
    def test_delivery_body_hashes_signs_and_carries_inline_content(self):
        runtime_key = key()
        n = neg()
        body = ap.build_delivery(cred(), n, "the brief", runtime_key, "rt-sha")
        assert body["negotiationId"] == "neg-1" and body["responderAgentId"] == SELLER_ID
        digest = hashlib.sha256(ap.canonical_json(body["result"]).encode()).digest()
        assert body["evidence"]["result_sha256"] == digest.hex()
        Ed25519PublicKey.from_public_bytes(runtime_key.public_raw).verify(
            base64.b64decode(body["evidence"]["signature_ed25519_b64"]), digest
        )
        assert body["evidence"]["public_key_fingerprint"] == runtime_key.fingerprint
        assert json.loads(body["verificationHints"]["inlineContent"]) == body["result"]
        assert body["verificationHints"]["requestId"] == "genesis-neg-1"
        assert body["result"]["output"] == "the brief"

    def test_same_result_always_hashes_the_same(self):
        a = ap.canonical_json({"b": 1, "a": [2, {"d": 1, "c": 2}]})
        b = ap.canonical_json({"a": [2, {"c": 2, "d": 1}], "b": 1})
        assert a == b

    def test_job_input_uses_requirements_then_service(self):
        text = ap.job_input(neg())
        assert "bounded certification task" in text and "open agent marketplace" in text
        assert len(ap.job_input(neg(requirements={"text": "x" * 20000}))) <= ap.MAX_INPUT_CHARS

    def test_job_input_carries_every_requirement_field(self):
        # Regression (2026-10-21): job_input used to forward only the preferred
        # prose keys, so a task referencing a sibling field (workflow_definition)
        # reached the agent without that field and the agent failed closed;
        # Conduit then honestly FAILED the verification and escrow never settled.
        text = ap.job_input(neg(requirements={
            "task": "Read the workflow definition in the 'workflow_definition' field of this request and audit it.",
            "deliverable": "The audit summary as structured text.",
            "workflow_definition": "Workflow 'nightly-invoice-sync' v1.0: fetch, transform, post, notify.",
        }))
        assert "workflow_definition" in text and "nightly-invoice-sync" in text
        assert "deliverable" in text and "audit summary as structured text" in text
        assert text.count("Read the workflow definition") == 1

    def test_job_input_includes_structured_fields_as_json(self):
        text = ap.job_input(neg(requirements={
            "task": "Summarise the context.",
            "context": {"region": "eu", "retries": 3},
        }))
        assert '"region": "eu"' in text and '"retries": 3' in text

    @pytest.mark.parametrize("over,expected", [
        ({}, True),
        ({"status": "PENDING"}, False),
        ({"escrowId": None}, False),
        ({"workResult": {"x": 1}}, False),
        ({"verificationStatus": "PENDING"}, False),
        ({"responderAgent": {"id": OTHER_ID}}, False),
    ])
    def test_only_accepted_held_undelivered_jobs_for_this_agent(self, over, expected):
        assert ap.should_fulfil(neg(**over), SELLER_ID) is expected


class TestSellerLoop:
    def test_accepted_job_is_executed_and_delivered_once_with_the_agents_own_key(self):
        market = FakeMarketplace()
        market.negotiations = [neg()]
        p = provider(market)
        run(p.tick())
        run(p.tick())
        assert len(market.delivered) == 1
        assert market.delivered[0]["responderAgentId"] == SELLER_ID
        assert SELLER_SLUG in market.delivered[0]["result"]["output"]
        assert p.ledger["neg-1"]["deliver_status"] == 200

    def test_nothing_is_delivered_before_escrow_exists_or_after_delivery(self):
        market = FakeMarketplace()
        market.negotiations = [neg(escrowId=None), neg(id="neg-2", verificationStatus="VERIFIED"), neg(id="neg-3", status="PENDING")]
        run(provider(market).tick())
        assert market.delivered == []

    def test_a_server_error_is_retried_but_a_client_error_is_final(self):
        market = FakeMarketplace()
        market.negotiations = [neg()]
        p = provider(market)
        market.deliver_status = 503
        run(p.tick()); run(p.tick())
        assert len(market.delivered) == 2
        market.deliver_status = 409
        run(p.tick()); run(p.tick())
        assert len(market.delivered) == 3

    def test_buyer_only_credential_never_delivers(self):
        market = FakeMarketplace()
        market.negotiations = [neg()]
        run(provider(market, [cred(roles=frozenset({ap.BUYER}))]).tick())
        assert market.delivered == []


class TestBuyer:
    def procurement(self, **over):
        fields = dict(
            id="neg-b", status="COUNTERED", requesterAgent={"id": BUYER_ID}, responderAgent={"id": OUTSIDER_ID},
            proposedBudget=0.05, notes=f"{ap.PROCURE_MARK} abc", counter={"status": "COUNTERED", "price": 0.05},
            escrowId=None,
        )
        fields.update(over)
        return neg(**fields)

    def test_counter_within_the_offer_is_accepted_once_at_the_counter_price(self):
        market = FakeMarketplace()
        market.negotiations = [self.procurement()]
        p = provider(market, [cred(BUYER_SLUG, BUYER_ID, frozenset({ap.BUYER}))])
        run(p.tick()); run(p.tick())
        assert market.accepted == [("neg-b", OUTSIDER_ID, 0.05)]

    @pytest.mark.parametrize("over", [
        {"counter": {"status": "COUNTERED", "price": 0.06}},          # above what we offered
        {"counter": {"status": "COUNTERED", "price": None}},
        {"notes": "someone else's negotiation"},                      # not initiated by this runtime
        {"requesterAgent": {"id": OTHER_ID}},                         # we are not the buyer
        {"status": "PENDING"},
    ])
    def test_counters_outside_policy_are_left_alone(self, over):
        market = FakeMarketplace()
        market.negotiations = [self.procurement(**over)]
        run(provider(market, [cred(BUYER_SLUG, BUYER_ID, frozenset({ap.BUYER}))]).tick())
        assert market.accepted == []

    def test_buyer_cap_stops_a_counter_the_offer_would_allow(self, monkeypatch):
        monkeypatch.setenv("GENESIS_AP2_BUYER_MAX_CENTS", "4")
        market = FakeMarketplace()
        market.negotiations = [self.procurement()]
        run(provider(market, [cred(BUYER_SLUG, BUYER_ID, frozenset({ap.BUYER}))]).tick())
        assert market.accepted == []

    def test_procure_discovers_a_seller_dynamically_and_negotiates_with_its_own_identity(self):
        market = FakeMarketplace()
        market.agents = [
            {"id": BUYER_ID, "slug": BUYER_SLUG, "name": "self", "description": "brief summaries", "basePriceCents": 10},
            {"id": OTHER_ID, "slug": "image-gen", "name": "Image Gen", "description": "pictures", "basePriceCents": 5},
            {"id": OUTSIDER_ID, "slug": "acme_brief_desk", "name": "Acme Brief Desk",
             "description": "Turns a short text into a brief summary", "basePriceCents": 10, "tags": ["brief", "summary"]},
        ]
        p = provider(market, [cred(BUYER_SLUG, BUYER_ID, frozenset({ap.BUYER}))])
        out = run(p.procure(BUYER_SLUG, need="bounded brief", budget=0.05, query="brief summary", requirements={"text": "hi"}))
        assert out["seller"]["slug"] == "acme_brief_desk"
        sent = market.negotiated[0]
        assert sent["requesterAgentId"] == BUYER_ID and sent["responderAgentId"] == OUTSIDER_ID
        assert sent["budget"] == 0.05 and ap.PROCURE_MARK in sent["notes"] and sent["requirements"] == {"text": "hi"}
        assert sent["autoSettle"] is True  # the buyer's own consent to Conduit-driven release

    def test_auto_settle_consent_can_be_turned_off(self, monkeypatch):
        monkeypatch.setenv("GENESIS_AP2_BUYER_AUTO_SETTLE", "false")
        market = FakeMarketplace()
        market.agents = [{"id": OUTSIDER_ID, "slug": "acme_brief_desk", "name": "Brief Desk", "description": "brief summary", "basePriceCents": 10}]
        p = provider(market, [cred(BUYER_SLUG, BUYER_ID, frozenset({ap.BUYER}))])
        run(p.procure(BUYER_SLUG, need="x", budget=0.05, query="brief"))
        assert market.negotiated[0]["autoSettle"] is False

    def test_procure_refuses_over_cap_unfunded_shape_and_missing_sellers(self):
        market = FakeMarketplace()
        p = provider(market, [cred(BUYER_SLUG, BUYER_ID, frozenset({ap.BUYER}))])
        with pytest.raises(ValueError):
            run(p.procure(BUYER_SLUG, need="x", budget=0.51, query="brief"))
        with pytest.raises(ValueError):
            run(p.procure(BUYER_SLUG, need="x", budget=0.055, query="brief"))
        with pytest.raises(LookupError):
            run(p.procure(BUYER_SLUG, need="x", budget=0.05, query="brief"))
        with pytest.raises(LookupError):
            run(p.procure(SELLER_SLUG, need="x", budget=0.05, query="brief"))   # seller-only credential cannot buy
        assert market.negotiated == []

    def test_voucher_redeem_is_signed_by_the_runtime_for_this_agent(self):
        market = FakeMarketplace()
        p = provider(market, [cred(BUYER_SLUG, BUYER_ID, frozenset({ap.BUYER}))])
        run(p.redeem_voucher(BUYER_SLUG, voucher_token="swv_x", nonce="n1", run_id="run-1"))
        sent = market.redeemed[0]
        message = f"swarmsync.wallet-voucher.redeem.v1|n1|run-1|{BUYER_ID}".encode()
        Ed25519PublicKey.from_public_bytes(base64.b64decode(sent["runtime_public_key"])).verify(
            base64.b64decode(sent["runtime_signature"]), message
        )
        assert sent["run_id"] == "run-1"


def _negotiate_request(main):
    return main.NegotiateRequest(
        event="negotiate", negotiation_id="n", requester_agent_id=OUTSIDER_ID, requester_agent_name="o",
        responder_agent_id=SELLER_ID, requested_service="s", budget=0.05,
        callback_url=CERT_URL + "/ap2/gateway/respond",
    )


class TestSellerNegotiationReply:
    @pytest.mark.parametrize("decided_price,expected_status", [(0.08, "COUNTERED"), (0.05, "ACCEPTED")])
    def test_an_accept_is_only_an_accept_at_the_buyers_offer(self, monkeypatch, decided_price, expected_status):
        import main

        sent: list[dict] = []

        async def fake_eval(slug, request):
            return {"status": "ACCEPTED", "price": decided_price, "notes": "ok"}

        async def fake_send(url, payload, env=None):
            sent.append(payload)
            return True

        monkeypatch.setattr(main, "evaluate_negotiation", fake_eval)
        monkeypatch.setattr(main, "send_negotiate_callback", fake_send)
        run(main._evaluate_and_respond(SELLER_SLUG, _negotiate_request(main), None))
        assert sent[0]["status"] == expected_status and sent[0]["price"] == decided_price


class TestManagedCredentials:
    def managed(self):
        return [("cert-staging", BUYER_SLUG, BUYER_ID, frozenset({ap.BUYER}))]

    def test_parse_managed_agents(self):
        raw = json.dumps({"cert-staging": {BUYER_SLUG: {"agent_id": BUYER_ID, "roles": ["buyer"]}}})
        assert ap.load_managed(lambda s: s in CANONICAL_SLUGS, raw) == self.managed()
        for bad in ({"nope": {BUYER_SLUG: {"agent_id": BUYER_ID}}}, {"cert-staging": {"not-genesis": {"agent_id": BUYER_ID}}},
                    {"cert-staging": {BUYER_SLUG: {"agent_id": "x"}}}, []):
            assert ap.load_managed(lambda s: s in CANONICAL_SLUGS, json.dumps(bad)) == []

    def test_the_gateway_obtains_the_agents_own_key_then_rotates_it(self):
        calls: list[tuple] = []
        keys = iter(["key-one", "key-two"])

        async def fetcher(api_url, secret, agent_id):
            calls.append((api_url, secret, agent_id))
            return 201, {"apiKey": next(keys)}

        market = FakeMarketplace()
        p = ap.Ap2Provider([], fake_executor, key(), "sha", lambda url, aid, k: market.client(url, aid, "secret-key"),
                           managed=self.managed(), credential_fetcher=fetcher)
        run(p.tick())
        assert [c.agent_id for c in p.credentials] == [BUYER_ID] and p.credentials[0].api_key == "key-one"
        assert calls[0][0] == CERT_URL and calls[0][1] == "cert-gateway-secret-0123456789" and calls[0][2] == BUYER_ID
        run(p.tick())
        assert len(calls) == 1  # fresh enough: no second issue
        run(p.refresh_managed(max_age=0))
        assert p.credentials[0].api_key == "key-two" and len(p.credentials) == 1

    def test_a_refusal_or_outage_keeps_the_loop_alive_and_adds_no_credential(self):
        async def refused(api_url, secret, agent_id):
            return 403, {"message": "The gateway does not host this agent"}

        async def down(api_url, secret, agent_id):
            raise RuntimeError("network")

        for fetcher in (refused, down):
            p = ap.Ap2Provider([], fake_executor, key(), "sha", FakeMarketplace().client, managed=self.managed(), credential_fetcher=fetcher)
            run(p.refresh_managed())
            assert p.credentials == []


class TestWallet:
    def test_balance_is_read_with_the_agents_own_credential_and_only_the_balance_fields_come_back(self):
        market = FakeMarketplace()
        p = provider(market, [cred(BUYER_SLUG, BUYER_ID, frozenset({ap.BUYER}))])
        out = run(p.wallet(BUYER_SLUG))
        assert out == {"http_status": 200, "agent_id": BUYER_ID, "wallet_id": "w-1", "currency": "USD", "balance": "4.95", "reserved": "0"}
        with pytest.raises(LookupError):
            run(p.wallet(SELLER_SLUG))  # seller-only credential has no buyer wallet view


class TestIdleBackoff:
    """Oct 29: polling every hosted agent's inbox every tick drove the marketplace database over its egress quota.
    An agent with nothing live is polled again only after the idle interval; a live deal keeps it on every tick."""

    class CountingMarket(FakeMarketplace):
        def __init__(self) -> None:
            super().__init__()
            self.polls = 0

        def client(self, url, agent_id, api_key):
            inner = super().client(url, agent_id, api_key)
            market = self
            original = inner.my_negotiations

            async def counted():
                market.polls += 1
                return await original()

            inner.my_negotiations = counted
            return inner

    def test_an_idle_agent_is_not_polled_again_until_the_idle_interval_passes(self, monkeypatch):
        monkeypatch.setenv("GENESIS_AP2_PROVIDER_IDLE_POLL_SECONDS", "300")
        market = self.CountingMarket()
        p = provider(market)
        run(p.tick())
        run(p.tick())
        run(p.tick())
        assert market.polls == 1
        p._next_poll_at = {k: 0.0 for k in p._next_poll_at}  # the interval has passed
        run(p.tick())
        assert market.polls == 2

    @pytest.mark.parametrize("live", [
        {"status": "PENDING", "escrowId": None},
        {"status": "COUNTERED", "escrowId": None},
        {"status": "ACCEPTED", "escrowId": None},
    ])
    def test_a_live_deal_keeps_the_agent_on_every_tick(self, monkeypatch, live):
        monkeypatch.setenv("GENESIS_AP2_PROVIDER_IDLE_POLL_SECONDS", "300")
        market = self.CountingMarket()
        market.negotiations = [neg(**live)]
        p = provider(market)
        run(p.tick())
        run(p.tick())
        assert market.polls == 2

    def test_finished_deals_do_not_keep_an_agent_hot(self):
        assert not ap.has_live_deal([neg(workResult={"output": "x"}), neg(verificationStatus="VERIFIED"),
                                     neg(status="DECLINED"), neg(status="EXPIRED"), "junk"])

    def test_an_offer_or_a_purchase_wakes_an_idle_agent(self, monkeypatch):
        monkeypatch.setenv("GENESIS_AP2_PROVIDER_IDLE_POLL_SECONDS", "300")
        market = self.CountingMarket()
        p = provider(market)
        run(p.tick())
        p.wake(SELLER_SLUG)
        run(p.tick())
        assert market.polls == 2

    def test_an_accepted_job_is_still_delivered_once(self, monkeypatch):
        monkeypatch.setenv("GENESIS_AP2_PROVIDER_IDLE_POLL_SECONDS", "300")
        market = self.CountingMarket()
        market.negotiations = [neg()]
        p = provider(market)
        run(p.tick())
        run(p.tick())
        assert len(market.delivered) == 1
