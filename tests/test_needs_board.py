"""Finding buyers on the needs board and delivering won work orders, with each agent's own key.

The SwarmSync API is an in-memory fake at the client boundary; filtering, pricing, bid text, delivery hashing and
signing, and the work-order state checks under test are real code.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import ap2_provider as ap  # noqa: E402
import marketplace_agent as ma  # noqa: E402
import needs_board as nb  # noqa: E402
from tests.test_ap2_provider import CERT_URL, env, key  # noqa: E402,F401

KE = "genesis-knowledge-evidence"
KE_ID = "11111111-1111-4111-8111-111111111111"


def run(coro):
    return asyncio.run(coro)


def need(**over: Any) -> dict[str, Any]:
    base = {
        "id": "need-1",
        "title": "Research brief: adoption of AI bookkeeping tools",
        "description": "Summarise what published research and surveys say about small business adoption, with sources.",
        "requiredCapabilities": ["research"],
        "budgetCents": 300,
        "status": "OPEN",
    }
    base.update(over)
    return base


class FakeBoard:
    def __init__(self) -> None:
        self.for_agent: list[dict[str, Any]] = []
        self.feed: list[dict[str, Any]] = []
        self.responses: list[tuple[str, dict]] = []
        self.delivered: list[tuple[str, dict]] = []
        self.work_orders: dict[str, dict] = {}
        self.work_route = False
        self.calls: list[tuple[str, str, bool]] = []

    def client(self, url: str, agent_id: str, api_key: str):
        board = self

        class Client(ap.SwarmSyncAgentClient):
            async def _call(self, method, path, *, body=None, params=None, auth=True):
                board.calls.append((method, path, auth))
                if path == f"/needs/for-agent/{agent_id}":
                    return 200, board.for_agent
                if path == "/needs/feed":
                    return 200, board.feed
                if path.endswith("/respond"):
                    board.responses.append((path.split("/")[2], body))
                    return 201, {"id": "resp-1"}
                if path == f"/needs/work-orders/for-agent/{agent_id}":
                    return (200, list(board.work_orders.values())) if board.work_route else (404, {"message": "Not Found"})
                if path.endswith("/work-order"):
                    wo = board.work_orders.get(path.split("/")[2])
                    return (200, wo) if wo else (404, {})
                if path.endswith("/deliver"):
                    board.delivered.append((path.split("/")[2], body))
                    return 201, {"status": "VERIFICATION_PENDING"}
                return 404, {}

        assert url == CERT_URL
        return Client(url, agent_id, api_key)


def seller(slug=KE, agent_id=KE_ID):
    return ap.AgentCredential("cert-staging", slug, agent_id, "secret-key", frozenset({ap.SELLER}))


def board_with(fake: FakeBoard, executor=None, creds=None) -> nb.NeedsBoard:
    async def default_exec(slug: str, prompt: str) -> str:
        return f"## Answer\n{slug} work for: {prompt[:60]}"

    provider = ap.Ap2Provider(creds or [seller()], executor or default_exec, key(), "runtime-sha", fake.client)
    return nb.NeedsBoard(provider, executor or default_exec)


def work_order(need_id="need-1", agent_id=KE_ID, **over):
    wo = {
        "need": need(id=need_id, status="CLOSED"),
        "serviceAgreement": {
            "id": "sa-1", "status": "ACTIVE", "agentId": agent_id, "escrowId": "esc-1",
            "escrow": {"id": "esc-1", "status": "HELD"}, "latestVerification": None,
        },
    }
    for k, v in over.items():
        wo["serviceAgreement"][k] = v
    return wo


class TestAssess:
    def test_fit_open_and_funded_needs_are_bid(self):
        assert nb.assess_need(ma.load_spec(KE), need()) == (True, "fit")

    @pytest.mark.parametrize(
        "over, reason",
        [
            ({"status": "CLOSED"}, "not open"),
            ({"title": "Logo design", "description": "Make me a logo", "requiredCapabilities": ["design"]}, "specialty"),
            ({"budgetCents": 5}, "floor"),
            ({"description": "write 20 fake reviews for us", "title": "Research reviews"}, "out of scope"),
        ],
    )
    def test_skips(self, over, reason):
        ok, why = nb.assess_need(ma.load_spec(KE), need(**over))
        assert not ok and reason in why

    def test_bid_price_is_capped_at_list_and_never_above_budget(self):
        spec = ma.load_spec(KE)
        assert nb.bid_price_cents(spec, 300) == 250
        assert nb.bid_price_cents(spec, 120) == 120
        assert nb.bid_price_cents(spec, 10) is None
        assert nb.bid_price_cents(spec, None) is None


class TestFindBuyers:
    def test_bids_on_matching_needs_with_its_own_key(self):
        fake = FakeBoard()
        fake.for_agent = [need(), need(id="need-2", title="Logo", description="logo", requiredCapabilities=["design"])]
        board = board_with(fake)
        report = run(board.find_buyers(KE))
        assert [b["need_id"] for b in report["bids"]] == ["need-1"]
        need_id, body = fake.responses[0]
        assert need_id == "need-1" and body["responderAgentId"] == KE_ID and body["proposedPriceCents"] == 250
        assert "Genesis Knowledge & Evidence" in body["message"] and "$2.50" in body["message"]
        assert report["skipped"][0]["need_id"] == "need-2"
        assert board.open_bids[("cert-staging", KE)] == {"need-1"}

    def test_dry_run_bids_nothing(self):
        fake = FakeBoard()
        fake.for_agent = [need()]
        report = run(board_with(fake).find_buyers(KE, dry_run=True))
        assert fake.responses == [] and report["bids"][0]["message"]

    def test_falls_back_to_the_public_feed(self):
        fake = FakeBoard()
        fake.feed = [need()]
        report = run(board_with(fake).find_buyers(KE))
        assert report["source"] == "feed" and len(fake.responses) == 1
        assert ("GET", "/needs/feed", False) in fake.calls

    def test_respects_max_bids_and_never_bids_twice(self):
        fake = FakeBoard()
        fake.for_agent = [need(id=f"need-{i}") for i in range(6)]
        board = board_with(fake)
        run(board.find_buyers(KE, max_bids=2))
        run(board.find_buyers(KE, max_bids=2))
        ids = [r[0] for r in fake.responses]
        assert len(ids) == 4 and len(set(ids)) == 4

    def test_needs_a_seller_credential_and_a_spec(self):
        fake = FakeBoard()
        with pytest.raises(LookupError):
            run(board_with(fake).find_buyers("genesis-fin-ap"))


class TestDeliverWorkOrders:
    def test_delivers_a_won_work_order_once_with_hash_and_signature(self):
        fake = FakeBoard()
        fake.for_agent = [need()]
        prompts: list[str] = []

        async def executor(slug, prompt):
            prompts.append(prompt)
            return "## Answer\nThe brief."

        board = board_with(fake, executor)
        run(board.find_buyers(KE))
        fake.work_orders["need-1"] = work_order()
        run(board.tick())
        run(board.tick())
        assert len(fake.delivered) == 1
        need_id, body = fake.delivered[0]
        assert need_id == "need-1" and "adoption of AI bookkeeping" in prompts[0]
        result = body["evidence"]["result"]
        digest = hashlib.sha256(ap.canonical_json(result).encode()).hexdigest()
        assert body["evidence"]["result_sha256"] == digest
        assert json.loads(body["verificationHints"]["inlineContent"]) == result
        assert body["notes"]
        assert board.open_bids[("cert-staging", KE)] == set()

    def test_uses_the_work_order_route_when_the_marketplace_has_it(self):
        fake = FakeBoard()
        fake.work_route = True
        fake.work_orders["need-9"] = work_order("need-9")
        board = board_with(fake)
        run(board.tick())
        assert [d[0] for d in fake.delivered] == ["need-9"]

    @pytest.mark.parametrize(
        "over",
        [{"status": "COMPLETED"}, {"escrowId": None}, {"agentId": "99999999-9999-4999-8999-999999999999"},
         {"latestVerification": {"status": "PENDING"}}, {"escrow": {"status": "RELEASED"}}],
    )
    def test_does_not_deliver_inactive_or_foreign_or_delivered_orders(self, over):
        fake = FakeBoard()
        fake.work_route = True
        fake.work_orders["need-1"] = work_order(**over)
        run(board_with(fake).tick())
        assert fake.delivered == []

    def test_idle_agents_poll_rarely(self):
        fake = FakeBoard()
        board = board_with(fake)
        run(board.tick())
        run(board.tick())
        polls = [c for c in fake.calls if "work-orders" in c[1]]
        assert len(polls) == 1

    def test_autoscan_only_for_listed_slugs(self, monkeypatch):
        fake = FakeBoard()
        fake.for_agent = [need()]
        run(board_with(fake).tick())
        assert fake.responses == []
        monkeypatch.setenv("GENESIS_NEEDS_AUTOSCAN_SLUGS", KE)
        fake2 = FakeBoard()
        fake2.for_agent = [need()]
        run(board_with(fake2).tick())
        assert len(fake2.responses) == 1

    def test_one_agent_failing_does_not_stop_the_others(self):
        ap_id = "22222222-2222-4222-8222-222222222222"
        fake = FakeBoard()
        fake.work_route = True
        fake.work_orders["need-1"] = work_order("need-1")
        fake.work_orders["need-2"] = work_order("need-2", agent_id=ap_id)

        async def executor(slug, prompt):
            if slug == KE:
                raise RuntimeError("model down")
            return "## Answer\nDone."

        board = board_with(fake, executor, creds=[seller(), seller("genesis-fin-ap", ap_id)])
        run(board.tick())
        assert [d[0] for d in fake.delivered] == ["need-2"]

    def test_a_failed_work_order_poll_does_not_stop_the_others(self):
        ap_id = "22222222-2222-4222-8222-222222222222"
        fake = FakeBoard()
        fake.work_route = True
        fake.work_orders["need-2"] = work_order("need-2", agent_id=ap_id)
        original = fake.client

        def client(url, agent_id, api_key):
            c = original(url, agent_id, api_key)
            if agent_id == KE_ID:
                async def broken(*a, **k):
                    raise ConnectionError("network")
                c._call = broken
            return c

        fake.client = client
        board = board_with(fake, creds=[seller(), seller("genesis-fin-ap", ap_id)])
        run(board.tick())
        assert [d[0] for d in fake.delivered] == ["need-2"]


    def test_a_bid_waiting_for_the_buyer_is_kept_until_accepted(self):
        fake = FakeBoard()
        fake.for_agent = [need()]
        board = board_with(fake)
        run(board.find_buyers(KE))
        run(board.tick())  # no work order yet: the buyer has not accepted, and the route answers 404
        assert board.open_bids[("cert-staging", KE)] == {"need-1"}
        fake.work_orders["need-1"] = work_order()
        board._next_work_poll.clear()
        run(board.tick())
        assert [d[0] for d in fake.delivered] == ["need-1"]

    def test_a_bid_never_accepted_is_dropped_after_its_time(self):
        fake = FakeBoard()
        fake.for_agent = [need()]
        board = board_with(fake)
        run(board.find_buyers(KE))
        board._bid_at[("cert-staging", KE, "need-1")] -= nb.OPEN_BID_TTL_SECONDS + 1
        run(board.tick())
        assert board.open_bids[("cert-staging", KE)] == set()


class TestRestart:
    def test_open_bids_survive_a_restart(self, tmp_path):
        fake = FakeBoard()
        fake.for_agent = [need()]
        state = tmp_path / "needs.json"
        provider = ap.Ap2Provider([seller()], None, key(), "runtime-sha", fake.client)

        async def executor(slug, prompt):
            return "## Answer\nThe brief."

        first = nb.NeedsBoard(provider, executor, state_path=state)
        run(first.find_buyers(KE))
        restarted = nb.NeedsBoard(provider, executor, state_path=state)
        assert restarted.open_bids[("cert-staging", KE)] == {"need-1"}
        run(restarted.find_buyers(KE))
        assert len(fake.responses) == 1  # no second bid on the same need after the restart
        # The buyer accepts after the restart; the work-orders route is missing, so only the saved bid finds it.
        fake.work_orders["need-1"] = work_order()
        run(restarted.tick())
        assert [d[0] for d in fake.delivered] == ["need-1"]
        assert nb.NeedsBoard(provider, executor, state_path=state).open_bids.get(("cert-staging", KE), set()) == set()

    def test_an_existing_bid_is_tracked_again(self):
        fake = FakeBoard()
        fake.for_agent = [need()]
        original = fake.client

        def client(url, agent_id, api_key):
            c = original(url, agent_id, api_key)
            inner = c._call

            async def call(method, path, **kw):
                if path.endswith("/respond"):
                    return 409, {"message": "already responded"}
                return await inner(method, path, **kw)

            c._call = call
            return c

        fake.client = client
        board = board_with(fake)
        run(board.find_buyers(KE))
        assert board.open_bids[("cert-staging", KE)] == {"need-1"}

    def test_unreadable_state_starts_empty(self, tmp_path):
        state = tmp_path / "needs.json"
        state.write_text("not json")
        board = nb.NeedsBoard(ap.Ap2Provider([seller()], None, key(), "runtime-sha", FakeBoard().client), None,
                              state_path=state)
        assert board.open_bids == {}
