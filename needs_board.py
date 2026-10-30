"""Finding buyers on the SwarmSync needs board, and delivering the work orders won there.

A buyer posts a SwarmNeed (title, description, required capabilities, budget). An agent bids with
``POST /needs/{id}/respond``; the buyer accepts one bid, which holds escrow under a work order; the agent delivers
with ``POST /needs/{id}/deliver``. Work orders have no AP2 negotiation, so the provider's negotiation inbox never
sees them: this module watches them itself.

"When directed": bidding never starts on its own. It runs when an operator calls
``POST /agents/{slug}/ap2/find-buyers`` on the gateway, or on a schedule only for the slugs listed in
``GENESIS_NEEDS_AUTOSCAN_SLUGS``. Delivery of an accepted work order is automatic, because the buyer's money is
already held for it.

Each bid is filtered by the agent's marketplace spec (fit, out-of-scope rules, price floor) and priced between the
floor and the agent's list price, never above the buyer's posted budget. Every call uses the agent's own key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import ap2_provider
import marketplace_agent

logger = logging.getLogger("needs_board")

Executor = Callable[[str, str], Awaitable[str]]

DEFAULT_MAX_BIDS = 3
MAX_BIDS_CAP = 10
WORK_ORDER_POLL_SECONDS = 300.0
IDLE_WORK_ORDER_POLL_SECONDS = 900.0
SCAN_SECONDS = 3600.0
# A bid with no work order yet is kept this long, then dropped as never accepted.
OPEN_BID_TTL_SECONDS = 14 * 86400.0
BID_MESSAGE_CHARS = 2000


def autoscan_slugs() -> set[str]:
    raw = os.getenv("GENESIS_NEEDS_AUTOSCAN_SLUGS", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def _need_text(need: dict[str, Any]) -> str:
    caps = " ".join(str(c) for c in need.get("requiredCapabilities") or [])
    return " ".join(str(need.get(k) or "") for k in ("title", "description", "summary")) + " " + caps


def _needs_list(data: Any) -> list[dict[str, Any]]:
    items = data if isinstance(data, list) else (data or {}).get("needs") or (data or {}).get("items") or []
    return [n for n in items if isinstance(n, dict) and n.get("id")]


def bid_price_cents(spec: dict[str, Any], budget_cents: Any) -> Optional[int]:
    """What to bid: the buyer's budget capped at list price; None when the budget is under the floor."""
    if not isinstance(budget_cents, (int, float)) or isinstance(budget_cents, bool) or budget_cents <= 0:
        return None
    floor = marketplace_agent.floor_cents(spec)
    if budget_cents < floor:
        return None
    return int(min(budget_cents, marketplace_agent.list_cents(spec)))


def assess_need(spec: dict[str, Any], need: dict[str, Any]) -> tuple[bool, str]:
    """Whether this agent should bid, and why not when it should not."""
    if str(need.get("status") or "OPEN") not in ("OPEN", "MATCHED"):
        return False, "not open"
    text = _need_text(need)
    rule = marketplace_agent.out_of_scope_match(spec, text)
    if rule is not None:
        return False, f"out of scope: {rule.get('reason')}"
    fit = marketplace_agent.fit_score(spec, text) + 2 * marketplace_agent.capability_overlap(
        spec, need.get("requiredCapabilities") or []
    )
    if fit < 2:
        return False, "not this agent's specialty"
    if bid_price_cents(spec, need.get("budgetCents")) is None:
        return False, "budget under this agent's floor"
    return True, "fit"


def _best_service(spec: dict[str, Any], text: str) -> dict[str, Any]:
    services = spec.get("services") or [{}]
    lowered = text.lower()

    def score(s: dict[str, Any]) -> int:
        words = set(str(s.get("name", "") + " " + s.get("description", "")).lower().split())
        return sum(1 for w in words if len(w) > 3 and w in lowered)

    return max(services, key=score)


def bid_message(spec: dict[str, Any], need: dict[str, Any], price_cents: int) -> str:
    """A specific, honest pitch: who the agent is, what it will deliver for this need, and the price."""
    service = _best_service(spec, _need_text(need))
    delivery = spec.get("delivery") or {}
    sections = ", ".join(delivery.get("sections") or [])
    title = str(need.get("title") or "this need").strip()[:160]
    msg = (
        f"{spec['display_name']}: {spec['specialty']}\n\n"
        f"For \"{title}\" I will deliver: {service.get('deliverable') or service.get('description') or 'the finished work'}. "
        f"Format: markdown with {sections}. "
        f"If anything in the brief is missing I will state my assumptions and still deliver. "
        f"Price: ${price_cents / 100:.2f}, delivered within minutes of acceptance."
    )
    return msg[:BID_MESSAGE_CHARS]


def need_job_text(need: dict[str, Any]) -> str:
    parts = [f"Service requested: {need.get('title') or ''}".strip()]
    for key in ("description", "summary"):
        if need.get(key):
            parts.append(str(need[key]).strip())
    if need.get("constraints"):
        parts.append(f"constraints: {need['constraints']}")
    if need.get("requiredCapabilities"):
        parts.append("required capabilities: " + ", ".join(str(c) for c in need["requiredCapabilities"]))
    return "\n\n".join(p for p in parts if p)[: ap2_provider.MAX_INPUT_CHARS]


def build_need_delivery(
    cred: ap2_provider.AgentCredential, need: dict[str, Any], output: str, key: ap2_provider.RuntimeKey, runtime_sha256: str
) -> dict[str, Any]:
    """The ``POST /needs/{id}/deliver`` body: the result, its hash and signature, and the Conduit inline content."""
    result = {
        "service": f"Genesis {cred.slug}",
        "agent": cred.slug,
        "job": "need-work-order",
        "need_id": need["id"],
        "output": output[: ap2_provider.MAX_OUTPUT_CHARS],
    }
    digest = hashlib.sha256(ap2_provider.canonical_json(result).encode("utf-8")).digest()
    return {
        "notes": f"Delivered by the Genesis {cred.slug} runtime for need {need['id']}.",
        "evidence": {
            "result": result,
            "runtime_sha256": runtime_sha256,
            "result_sha256": digest.hex(),
            "signature_ed25519_b64": key.sign(digest),
            "public_key_fingerprint": key.fingerprint,
        },
        "verificationHints": {"inlineContent": ap2_provider.canonical_json(result)},
    }


class BidStore:
    """Restart-critical open bids. ``put`` and ``drop`` raise when the write does not stick."""

    def load(self) -> tuple[dict[tuple[str, str], set[str]], dict[tuple[str, str, str], float]]:
        raise NotImplementedError

    def put(self, env: str, slug: str, need_id: str, bid_at: float) -> None:
        raise NotImplementedError

    def drop(self, env: str, slug: str, need_id: str) -> None:
        raise NotImplementedError


class UnavailableBidStore(BidStore):
    """No Postgres and no explicit file path. Every put fails so a bid is refused."""

    def load(self) -> tuple[dict[tuple[str, str], set[str]], dict[tuple[str, str, str], float]]:
        return {}, {}

    def put(self, env: str, slug: str, need_id: str, bid_at: float) -> None:
        raise OSError("no durable needs-bid store")

    def drop(self, env: str, slug: str, need_id: str) -> None:
        return None


class FileBidStore(BidStore):
    """Explicit ``GENESIS_NEEDS_STATE_PATH`` only. Never the default, and never ``/var/data``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._bids, self._at = self._read()

    def load(self) -> tuple[dict[tuple[str, str], set[str]], dict[tuple[str, str, str], float]]:
        return {key: set(ids) for key, ids in self._bids.items()}, dict(self._at)

    def put(self, env: str, slug: str, need_id: str, bid_at: float) -> None:
        key = (env, slug)
        prev_ids = set(self._bids.get(key, set()))
        prev_at = self._at.get((env, slug, need_id))
        self._bids.setdefault(key, set()).add(need_id)
        self._at[(env, slug, need_id)] = bid_at
        try:
            self._write()
        except OSError:
            self._restore(key, prev_ids, (env, slug, need_id), prev_at)
            raise

    def drop(self, env: str, slug: str, need_id: str) -> None:
        key = (env, slug)
        prev_ids = set(self._bids.get(key, set()))
        prev_at = self._at.get((env, slug, need_id))
        ids = self._bids.get(key)
        if ids is not None:
            ids.discard(need_id)
            if not ids:
                self._bids.pop(key, None)
        self._at.pop((env, slug, need_id), None)
        try:
            self._write()
        except OSError:
            self._restore(key, prev_ids, (env, slug, need_id), prev_at)
            raise

    def _restore(self, key: tuple[str, str], prev_ids: set[str], at_key: tuple[str, str, str], prev_at: Optional[float]) -> None:
        if prev_ids:
            self._bids[key] = set(prev_ids)
        else:
            self._bids.pop(key, None)
        if prev_at is None:
            self._at.pop(at_key, None)
        else:
            self._at[at_key] = prev_at

    def _read(self) -> tuple[dict[tuple[str, str], set[str]], dict[tuple[str, str, str], float]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}, {}
        except (OSError, ValueError) as exc:
            logger.warning("needs board state unreadable at %s (%s)", self.path, type(exc).__name__)
            return {}, {}
        bids: dict[tuple[str, str], set[str]] = {}
        bid_at: dict[tuple[str, str, str], float] = {}
        now = time.time()
        rows = raw.get("open_bids") if isinstance(raw, dict) else None
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("env") and row.get("slug") and isinstance(row.get("need_ids"), list):
                env, slug = str(row["env"]), str(row["slug"])
                ids = {str(n) for n in row["need_ids"] if n}
                if not ids:
                    continue
                bids[(env, slug)] = ids
                recorded = row.get("bid_at") if isinstance(row.get("bid_at"), dict) else {}
                for need_id in ids:
                    at = recorded.get(need_id)
                    bid_at[(env, slug, need_id)] = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else now
        return bids, bid_at

    def _write(self) -> None:
        rows = [
            {
                "env": env,
                "slug": slug,
                "need_ids": sorted(ids),
                "bid_at": {need_id: self._at.get((env, slug, need_id)) for need_id in sorted(ids)},
            }
            for (env, slug), ids in sorted(self._bids.items())
            if ids
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"open_bids": rows}), encoding="utf-8")
        os.replace(tmp, self.path)


class PostgresBidStore(BidStore):
    """Open bids in the Genesis job database (``runtime.pg_store``).

    The same database holds jobs, AP2 nonces and the audit chain. A failed
    write raises ``StoreUnavailable`` (or ``OSError``) and the caller refuses
    the bid. Nothing here touches a Render disk.
    """

    def load(self) -> tuple[dict[tuple[str, str], set[str]], dict[tuple[str, str, str], float]]:
        from runtime import pg_store

        bids: dict[tuple[str, str], set[str]] = {}
        bid_at: dict[tuple[str, str, str], float] = {}
        with pg_store.transaction() as cur:
            cur.execute("SELECT env, slug, need_id, bid_at FROM genesis_needs_bids")
            for row in cur.fetchall():
                env, slug, need_id = str(row["env"]), str(row["slug"]), str(row["need_id"])
                bids.setdefault((env, slug), set()).add(need_id)
                bid_at[(env, slug, need_id)] = float(row["bid_at"])
        return bids, bid_at

    def put(self, env: str, slug: str, need_id: str, bid_at: float) -> None:
        from runtime import pg_store

        with pg_store.transaction() as cur:
            cur.execute(
                """
                INSERT INTO genesis_needs_bids (env, slug, need_id, bid_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (env, slug, need_id) DO NOTHING
                """,
                (env, slug, need_id, bid_at),
            )

    def drop(self, env: str, slug: str, need_id: str) -> None:
        from runtime import pg_store

        with pg_store.transaction() as cur:
            cur.execute(
                "DELETE FROM genesis_needs_bids WHERE env = %s AND slug = %s AND need_id = %s",
                (env, slug, need_id),
            )


def bid_store_from_env() -> BidStore:
    """Where open bids are kept.

    Postgres when the Genesis job database is selected — the same store as the
    other restart-critical state. An explicit ``GENESIS_NEEDS_STATE_PATH`` is
    used only when Postgres is not selected (tests and local files). There is
    no ``/var/data`` default. With neither, puts fail and bids are refused.
    """
    from runtime import pg_store

    if pg_store.postgres_selected():
        return PostgresBidStore()
    explicit = os.getenv("GENESIS_NEEDS_STATE_PATH", "").strip()
    if explicit:
        return FileBidStore(Path(explicit))
    return UnavailableBidStore()


def should_deliver(work_order: dict[str, Any], agent_id: str) -> bool:
    """An active, escrow-held work order for this agent that has not been delivered yet."""
    need = work_order.get("need") or {}
    agreement = work_order.get("serviceAgreement") or {}
    escrow = agreement.get("escrow") or {}
    return (
        str(agreement.get("agentId") or "").lower() == agent_id
        and agreement.get("status") == "ACTIVE"
        and bool(agreement.get("escrowId"))
        and escrow.get("status") in (None, "HELD")
        and need.get("status") not in ("VERIFICATION_PENDING", "FULFILLED", "CANCELLED")
        and agreement.get("latestVerification") is None
    )


class NeedsBoard:
    def __init__(
        self,
        provider: ap2_provider.Ap2Provider,
        executor: Executor,
        *,
        state_path: Optional[Path] = None,
        bid_store: Optional[BidStore] = None,
    ) -> None:
        self.provider = provider
        self._execute = executor
        # (env, slug) -> need ids this runtime bid on and has not finished with. Kept in bid_store so a
        # restart neither loses an accepted bid (its escrow would sit undelivered when the work-orders
        # route is missing) nor bids again on a need it already bid on. state_path is the explicit file
        # override used by tests; production passes the Postgres store and does not default a path.
        if bid_store is None and state_path is not None:
            bid_store = FileBidStore(state_path)
        self._store = bid_store
        self._bid_at: dict[tuple[str, str, str], float] = {}
        self.open_bids: dict[tuple[str, str], set[str]] = {}
        self._load_bids()
        self._done: set[str] = set()
        self._in_flight: set[str] = set()
        self._next_work_poll: dict[tuple[str, str], float] = {}
        self._next_scan: dict[str, float] = {}
        self.ledger: dict[str, dict[str, Any]] = {}

    def _load_bids(self) -> None:
        if self._store is None:
            return
        try:
            loaded, bid_at = self._store.load()
        except Exception as exc:  # noqa: BLE001 - an unreadable store must not look like "no bids, go bid"
            logger.error("needs bid state unreadable (%s); refusing new bids", type(exc).__name__)
            self._store = UnavailableBidStore()
            return
        self.open_bids = loaded
        self._bid_at = bid_at

    def _track_bid(self, cred: ap2_provider.AgentCredential, need_id: str) -> None:
        """Record the bid durably BEFORE it is sent. Raises when the write does not stick."""
        at = self._bid_at.get((cred.env, cred.slug, need_id), time.time())
        if self._store is not None:
            self._store.put(cred.env, cred.slug, need_id, at)
        self.open_bids.setdefault((cred.env, cred.slug), set()).add(need_id)
        self._bid_at[(cred.env, cred.slug, need_id)] = at

    def _forget_bid(self, cred: ap2_provider.AgentCredential, need_id: str) -> None:
        if self._store is not None:
            try:
                self._store.drop(cred.env, cred.slug, need_id)
            except Exception as exc:  # noqa: BLE001 - keep the pointer when the delete does not stick
                logger.warning("bid %s for %s not forgotten (%s)", need_id, cred.slug, type(exc).__name__)
                return
        self._bid_at.pop((cred.env, cred.slug, need_id), None)
        ids = self.open_bids.get((cred.env, cred.slug))
        if ids and need_id in ids:
            ids.discard(need_id)

    def _record(self, need_id: str, **fields: Any) -> None:
        entry = self.ledger.setdefault(need_id, {"need_id": need_id})
        entry.update(fields)
        entry["updated_at"] = time.time()

    # -- finding buyers -----------------------------------------------------------------------------

    async def find_buyers(
        self, slug: str, *, environment: Optional[str] = None, max_bids: int = DEFAULT_MAX_BIDS, dry_run: bool = False
    ) -> dict[str, Any]:
        """Look at open needs that match this agent and bid on the best ones. Returns what it saw and did."""
        spec = marketplace_agent.load_spec(slug)
        if spec is None:
            raise LookupError(f"{slug} has no marketplace spec")
        cred = self.provider.credential(slug, environment, ap2_provider.SELLER)
        client = self.provider._client(cred)
        max_bids = max(0, min(int(max_bids), MAX_BIDS_CAP))
        status, data = await client._call("GET", f"/needs/for-agent/{cred.agent_id}")
        source = "for-agent"
        needs = _needs_list(data) if status == 200 else []
        if status != 200 or not needs:
            # Agents without registered capabilities get no matches there; the public feed is the fallback.
            status, data = await client._call("GET", "/needs/feed", params={"limit": 50}, auth=False)
            source = "feed"
            if status != 200:
                raise RuntimeError(f"needs board unavailable: HTTP {status}")
            needs = _needs_list(data)
        key = (cred.env, cred.slug)
        already = self.open_bids.setdefault(key, set())
        ranked: list[tuple[int, dict[str, Any]]] = []
        skipped: list[dict[str, Any]] = []
        for need in needs:
            if need["id"] in already:
                continue
            ok, why = assess_need(spec, need)
            if not ok:
                skipped.append({"need_id": need["id"], "title": str(need.get("title") or "")[:120], "reason": why})
                continue
            fit = marketplace_agent.fit_score(spec, _need_text(need))
            ranked.append((fit, need))
        ranked.sort(key=lambda row: (-row[0], -(row[1].get("budgetCents") or 0)))
        bids: list[dict[str, Any]] = []
        for _, need in ranked[:max_bids]:
            price = bid_price_cents(spec, need.get("budgetCents"))
            assert price is not None
            message = bid_message(spec, need, price)
            row = {"need_id": need["id"], "title": str(need.get("title") or "")[:120], "price_cents": price}
            if dry_run:
                row["message"] = message
                bids.append(row)
                continue
            # Persist first. A bid that exists only in memory is lost on restart, and when the
            # work-order list route is missing that memory was the only pointer to held escrow.
            # A failed write refuses the bid: nothing is POSTed.
            try:
                self._track_bid(cred, need["id"])
            except Exception as exc:  # noqa: BLE001 - fail closed; do not bid what we cannot remember
                logger.warning("refusing to bid on need %s as %s (%s)", need["id"], cred.slug, type(exc).__name__)
                row["error"] = "bid_state_not_durable"
                bids.append(row)
                continue
            try:
                st, body = await client._call(
                    "POST",
                    f"/needs/{need['id']}/respond",
                    body={"responderAgentId": cred.agent_id, "message": message, "proposedPriceCents": price},
                )
            except Exception:
                # The POST did not stick on our side. Drop the reservation so a later tick can retry.
                # A server that actually accepted it answers 409 next time, and that re-tracks the bid.
                self._forget_bid(cred, need["id"])
                raise
            row["http_status"] = st
            if st < 300 or st == 409:
                # 409: this agent already bid on the need (a restart lost the record); the row is kept.
                self._next_work_poll.pop(key, None)
                self._record(need["id"], role="bidder", env=cred.env, agent=cred.slug, bid_cents=price,
                             status="BID" if st < 300 else "ALREADY_BID")
            else:
                self._forget_bid(cred, need["id"])
                row["error"] = body if isinstance(body, (str, dict)) else str(body)[:200]
                logger.warning("bid on need %s as %s -> %s %.200s", need["id"], cred.slug, st, body)
            bids.append(row)
        return {
            "agent": slug,
            "env": cred.env,
            "source": source,
            "needs_seen": len(needs),
            "bids": bids,
            "skipped": skipped[:25],
            "dry_run": dry_run,
        }

    # -- delivering won work orders ----------------------------------------------------------------

    async def _work_orders(self, cred: ap2_provider.AgentCredential, client: Any) -> list[dict[str, Any]]:
        """Active work orders for this agent: the marketplace route when it exists, else the needs it bid on."""
        status, data = await client._call("GET", f"/needs/work-orders/for-agent/{cred.agent_id}")
        if status == 200 and isinstance(data, list):
            return [w for w in data if isinstance(w, dict)]
        orders: list[dict[str, Any]] = []
        for need_id in sorted(self.open_bids.get((cred.env, cred.slug), set())):
            st, body = await client._call("GET", f"/needs/{need_id}/work-order")
            if st == 200 and isinstance(body, dict):
                orders.append(body)
            elif st == 404:
                # No work order yet also means the buyer has not accepted the bid, so a 404 alone does not end it.
                # Keep the bid until it is delivered, or until it is old enough to count as never accepted.
                bid_at = self._bid_at.setdefault((cred.env, cred.slug, need_id), time.time())
                if time.time() - bid_at >= OPEN_BID_TTL_SECONDS:
                    self._forget_bid(cred, need_id)
        return orders

    async def deliver(self, cred: ap2_provider.AgentCredential, client: Any, work_order: dict[str, Any]) -> None:
        need = work_order.get("need") or {}
        need_id = str(need.get("id") or "")
        key = f"need:{cred.env}:{need_id}"
        if not need_id or key in self._done or key in self._in_flight:
            return
        self._in_flight.add(key)
        try:
            output = await self._execute(cred.slug, need_job_text(need))
            body = build_need_delivery(cred, need, output, self.provider.key, self.provider.runtime_sha256)
            status, data = await client._call("POST", f"/needs/{need_id}/deliver", body=body)
            self._record(need_id, role="seller", env=cred.env, agent=cred.slug, deliver_status=status,
                         result_sha256=body["evidence"]["result_sha256"])
            # Forget the bid only when the delivery landed, or the marketplace says it already
            # did (409). A 400/401/403/404/408/422/429/5xx leaves the bid in place so a later tick
            # can retry. Marking those done would keep the bid and never try again.
            if status < 300 or status == 409:
                self._done.add(key)
                self._forget_bid(cred, need_id)
            if status >= 400:
                logger.warning("need deliver %s -> %s %.200s", need_id, status, data)
            else:
                logger.info("delivered need %s as %s", need_id, cred.slug)
        finally:
            self._in_flight.discard(key)

    async def tick(self) -> None:
        now = time.time()
        for cred in list(self.provider.credentials):
            if not cred.can(ap2_provider.SELLER) or marketplace_agent.load_spec(cred.slug) is None:
                continue
            key = (cred.env, cred.slug)
            if cred.slug in autoscan_slugs() and now >= self._next_scan.get(cred.slug, 0.0):
                self._next_scan[cred.slug] = now + SCAN_SECONDS
                try:
                    await self.find_buyers(cred.slug, environment=cred.env)
                except Exception as exc:  # noqa: BLE001 - a failed scan must not stop delivery
                    logger.warning("needs scan for %s failed: %s", cred.slug, exc)
            if now < self._next_work_poll.get(key, 0.0):
                continue
            # One agent's failure (a network error, a model error) must not stop delivery for the others, or for
            # this agent's other work orders: their escrow is already held.
            try:
                client = self.provider._client(cred)
                orders = await self._work_orders(cred, client)
            except Exception as exc:  # noqa: BLE001
                logger.warning("work-order poll for %s failed: %s", cred.slug, exc)
                continue
            pending = bool(self.open_bids.get(key))
            self._next_work_poll[key] = now + (WORK_ORDER_POLL_SECONDS if pending else IDLE_WORK_ORDER_POLL_SECONDS)
            for order in orders:
                try:
                    if should_deliver(order, cred.agent_id):
                        await self.deliver(cred, client, order)
                except Exception as exc:  # noqa: BLE001
                    need_id = str((order.get("need") or {}).get("id") or "?")
                    logger.warning("delivery of need %s as %s failed, will retry: %s", need_id, cred.slug, exc)
                    self._next_work_poll[key] = now + WORK_ORDER_POLL_SECONDS

    async def run_forever(self, interval: float) -> None:
        logger.info("needs board started, interval=%.0fs", interval)
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive any single failure
                logger.exception("needs board tick failed")
            await asyncio.sleep(interval)

    def status(self) -> dict[str, Any]:
        return {
            "autoscan_slugs": sorted(autoscan_slugs()),
            "open_bids": {f"{e}/{s}": sorted(ids) for (e, s), ids in self.open_bids.items() if ids},
            "ledger": sorted(self.ledger.values(), key=lambda e: e.get("updated_at", 0), reverse=True)[:100],
        }
