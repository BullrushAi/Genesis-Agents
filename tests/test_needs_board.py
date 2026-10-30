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
        self.deliver_status = 201
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
                    return board.deliver_status, {"status": "VERIFICATION_PENDING"}
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


def _clear_store_env(monkeypatch) -> None:
    for name in ("GENESIS_NEEDS_STATE_PATH", "GENESIS_JOB_DATABASE_URL", "DIRECT_URL", "DATABASE_URL", "GENESIS_STORE_BACKEND"):
        monkeypatch.delenv(name, raising=False)


class TestEscrowBidDurability:
    def test_startup_does_not_default_the_bid_file_to_var_data(self, monkeypatch):
        _clear_store_env(monkeypatch)
        source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
        assert "/var/data/genesis-needs-board.json" not in source
        assert isinstance(nb.bid_store_from_env(), nb.UnavailableBidStore)

    def test_the_job_database_is_the_bid_store_when_postgres_is_selected(self, monkeypatch):
        _clear_store_env(monkeypatch)
        monkeypatch.setenv("GENESIS_JOB_DATABASE_URL", "postgresql://127.0.0.1:1/genesis")
        monkeypatch.setenv("GENESIS_NEEDS_STATE_PATH", "/var/data/genesis-needs-board.json")
        store = nb.bid_store_from_env()
        assert isinstance(store, nb.PostgresBidStore)

    def test_postgres_bid_rows_use_bound_parameters(self, monkeypatch):
        class Cursor:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple]] = []

            def execute(self, sql, params=None):
                self.calls.append((sql, tuple(params or ())))

            def fetchall(self):
                return [{"env": "cert-staging", "slug": KE, "need_id": "need-1", "bid_at": 1.5}]

        cursor = Cursor()

        class Tx:
            def __enter__(self):
                return cursor

            def __exit__(self, *args):
                return False

        import runtime.pg_store as pg_store

        monkeypatch.setattr(pg_store, "transaction", lambda: Tx())
        store = nb.PostgresBidStore()
        bids, at = store.load()
        assert bids[("cert-staging", KE)] == {"need-1"}
        assert at[("cert-staging", KE, "need-1")] == 1.5
        store.put("cert-staging", KE, "need-2", 2.0)
        insert_sql, insert_params = cursor.calls[-1]
        assert "INSERT INTO genesis_needs_bids" in insert_sql
        assert "%s" in insert_sql
        assert insert_params == ("cert-staging", KE, "need-2", 2.0)
        store.drop("cert-staging", KE, "need-2")
        delete_sql, delete_params = cursor.calls[-1]
        assert "DELETE FROM genesis_needs_bids" in delete_sql
        assert delete_params == ("cert-staging", KE, "need-2")

    def test_a_failed_bid_write_is_not_sent(self, tmp_path):
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("x")
        state = blocked / "needs.json"
        fake = FakeBoard()
        fake.for_agent = [need()]
        board = nb.NeedsBoard(
            ap.Ap2Provider([seller()], None, key(), "runtime-sha", fake.client), None, state_path=state
        )
        report = run(board.find_buyers(KE))
        assert fake.responses == []
        assert report["bids"][0]["error"] == "bid_state_not_durable"
        assert "http_status" not in report["bids"][0]
        assert board.open_bids.get(("cert-staging", KE), set()) == set()

    def test_no_durable_store_refuses_the_bid(self, monkeypatch):
        _clear_store_env(monkeypatch)
        fake = FakeBoard()
        fake.for_agent = [need()]
        board = nb.NeedsBoard(
            ap.Ap2Provider([seller()], None, key(), "runtime-sha", fake.client),
            None,
            bid_store=nb.bid_store_from_env(),
        )
        report = run(board.find_buyers(KE))
        assert fake.responses == []
        assert report["bids"][0]["error"] == "bid_state_not_durable"
        assert board.open_bids.get(("cert-staging", KE), set()) == set()

    def test_an_unreadable_store_refuses_new_bids(self):
        class Unreadable(nb.BidStore):
            def load(self):
                raise RuntimeError("store down")

            def put(self, env, slug, need_id, bid_at):
                raise AssertionError("must not bid when the store could not be read")

            def drop(self, env, slug, need_id):
                return None

        fake = FakeBoard()
        fake.for_agent = [need()]
        board = nb.NeedsBoard(
            ap.Ap2Provider([seller()], None, key(), "runtime-sha", fake.client), None, bid_store=Unreadable()
        )
        report = run(board.find_buyers(KE))
        assert fake.responses == []
        assert report["bids"][0]["error"] == "bid_state_not_durable"
        assert board.open_bids.get(("cert-staging", KE), set()) == set()


class TestDeliveryKeepsTheBidUnlessItLanded:
    @pytest.mark.parametrize("status", [200, 201, 299, 409])
    def test_forgets_the_bid_only_when_delivery_lands_or_already_landed(self, status):
        fake = FakeBoard()
        fake.deliver_status = status
        fake.for_agent = [need()]
        board = board_with(fake)
        run(board.find_buyers(KE))
        fake.work_orders["need-1"] = work_order()
        run(board.tick())
        assert [d[0] for d in fake.delivered] == ["need-1"]
        assert board.open_bids[("cert-staging", KE)] == set()
        assert "need:cert-staging:need-1" in board._done

    @pytest.mark.parametrize("status", [300, 400, 401, 403, 404, 408, 422, 429, 500, 503])
    def test_a_non_delivery_status_leaves_the_bid_for_a_later_tick(self, status):
        fake = FakeBoard()
        fake.deliver_status = status
        fake.for_agent = [need()]
        board = board_with(fake)
        run(board.find_buyers(KE))
        fake.work_orders["need-1"] = work_order()
        run(board.tick())
        assert [d[0] for d in fake.delivered] == ["need-1"]
        assert board.open_bids[("cert-staging", KE)] == {"need-1"}
        assert "need:cert-staging:need-1" not in board._done
        fake.deliver_status = 201
        board._next_work_poll.clear()
        run(board.tick())
        assert [d[0] for d in fake.delivered] == ["need-1", "need-1"]
        assert board.open_bids[("cert-staging", KE)] == set()

    def test_run_job_rejection_is_not_posted_and_the_bid_stays(self, monkeypatch):
        monkeypatch.setenv("GENESIS_MARKET_SELF_REVIEW", "false")

        async def no_sources(spec, job):
            return job

        monkeypatch.setattr(ma, "_maybe_gather_sources", no_sources)
        fake = FakeBoard()
        fake.for_agent = [need()]
        replies = ['{"status": "PARTIAL"}', '{"status": "PARTIAL"}']

        async def executor(slug, prompt):
            class Scripted:
                async def __call__(self, system, user):
                    return replies.pop(0)

            return await ma.run_job(slug, prompt, Scripted())

        board = board_with(fake, executor)
        run(board.find_buyers(KE))
        fake.work_orders["need-1"] = work_order()
        run(board.tick())
        assert fake.delivered == []
        assert board.open_bids[("cert-staging", KE)] == {"need-1"}
        assert "need:cert-staging:need-1" not in board._done
