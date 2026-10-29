"""AP2 provider / buyer contract for Genesis agents on the open SwarmSync marketplace (SSCERT-28).

Genesis is one independent provider on the marketplace. It uses the same public rails any outside agent
uses and holds no privileged channel:

* Identity: each Genesis agent holds its own SwarmSync agent API key (``x-agent-api-key``), bound to that
  agent only. Nothing here is derived from the gateway shared secret.
* Seller: negotiation answers still arrive at ``/agents/{slug}/negotiate`` and go back through
  ``/ap2/gateway/respond`` (main.py). This module adds the missing half: once a negotiation is ACCEPTED
  and its escrow is held, run the bounded job, hash and sign the output, and ``POST /ap2/deliver``.
  Conduit verification and settlement are SwarmSync's; Genesis never releases, refunds or verifies.
* Buyer: discover a seller through public discovery, ``POST /ap2/negotiate`` with the agent's own key and
  an explicit budget, accept the seller's counter when it is within that budget, and then wait. The
  buyer's spend authority is the funds in its own agent wallet (sponsor voucher) and the offer it made.

Containment: this module never imports ``escrow_client`` and never calls ``/payments/*``. It is inert
unless ``GENESIS_AP2_PROVIDER_ENABLED`` is true and credentials exist for a trusted SwarmSync environment.

Managed credentials (``GENESIS_MANAGED_AGENTS``, JSON, no secrets in it)::

    {"default": {"genesis-knowledge-evidence": {"agent_id": "<uuid>", "roles": ["buyer"]}}}

For an agent listed there the gateway obtains its own agent-bound key from SwarmSync
(``POST /ap2/gateway/agent-credential``, authenticated by that environment's gateway secret, honoured only for
agents the gateway provably hosts) and rotates it every few hours. No human session is involved.

Static credentials (``GENESIS_AGENT_CREDENTIALS``, secret, JSON)::

    {"cert-staging": {"genesis-fin-close": {"agent_id": "<uuid>", "api_key": "<key>",
                                            "roles": ["seller", "buyer"]}}}

``roles`` defaults to ``["seller"]``. An environment name must exist in ``swarmsync_environments``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import swarmsync_environments as swarmsync_envs

logger = logging.getLogger("ap2_provider")

SELLER = "seller"
BUYER = "buyer"
PROCURE_MARK = "[genesis-procure]"
MAX_INPUT_CHARS = 8000
MAX_OUTPUT_CHARS = 20000
DEFAULT_BUYER_CAP_CENTS = 50
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

Executor = Callable[[str, str], Awaitable[str]]


def enabled() -> bool:
    return os.getenv("GENESIS_AP2_PROVIDER_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def buyer_auto_settle() -> bool:
    """Whether this runtime's purchases consent to Conduit-driven release (default on: an autonomous buyer
    has no human to click verify). The marketplace still enforces binding, hash and escrow checks."""
    return os.getenv("GENESIS_AP2_BUYER_AUTO_SETTLE", "true").strip().lower() not in {"0", "false", "no"}


def load_managed(is_canonical: Callable[[str], bool], raw: Optional[str] = None) -> list[tuple[str, str, str, frozenset[str]]]:
    """Parse ``GENESIS_MANAGED_AGENTS`` into (environment, slug, agent_id, roles). Bad entries are skipped."""
    raw = (os.getenv("GENESIS_MANAGED_AGENTS", "") if raw is None else raw).strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        logger.error("GENESIS_MANAGED_AGENTS is not valid JSON")
        return []
    trusted = {e.name for e in swarmsync_envs.environments()}
    out: list[tuple[str, str, str, frozenset[str]]] = []
    for env_name, agents in (data.items() if isinstance(data, dict) else []):
        if env_name not in trusted or not isinstance(agents, dict):
            logger.error("GENESIS_MANAGED_AGENTS: environment %r is not trusted", env_name)
            continue
        for slug, entry in agents.items():
            if not isinstance(entry, dict):
                continue
            agent_id = str(entry.get("agent_id") or "")
            roles = frozenset(r for r in (entry.get("roles") or [SELLER]) if r in {SELLER, BUYER})
            if not is_canonical(slug) or not _UUID.match(agent_id) or not roles:
                logger.error("GENESIS_MANAGED_AGENTS: %s/%s skipped", env_name, slug)
                continue
            out.append((env_name, slug, agent_id.lower(), roles))
    return out


async def fetch_gateway_credential(
    api_url: str, gateway_secret: str, agent_id: str, timeout: float = 20.0, http: Optional[httpx.AsyncClient] = None
) -> tuple[int, Any]:
    """Ask SwarmSync for this hosted agent's own key, authenticated as the gateway of that environment."""
    url = f"{api_url.rstrip('/')}/ap2/gateway/agent-credential"
    headers = {"x-agent-gateway-secret": gateway_secret, "content-type": "application/json"}
    if http is not None:
        resp = await http.post(url, json={"agentId": agent_id}, headers=headers, timeout=timeout)
    else:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.post(url, json={"agentId": agent_id}, headers=headers)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, resp.text[:200]


def idle_poll_seconds() -> float:
    """How long an agent with no live negotiation waits between inbox polls (default 15 minutes).

    Every poll is a database read on SwarmSync's side, and most hosted agents have nothing open, so polling them
    all every tick drove the marketplace database over its egress quota (Oct 29). An agent with a live deal, or
    one that was just offered work or just bought, is polled every tick as before."""
    try:
        return max(0.0, float(os.getenv("GENESIS_AP2_PROVIDER_IDLE_POLL_SECONDS", "900")))
    except ValueError:
        return 900.0


LIVE_STATUSES = {"PENDING", "COUNTERED"}


def has_live_deal(negotiations: list[Any]) -> bool:
    """Whether a later poll could lead this runtime to act: an open offer or counter, or an accepted deal that is
    neither delivered nor verified yet."""
    for n in negotiations:
        if not isinstance(n, dict):
            continue
        status = n.get("status")
        if status in LIVE_STATUSES:
            return True
        if status == "ACCEPTED" and not n.get("workResult") and n.get("verificationStatus") is None:
            return True
    return False


def buyer_cap_cents() -> int:
    try:
        return max(0, int(os.getenv("GENESIS_AP2_BUYER_MAX_CENTS", str(DEFAULT_BUYER_CAP_CENTS))))
    except ValueError:
        return DEFAULT_BUYER_CAP_CENTS


# ---------------------------------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentCredential:
    env: str
    slug: str
    agent_id: str
    api_key: str = field(repr=False)
    roles: frozenset[str] = frozenset({SELLER})

    def can(self, role: str) -> bool:
        return role in self.roles


def load_credentials(
    is_canonical: Callable[[str], bool],
    raw: Optional[str] = None,
) -> list[AgentCredential]:
    """Parse ``GENESIS_AGENT_CREDENTIALS``. Bad entries are skipped and logged (names only, never keys)."""
    raw = (os.getenv("GENESIS_AGENT_CREDENTIALS", "") if raw is None else raw).strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        logger.error("GENESIS_AGENT_CREDENTIALS is not valid JSON; the AP2 provider has no credentials")
        return []
    if not isinstance(data, dict):
        logger.error("GENESIS_AGENT_CREDENTIALS must be an object keyed by environment name")
        return []
    trusted = {e.name for e in swarmsync_envs.environments()}
    out: list[AgentCredential] = []
    for env_name, agents in data.items():
        if env_name not in trusted:
            logger.error("GENESIS_AGENT_CREDENTIALS: environment %r is not a trusted SwarmSync environment", env_name)
            continue
        if not isinstance(agents, dict):
            continue
        for slug, entry in agents.items():
            if not isinstance(entry, dict) or not is_canonical(slug):
                logger.error("GENESIS_AGENT_CREDENTIALS: %s/%s skipped (not a canonical agent entry)", env_name, slug)
                continue
            agent_id = str(entry.get("agent_id") or "")
            api_key = str(entry.get("api_key") or "")
            roles = entry.get("roles") or [SELLER]
            if not _UUID.match(agent_id) or not api_key or not isinstance(roles, list):
                logger.error("GENESIS_AGENT_CREDENTIALS: %s/%s skipped (agent_id or api_key invalid)", env_name, slug)
                continue
            valid_roles = frozenset(r for r in roles if r in {SELLER, BUYER})
            if not valid_roles:
                continue
            out.append(AgentCredential(env_name, slug, agent_id.lower(), api_key, valid_roles))
    return out


# ---------------------------------------------------------------------------------------------------
# Runtime signing identity (the same kind of Ed25519 identity any outside runtime generates)
# ---------------------------------------------------------------------------------------------------


class RuntimeKey:
    def __init__(self, private: Ed25519PrivateKey, persistent: bool) -> None:
        self._private = private
        self.persistent = persistent
        self.public_raw = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    @classmethod
    def load(cls) -> "RuntimeKey":
        b64 = os.getenv("GENESIS_RUNTIME_ED25519_PKCS8_B64", "").strip()
        if b64:
            key = serialization.load_der_private_key(base64.b64decode(b64), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError("GENESIS_RUNTIME_ED25519_PKCS8_B64 is not an Ed25519 key")
            return cls(key, True)
        logger.warning("GENESIS_RUNTIME_ED25519_PKCS8_B64 unset: using an ephemeral runtime key")
        return cls(Ed25519PrivateKey.generate(), False)

    @property
    def public_b64(self) -> str:
        return base64.b64encode(self.public_raw).decode()

    @property
    def public_b64url(self) -> str:
        return base64.urlsafe_b64encode(self.public_raw).decode().rstrip("=")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.public_raw).hexdigest()

    def sign(self, data: bytes) -> str:
        return base64.b64encode(self._private.sign(data)).decode()


# ---------------------------------------------------------------------------------------------------
# Delivery construction (pure functions: no network, no clock)
# ---------------------------------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """Key-sorted compact JSON: the same output always hashes the same."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def job_input(negotiation: dict[str, Any]) -> str:
    """The bounded job text a buyer asked for: its requirements first, then the service description.

    Every requirement field reaches the agent. The preferred prose keys
    (text/input/prompt/task/description) come first, verbatim; every remaining
    field follows under its own name. A task that references a sibling field
    (``workflow_definition``, ``deliverable``, ...) must find that field in the
    job text - dropping it makes the agent fail closed on a job the buyer
    actually specified in full.
    """
    req = negotiation.get("requirements")
    parts: list[str] = []
    if isinstance(req, dict):
        preferred = ("text", "input", "prompt", "task", "description")
        emitted: set[str] = set()
        for key in preferred:
            value = req.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
                emitted.add(key)
        for key in sorted(req):
            if key in emitted:
                continue
            value = req.get(key)
            if isinstance(value, str):
                if value.strip():
                    parts.append(f"{key}: {value.strip()}")
            elif value is not None:
                parts.append(f"{key}: {json.dumps(value, sort_keys=True, ensure_ascii=False)}")
    service = str(negotiation.get("requestedService") or "").strip()
    notes = str(negotiation.get("notes") or "").strip()
    head = f"Service requested: {service}" if service else ""
    text = "\n\n".join(p for p in [head, *parts, notes if notes and PROCURE_MARK not in notes else ""] if p)
    return text[:MAX_INPUT_CHARS]


def build_delivery(
    credential: AgentCredential,
    negotiation: dict[str, Any],
    output: str,
    key: RuntimeKey,
    runtime_sha256: str,
) -> dict[str, Any]:
    """The ``POST /ap2/deliver`` body: result, its hash and signature, and the Conduit inline content."""
    result = {
        "service": f"Genesis {credential.slug}",
        "agent": credential.slug,
        "job": "bounded",
        "negotiation_id": negotiation["id"],
        "output": output[:MAX_OUTPUT_CHARS],
    }
    digest = hashlib.sha256(canonical_json(result).encode("utf-8")).digest()
    return {
        "negotiationId": negotiation["id"],
        "responderAgentId": credential.agent_id,
        "result": result,
        "evidence": {
            "runtime_sha256": runtime_sha256,
            "result_sha256": digest.hex(),
            "signature_ed25519_b64": key.sign(digest),
            "public_key_fingerprint": key.fingerprint,
        },
        "notes": f"Delivered by the Genesis {credential.slug} runtime.",
        "verificationHints": {
            "inlineContent": canonical_json(result),
            "requestId": f"genesis-{negotiation['id']}",
        },
    }


def should_fulfil(negotiation: dict[str, Any], agent_id: str) -> bool:
    """An accepted, escrow-held job addressed to this agent that has not been delivered yet."""
    responder = negotiation.get("responderAgent") or {}
    return (
        negotiation.get("status") == "ACCEPTED"
        and str(responder.get("id") or "").lower() == agent_id
        and bool(negotiation.get("escrowId"))
        and not negotiation.get("workResult")
        and negotiation.get("verificationStatus") is None
    )


def counter_price(negotiation: dict[str, Any]) -> Optional[float]:
    counter = negotiation.get("counter")
    price = counter.get("price") if isinstance(counter, dict) else None
    return float(price) if isinstance(price, (int, float)) and not isinstance(price, bool) else None


def should_accept_counter(negotiation: dict[str, Any], agent_id: str, cap_cents: int) -> Optional[float]:
    """The counter price to accept, or None. Only this runtime's own procurements, never above the offer or cap."""
    requester = negotiation.get("requesterAgent") or {}
    if (
        negotiation.get("status") != "COUNTERED"
        or str(requester.get("id") or "").lower() != agent_id
        or PROCURE_MARK not in str(negotiation.get("notes") or "")
    ):
        return None
    price = counter_price(negotiation)
    budget = negotiation.get("proposedBudget")
    if price is None or not isinstance(budget, (int, float)):
        return None
    cents = round(price * 100)
    if cents <= 0 or cents > round(float(budget) * 100) or cents > cap_cents:
        return None
    return cents / 100


def rank_sellers(agents: list[dict[str, Any]], query: str, exclude_ids: set[str]) -> list[dict[str, Any]]:
    """Best match first: words of the query found in the listing, then the cheaper listing."""
    words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for agent in agents:
        agent_id = str(agent.get("id") or "").lower()
        if not agent_id or agent_id in exclude_ids:
            continue
        haystack = " ".join(
            str(agent.get(k) or "") for k in ("name", "description", "slug")
        ).lower() + " " + " ".join(str(t) for t in (agent.get("tags") or []) + (agent.get("categories") or [])).lower()
        hits = sum(1 for w in words if w in haystack)
        if words and hits == 0:
            continue
        price = agent.get("basePriceCents")
        scored.append((-hits, int(price) if isinstance(price, (int, float)) else 10**9, agent))
    scored.sort(key=lambda row: (row[0], row[1], str(row[2].get("slug") or "")))
    return [row[2] for row in scored]


# ---------------------------------------------------------------------------------------------------
# SwarmSync client (public rails only; one agent's own key)
# ---------------------------------------------------------------------------------------------------


class SwarmSyncAgentClient:
    def __init__(
        self, api_url: str, agent_id: str, api_key: str, timeout: float = 20.0, http: Optional[httpx.AsyncClient] = None
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.agent_id = agent_id
        self._api_key = api_key
        self._timeout = timeout
        # One long-lived client owned by the provider: building an httpx client per request re-creates the TLS context and
        # a fresh connection (plus resolver threads) on every poll, ~19 credentials every few seconds, for the life of the process.
        self._http = http

    async def _call(
        self, method: str, path: str, *, body: Any = None, params: Optional[dict[str, Any]] = None, auth: bool = True
    ) -> tuple[int, Any]:
        headers = {"content-type": "application/json"}
        if auth:
            headers["x-agent-api-key"] = self._api_key
        if self._http is not None:
            resp = await self._http.request(
                method, f"{self.api_url}{path}", json=body, params=params, headers=headers, timeout=self._timeout
            )
        else:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
                resp = await client.request(method, f"{self.api_url}{path}", json=body, params=params, headers=headers)
        try:
            data: Any = resp.json()
        except ValueError:
            data = resp.text[:300]
        return resp.status_code, data

    async def my_negotiations(self) -> tuple[int, Any]:
        return await self._call("GET", "/ap2/negotiations/my", params={"agentId": self.agent_id})

    async def negotiation(self, negotiation_id: str) -> tuple[int, Any]:
        return await self._call("GET", f"/ap2/negotiations/{negotiation_id}")

    async def accept(self, negotiation_id: str, responder_agent_id: str, price: float) -> tuple[int, Any]:
        return await self._call(
            "POST",
            "/ap2/respond",
            body={
                "negotiationId": negotiation_id,
                "responderAgentId": responder_agent_id,
                "status": "ACCEPTED",
                "price": price,
            },
        )

    async def deliver(self, body: dict[str, Any]) -> tuple[int, Any]:
        return await self._call("POST", "/ap2/deliver", body=body)

    async def negotiate(self, body: dict[str, Any]) -> tuple[int, Any]:
        return await self._call("POST", "/ap2/negotiate", body=body)

    async def search_agents(self, query: str, limit: int = 25) -> tuple[int, Any]:
        return await self._call("GET", "/agents", params={"search": query, "limit": limit}, auth=False)

    async def redeem_voucher(self, body: dict[str, Any]) -> tuple[int, Any]:
        return await self._call("POST", "/wallet-vouchers/redeem", body=body)

    async def wallet(self) -> tuple[int, Any]:
        """The agent's own wallet, read with its own key (balance only; the key cannot do more than its scopes)."""
        return await self._call("GET", f"/wallets/agent/{self.agent_id}")


def _agents_list(data: Any) -> list[dict[str, Any]]:
    items = data if isinstance(data, list) else (data or {}).get("agents") or (data or {}).get("items") or []
    return [a for a in items if isinstance(a, dict)]


# ---------------------------------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------------------------------


class Ap2Provider:
    def __init__(
        self,
        credentials: list[AgentCredential],
        executor: Executor,
        key: RuntimeKey,
        runtime_sha256: str,
        client_factory: Optional[Callable[[str, str, str], SwarmSyncAgentClient]] = None,
        managed: Optional[list[tuple[str, str, str, frozenset[str]]]] = None,
        credential_fetcher: Optional[Callable[[str, str, str], Awaitable[tuple[int, Any]]]] = None,
    ) -> None:
        self.credentials = list(credentials)
        self.managed = managed or []
        self._http: Optional[httpx.AsyncClient] = httpx.AsyncClient(timeout=20.0, follow_redirects=False)
        self._fetch_credential = credential_fetcher or (
            lambda api_url, secret, agent_id: fetch_gateway_credential(api_url, secret, agent_id, http=self._http)
        )
        self._managed_issued_at: dict[tuple[str, str], float] = {}
        # Failure backoff for managed credential provisioning: a failed fetch
        # must NOT be retried on every 8s tick. _managed_failures counts
        # consecutive failures per (environment, slug); _managed_next_retry is
        # the earliest time that agent may be retried again.
        self._managed_failures: dict[tuple[str, str], int] = {}
        self._managed_next_retry: dict[tuple[str, str], float] = {}
        self._execute = executor
        self.key = key
        self.runtime_sha256 = runtime_sha256
        self._client_factory = client_factory or (
            lambda url, agent_id, api_key: SwarmSyncAgentClient(url, agent_id, api_key, http=self._http)
        )
        self._done: set[str] = set()
        self._in_flight: set[str] = set()
        self.ledger: dict[str, dict[str, Any]] = {}
        self.last_tick_at: Optional[float] = None
        self.tick_errors = 0
        # (env, slug) -> earliest time.time() of the next inbox poll; absent means poll on the next tick.
        self._next_poll_at: dict[tuple[str, str], float] = {}

    # -- plumbing ---------------------------------------------------------------------------------

    def _client(self, cred: AgentCredential) -> SwarmSyncAgentClient:
        env = next((e for e in swarmsync_envs.environments() if e.name == cred.env), None)
        if env is None:
            raise RuntimeError(f"environment {cred.env} is no longer trusted")
        return self._client_factory(env.api_url, cred.agent_id, cred.api_key)

    def credential(self, slug: str, env: Optional[str], role: str) -> AgentCredential:
        matches = [c for c in self.credentials if c.slug == slug and c.can(role) and (env is None or c.env == env)]
        if not matches:
            raise LookupError(f"no {role} credential for {slug}" + (f" in {env}" if env else ""))
        if len(matches) > 1:
            raise LookupError(f"{slug} has credentials in several environments; pass environment")
        return matches[0]

    def _record(self, negotiation_id: str, **fields: Any) -> None:
        entry = self.ledger.setdefault(negotiation_id, {"negotiation_id": negotiation_id})
        entry.update(fields)
        entry["updated_at"] = time.time()

    # -- seller: fulfil accepted jobs --------------------------------------------------------------

    async def fulfil(self, cred: AgentCredential, client: SwarmSyncAgentClient, negotiation: dict[str, Any]) -> None:
        nid = negotiation["id"]
        key = f"deliver:{cred.env}:{nid}"
        if key in self._done or key in self._in_flight:
            return
        self._in_flight.add(key)
        try:
            output = await self._execute(cred.slug, job_input(negotiation))
            body = build_delivery(cred, negotiation, output, self.key, self.runtime_sha256)
            status, data = await client.deliver(body)
            self._record(
                nid, role=SELLER, env=cred.env, agent=cred.slug, deliver_status=status,
                result_sha256=body["evidence"]["result_sha256"],
            )
            if status < 300 or (400 <= status < 500 and status != 429):
                self._done.add(key)
            if status >= 400:
                logger.warning("deliver %s -> %s %.200s", nid, status, data)
            else:
                logger.info("delivered %s as %s -> %s", nid, cred.slug, status)
        finally:
            self._in_flight.discard(key)

    # -- buyer: accept counters on our own procurements -------------------------------------------

    async def settle_counter(self, cred: AgentCredential, client: SwarmSyncAgentClient, negotiation: dict[str, Any]) -> None:
        price = should_accept_counter(negotiation, cred.agent_id, buyer_cap_cents())
        if price is None:
            return
        nid = negotiation["id"]
        key = f"accept:{cred.env}:{nid}"
        if key in self._done or key in self._in_flight:
            return
        self._in_flight.add(key)
        try:
            responder_id = (negotiation.get("responderAgent") or {}).get("id")
            status, data = await client.accept(nid, responder_id, price)
            self._record(nid, role=BUYER, env=cred.env, agent=cred.slug, accept_status=status, accepted_price=price)
            if status < 300 or (400 <= status < 500 and status != 429):
                self._done.add(key)
            if status >= 400:
                logger.warning("accept %s -> %s %.200s", nid, status, data)
        finally:
            self._in_flight.discard(key)

    # -- loop --------------------------------------------------------------------------------------

    async def refresh_managed(self, max_age: float = 6 * 3600.0) -> None:
        """Obtain, and periodically rotate, the own key of each managed agent. A failure keeps the old key."""
        now = time.time()
        for env_name, slug, agent_id, roles in self.managed:
            key = (env_name, slug)
            issued = self._managed_issued_at.get(key)
            if issued is not None and now - issued < max_age:
                continue
            if now < self._managed_next_retry.get(key, 0):
                continue
            env = next((e for e in swarmsync_envs.environments() if e.name == env_name), None)
            if env is None or not env.gateway_secret:
                logger.error("managed credential for %s/%s: no gateway secret for that environment", env_name, slug)
                continue
            try:
                status, data = await self._fetch_credential(env.api_url, env.gateway_secret, agent_id)
            except Exception as exc:  # noqa: BLE001 - keep the loop alive; back off, do not retry next tick
                failures = self._managed_failures.get(key, 0) + 1
                self._managed_failures[key] = failures
                delay = min(60 * (2 ** (failures - 1)), 1800)
                self._managed_next_retry[key] = now + delay
                logger.warning(
                    "managed credential for %s/%s failed: %s; next retry in %ss",
                    env_name, slug, type(exc).__name__, delay,
                )
                continue
            if status not in (200, 201) or not isinstance(data, dict) or not data.get("apiKey"):
                failures = self._managed_failures.get(key, 0) + 1
                self._managed_failures[key] = failures
                base = 120 if status == 429 else 60
                delay = min(base * (2 ** (failures - 1)), 1800)
                self._managed_next_retry[key] = now + delay
                logger.warning(
                    "managed credential for %s/%s refused: HTTP %s; next retry in %ss",
                    env_name, slug, status, delay,
                )
                continue
            fresh = AgentCredential(env_name, slug, agent_id, str(data["apiKey"]), roles)
            self.credentials = [c for c in self.credentials if not (c.env == env_name and c.slug == slug)] + [fresh]
            self._managed_issued_at[key] = now
            self._managed_failures.pop(key, None)
            self._managed_next_retry.pop(key, None)
            logger.info("managed credential for %s/%s issued", env_name, slug)

    def wake(self, slug: str) -> None:
        """Poll this agent on the next tick: it was just offered work, or just opened a purchase."""
        for key in [k for k in self._next_poll_at if k[1] == slug]:
            del self._next_poll_at[key]

    async def tick(self) -> None:
        await self.refresh_managed()
        for cred in self.credentials:
            key = (cred.env, cred.slug)
            if time.time() < self._next_poll_at.get(key, 0.0):
                continue
            client = self._client(cred)
            status, data = await client.my_negotiations()
            if status != 200 or not isinstance(data, list):
                logger.warning("poll %s/%s -> %s", cred.env, cred.slug, status)
                continue
            if has_live_deal(data):
                self._next_poll_at.pop(key, None)
            else:
                self._next_poll_at[key] = time.time() + idle_poll_seconds()
            for negotiation in data:
                if not isinstance(negotiation, dict) or "id" not in negotiation:
                    continue
                self._observe(cred, negotiation)
                if cred.can(SELLER) and should_fulfil(negotiation, cred.agent_id):
                    await self.fulfil(cred, client, negotiation)
                elif cred.can(BUYER):
                    await self.settle_counter(cred, client, negotiation)
        self.last_tick_at = time.time()

    def _observe(self, cred: AgentCredential, negotiation: dict[str, Any]) -> None:
        requester = (negotiation.get("requesterAgent") or {}).get("id")
        if str(requester or "").lower() == cred.agent_id and PROCURE_MARK in str(negotiation.get("notes") or ""):
            tx = negotiation.get("transaction") or {}
            self._record(
                negotiation["id"], role=BUYER, env=cred.env, agent=cred.slug, status=negotiation.get("status"),
                escrow_id=negotiation.get("escrowId"), verification=negotiation.get("verificationStatus"),
                conduit_verified=negotiation.get("conduitVerified"), transaction_status=tx.get("status"),
            )

    async def aclose(self) -> None:
        """Release the shared HTTP client (called once at shutdown)."""
        http, self._http = self._http, None
        if http is not None:
            await http.aclose()

    async def run_forever(self, interval: float) -> None:
        logger.info("ap2 provider started: %d credential(s), interval=%.1fs", len(self.credentials), interval)
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive any single failure
                self.tick_errors += 1
                logger.exception("ap2 provider tick failed")
            await asyncio.sleep(interval)

    # -- buyer: procure ----------------------------------------------------------------------------

    async def procure(
        self,
        slug: str,
        *,
        need: str,
        budget: float,
        query: str,
        requirements: Optional[dict[str, Any]] = None,
        environment: Optional[str] = None,
    ) -> dict[str, Any]:
        """Discover a seller and open a negotiation as this Genesis agent, with its own key and an explicit offer."""
        cred = self.credential(slug, environment, BUYER)
        cents = round(budget * 100)
        if cents <= 0 or abs(budget * 100 - cents) > 1e-6:
            raise ValueError("budget must be positive with at most 2 decimal places")
        if cents > buyer_cap_cents():
            raise ValueError(f"budget exceeds the buyer cap of {buyer_cap_cents()} cents")
        client = self._client(cred)
        status, data = await client.search_agents(query)
        if status != 200:
            raise RuntimeError(f"discovery failed: HTTP {status}")
        candidates = rank_sellers(_agents_list(data), query, exclude_ids={cred.agent_id})
        if not candidates:
            raise LookupError(f"discovery found no seller for {query!r}")
        seller = candidates[0]
        nonce = uuid.uuid4().hex[:12]
        status, body = await client.negotiate(
            {
                "requesterAgentId": cred.agent_id,
                "responderAgentId": seller["id"],
                "requestedService": need,
                "budget": cents / 100,
                "requirements": requirements or {},
                "notes": f"{PROCURE_MARK} {nonce}",
                "autoSettle": buyer_auto_settle(),
            }
        )
        result = {
            "http_status": status,
            "seller": {k: seller.get(k) for k in ("id", "slug", "name", "basePriceCents")},
            "candidates_considered": [c.get("slug") for c in candidates[:5]],
            "negotiation": body if status < 300 else None,
            "error": None if status < 300 else body,
        }
        if status < 300 and isinstance(body, dict) and body.get("id"):
            self._record(body["id"], role=BUYER, env=cred.env, agent=cred.slug, status=body.get("status"),
                         seller=seller.get("slug"), budget=cents / 100)
            self.wake(cred.slug)
        return result

    async def wallet(self, slug: str, environment: Optional[str] = None) -> dict[str, Any]:
        """Balance and reserved funds of this agent's own wallet, through its own credential."""
        cred = self.credential(slug, environment, BUYER)
        status, data = await self._client(cred).wallet()
        if status != 200 or not isinstance(data, dict):
            return {"http_status": status, "agent_id": cred.agent_id}
        return {
            "http_status": status,
            "agent_id": cred.agent_id,
            "wallet_id": data.get("id"),
            "currency": data.get("currency"),
            "balance": data.get("balance"),
            "reserved": data.get("reserved"),
        }

    async def redeem_voucher(self, slug: str, *, voucher_token: str, nonce: str, run_id: str, environment: Optional[str] = None) -> dict[str, Any]:
        """Redeem a sponsor-issued voucher into this agent's own wallet, signed by the runtime key it was issued to."""
        cred = self.credential(slug, environment, BUYER)
        message = f"swarmsync.wallet-voucher.redeem.v1|{nonce}|{run_id}|{cred.agent_id}".encode()
        status, data = await self._client(cred).redeem_voucher(
            {
                "voucher_token": voucher_token,
                "run_id": run_id,
                "runtime_public_key": self.key.public_b64,
                "runtime_signature": self.key.sign(message),
            }
        )
        return {"http_status": status, "body": data}

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "credentials": [
                {"env": c.env, "slug": c.slug, "agent_id": c.agent_id, "roles": sorted(c.roles)} for c in self.credentials
            ],
            "runtime": {
                "ed25519_public_key_b64": self.key.public_b64,
                "fingerprint": self.key.fingerprint,
                "persistent": self.key.persistent,
                "runtime_sha256": self.runtime_sha256,
            },
            "buyer_cap_cents": buyer_cap_cents(),
            "last_tick_at": self.last_tick_at,
            "tick_errors": self.tick_errors,
            "ledger": sorted(self.ledger.values(), key=lambda e: e.get("updated_at", 0), reverse=True)[:100],
        }

