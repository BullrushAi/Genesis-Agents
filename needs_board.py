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
        self, provider: ap2_provider.Ap2Provider, executor: Executor, *, state_path: Optional[Path] = None
    ) -> None:
        self.provider = provider
        self._execute = executor
        # (env, slug) -> need ids this runtime bid on and has not finished with. Kept in state_path when one is
        # given, so a restart neither loses an accepted bid (its escrow would sit undelivered when the
        # work-orders route is missing) nor bids again on a need it already bid on.
        self._state_path = state_path
        self.open_bids: dict[tuple[str, str], set[str]] = self._load_bids()
        self._done: set[str] = set()
        self._in_flight: set[str] = set()
        self._next_work_poll: dict[tuple[str, str], float] = {}
        self._next_scan: dict[str, float] = {}
        self.ledger: dict[str, dict[str, Any]] = {}

    def _load_bids(self) -> dict[tuple[str, str], set[str]]:
        if self._state_path is None:
            return {}
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("needs board state unreadable at %s: %s", self._state_path, exc)
            return {}
        bids: dict[tuple[str, str], set[str]] = {}
        rows = raw.get("open_bids") if isinstance(raw, dict) else None
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("env") and row.get("slug") and isinstance(row.get("need_ids"), list):
                bids[(str(row["env"]), str(row["slug"]))] = {str(n) for n in row["need_ids"] if n}
        return bids

    def _save_bids(self) -> None:
        if self._state_path is None:
            return
        rows = [{"env": e, "slug": s, "need_ids": sorted(ids)} for (e, s), ids in sorted(self.open_bids.items()) if ids]
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_name(self._state_path.name + ".tmp")
            tmp.write_text(json.dumps({"open_bids": rows}), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError as exc:
            logger.warning("needs board state not saved to %s: %s", self._state_path, exc)

    def _forget_bid(self, cred: ap2_provider.AgentCredential, need_id: str) -> None:
        ids = self.open_bids.get((cred.env, cred.slug))
        if ids and need_id in ids:
            ids.discard(need_id)
            self._save_bids()

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
            st, body = await client._call(
                "POST",
                f"/needs/{need['id']}/respond",
                body={"responderAgentId": cred.agent_id, "message": message, "proposedPriceCents": price},
            )
            row["http_status"] = st
            if st < 300 or st == 409:
                # 409: this agent already bid on the need (a restart lost the record); track it again.
                already.add(need["id"])
                self._save_bids()
                self._next_work_poll.pop(key, None)
                self._record(need["id"], role="bidder", env=cred.env, agent=cred.slug, bid_cents=price,
                             status="BID" if st < 300 else "ALREADY_BID")
            else:
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
            if status < 300 or (400 <= status < 500 and status != 429):
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
