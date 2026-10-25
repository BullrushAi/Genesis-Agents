"""
SwarmSync Agent Gateway
=======================
Single FastAPI service that exposes all Genesis agents under one deployment.

Each agent is accessible at:  POST /agents/{slug}/run
  Body: { "prompt": "...", "testContext": true }
  Response: { "response": "..." }

Conduit-heavy agents declare `job_mode: "async"`. Real `/run` requests for
those bundles return a queued `{job_id, poll_url}` payload immediately so
Render's request proxy is never held open while browser automation starts.

Canonical testContext/live-test requests use the same exact native-Anthropic
forced Haiku candidate and separately bound Haiku safety-judge path as canonical runtime
execution. A gateway/OpenAI configuration fails closed before provider dispatch.

GENESIS_LLM_MODEL and SwarmSync routing remain legacy/noncanonical behavior;
they cannot select or override either canonical V5 model.

Environment variables:
  LLM_API_KEY         - native Anthropic key required for canonical V5
  PORT                - server port (default 8000)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import subprocess
import logging
import uuid
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from typing import Optional, Any
from collections.abc import Mapping

import httpx
import ap2_provider
import swarmsync_environments as swarmsync_envs
import verification_job_store
from browser_install import ensure_chromium_installed
from fastapi import Depends, FastAPI, HTTPException, Header, BackgroundTasks, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from canonical_result_contract import (
    CANONICAL_RESULT_CONTRACT_VERSION,
    CANONICAL_STATUSES,
    FINANCE_AGENT_IDS,
    consume_canonical_result_submission,
    validate_canonical_result_semantics,
)
from runtime.financeos_governed_input import (
    bind_financeos_governed_context,
    extract_raw_context,
    fetch_live_financeos_context,
)
from runtime.semantic_safety_judge import (
    CANDIDATE_MODEL,
    CANDIDATE_READ_TIMEOUT_S,
    ENDPOINT_READ_BUDGET_S,
    ENDPOINT_RESERVED_OVERHEAD_S,
    JUDGE_READ_TIMEOUT_S,
    JUDGE_MODEL,
    POLICY_VERSION as SEMANTIC_JUDGE_POLICY_VERSION,
    REASON_CANDIDATE_TRANSPORT_FAILED,
    ExactAnthropicTransportError,
    call_exact_anthropic_judge,
    call_exact_anthropic_candidate,
    canonical_sha256,
    judge_candidate_semantics,
    require_canonical_native_anthropic_config,
    require_exact_model_role_binding,
)
from runtime.router_canonical import (
    TRANSPORT_NATIVE,
    call_router_canonical_candidate,
    call_router_judge,
    config_rejected,
    router_fallback_allowed,
)
from runtime.anthropic_wire import (
    exact_forced_tool_envelope_reason,
)

# Verification system
from verification_models import (
    ArbitrageVerificationRequest,
    ArbitrageVerificationResult,
    VerifyRequest,
    VerifyJobStatus,
    VerificationResult,
    VerificationSpec,
    VerificationContext,
)
from conduit_verifier import run_verification

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Strong references to fire-and-forget background tasks (asyncio only keeps weak refs).
_background_tasks: set = set()


# Phase 2 — consolidated agent runtime backed by skill bundles.
# Loaded lazily on first /agents/{slug}/run call so the gateway still starts
# even if conduit-browser or aiohttp aren't yet installed in the environment.
try:
    from bundle_loader import load_bundle, list_bundles
    from agent_runtime import AgentRuntime
    _RUNTIME_IMPORT_OK = True
except Exception:  # pragma: no cover - ultra-defensive
    load_bundle = None  # type: ignore[assignment]
    list_bundles = None  # type: ignore[assignment]
    AgentRuntime = None  # type: ignore[assignment]
    _RUNTIME_IMPORT_OK = False
    logger.warning("agent_runtime/bundle_loader could not be imported; persona fallback only")

# Phase 10 - capability cards + marketplace listing endpoints.
from capability_cards import card_for, all_cards

# Phase 9 - output storage + Conduit session delivery.
from pathlib import Path
from fastapi.responses import FileResponse, HTMLResponse
from artifact_store import upload_dir, get_signed_url, list_artifacts

# Static assets directory (Genesis admin UI, etc.). Bundled with the
# agents-gateway Render service via the rootDir build context.
_STATIC_DIR = Path(__file__).parent / "static"
from conduit_sessions import store_session, load_session, delete_session

# Phase 8 — Genesis job state machine. Imported lazily-tolerant so the gateway
# still boots if psycopg or DATABASE_URL aren't configured in dev.
try:
    from job_store import create_job, get_job
    _JOB_STORE_OK = True
except Exception:  # pragma: no cover - ultra-defensive
    create_job = None  # type: ignore[assignment]
    get_job = None  # type: ignore[assignment]
    _JOB_STORE_OK = False
    logger.warning("job_store could not be imported; async job endpoints disabled")

# Phase 7 — VCAP proof bundles. Tolerant import for environments without
# `cryptography` (the verify endpoint will return 503 in that case).
try:
    from proof_bridge import verify_vcap_wrapper_jwt
    _PROOF_BRIDGE_OK = True
except Exception:  # pragma: no cover - ultra-defensive
    verify_vcap_wrapper_jwt = None  # type: ignore[assignment]
    _PROOF_BRIDGE_OK = False
    logger.warning("proof_bridge could not be imported; /proofs/* disabled")

# Phase 6 — escrow client wrappers around /payments/ap2/* on swarmsync-api.
#
# ESCROW CONTAINMENT (docs/FINANCE-TOOL-CONTRACTS.md Sections 3.8 and 6.3).
# All four escrow functions are PERMANENTLY_PROHIBITED. They construct and
# transmit funds movements with no per-call human authorization anywhere in the
# path. escrow_guard.escrow_permitted() is the single choke point: it returns
# True only under an explicit GENESIS_DEPLOYMENT_PROFILE=swarmsync-marketplace.
# Unset, or the Cato-facing profile, leaves _ESCROW_OK False, which makes every
# escrow call site below unreachable — the same code path the gateway already
# takes for free-tier (Cato) callers, so nothing new can break.
from escrow_guard import escrow_permitted as _escrow_permitted

if not _escrow_permitted():
    initiate_escrow = None  # type: ignore[assignment]
    complete_escrow = None  # type: ignore[assignment]
    release_escrow = None  # type: ignore[assignment]
    calculate_split = None  # type: ignore[assignment]
    _ESCROW_OK = False
    logger.warning(
        "escrow containment active: escrow_client is NOT bound in this build. "
        "Automation may prepare; a human pays; automation records."
    )
else:
    try:
        from escrow_client import (
            initiate_escrow,
            complete_escrow,
            release_escrow,
            calculate_split,
        )
        _ESCROW_OK = True
    except Exception:  # pragma: no cover - ultra-defensive
        initiate_escrow = None  # type: ignore[assignment]
        complete_escrow = None  # type: ignore[assignment]
        release_escrow = None  # type: ignore[assignment]
        calculate_split = None  # type: ignore[assignment]
        _ESCROW_OK = False
        logger.warning("escrow_client could not be imported; running free-tier only")

_RUNTIME: Any = None


def _get_runtime() -> Any:
    """Return a process-wide AgentRuntime singleton.

    Reads LLM_API_URL / LLM_API_KEY at construction time. Returns None if the
    runtime module failed to import.
    """
    global _RUNTIME
    if not _RUNTIME_IMPORT_OK:
        return None
    if _RUNTIME is None:
        _RUNTIME = AgentRuntime(llm_url=_llm_api_url(), llm_key=_llm_api_key())
    else:
        # Allow env rotation without process restart (local dev / Render reload).
        _RUNTIME.llm_key = _llm_api_key()
        _RUNTIME.llm_url = _llm_api_url()
    return _RUNTIME


_GENESIS_AUTO_WORKER_TASK: Optional[Any] = None


async def _genesis_auto_worker_loop() -> None:
    """In-process background worker loop (Phase 7).

    Activated by GENESIS_WORKER_ENABLED=true. Polls for QUEUED jobs on a fixed
    interval WITHOUT calling the internal tick HTTP endpoint.
    """
    from worker import run_tick, _worker_state  # noqa: PLC0415

    interval = float(os.getenv("GENESIS_WORKER_INTERVAL_SECONDS", "10"))
    tick_limit = int(os.getenv("GENESIS_WORKER_TICK_LIMIT", "3"))
    _worker_state["enabled"] = True
    logger.info(
        "genesis_auto_worker started interval=%.1fs tick_limit=%d", interval, tick_limit
    )
    while True:
        try:
            result = await run_tick(limit=tick_limit, expire_stale=True)
            _worker_state["last_tick_at"] = __import__("time").time()
            if result.get("claimed", 0) > 0:
                logger.info(
                    "genesis_auto_worker tick claimed=%d processed=%d jobs=%s",
                    result["claimed"],
                    result["processed"],
                    result.get("job_ids", []),
                )
        except Exception:
            logger.exception("genesis_auto_worker tick error")
        await asyncio.sleep(interval)


_AP2_PROVIDER: Optional[ap2_provider.Ap2Provider] = None
_AP2_PROVIDER_TASK: Optional[Any] = None


async def _execute_bounded_job(slug: str, prompt: str) -> str:
    """Run one bounded job for an accepted AP2 sale: the agent's reviewed bundle prompt over the buyer's input.

    Same persona/LLM path as negotiation evaluation: no tools, no external effects, text out. The canonical
    business-completion firewall still applies to what the text may claim; delivery is analysis, not an action.
    """
    import library_agents

    if library_agents.is_library_slug(slug):
        raw = await call_llm_router(library_agents.system_prompt(slug), prompt)
        text, _ = _router_result_payload(raw)
        return text

    from bundle_loader import load_bundle

    canonical_slug = _require_canonical_agent(slug)
    bundle = load_bundle(canonical_slug)
    if bundle is None:
        raise RuntimeError(f"no reviewed bundle for {canonical_slug}")
    raw = await call_llm_router(str(bundle["system_prompt"]), prompt)
    text, _ = _router_result_payload(raw)
    return text


def _start_ap2_provider() -> None:
    """Start the AP2 provider/buyer loop. Inert unless explicitly enabled and credentialed."""
    global _AP2_PROVIDER, _AP2_PROVIDER_TASK
    if not ap2_provider.enabled():
        return
    from canonical_profiles import CANONICAL_SLUGS

    import library_agents

    def _is_hosted_agent(slug: str) -> bool:
        return slug in CANONICAL_SLUGS or library_agents.is_library_slug(slug)

    credentials = ap2_provider.load_credentials(is_canonical=_is_hosted_agent)
    managed = ap2_provider.load_managed(is_canonical=_is_hosted_agent)
    if not credentials and not managed:
        logger.warning("GENESIS_AP2_PROVIDER_ENABLED is set but there are no valid credentials; provider not started")
        return
    import hashlib as _hashlib

    runtime_sha = _hashlib.sha256(open(__file__, "rb").read() + open(ap2_provider.__file__, "rb").read()).hexdigest()
    _AP2_PROVIDER = ap2_provider.Ap2Provider(
        credentials, _execute_bounded_job, ap2_provider.RuntimeKey.load(), runtime_sha, managed=managed
    )
    interval = float(os.getenv("GENESIS_AP2_PROVIDER_POLL_SECONDS", "8"))
    _AP2_PROVIDER_TASK = asyncio.create_task(_AP2_PROVIDER.run_forever(interval))
    _background_tasks.add(_AP2_PROVIDER_TASK)
    _AP2_PROVIDER_TASK.add_done_callback(_background_tasks.discard)
    logger.info("ap2 provider enabled and started for %d credential(s), %d managed agent(s)", len(credentials), len(managed))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup guards, verification recovery and the optional in-process worker."""
    global _GENESIS_AUTO_WORKER_TASK

    # Canonical provider role contracts are source-owned and distinct even
    # though both use exact Haiku. Refuse startup if either binding is altered.
    require_exact_model_role_binding()

    # PERMANENTLY_PROHIBITED enforcement, Layer 2
    # (docs/FINANCE-TOOL-CONTRACTS.md Section 6.2).
    #
    # This runs BEFORE the app accepts traffic and is deliberately NOT wrapped
    # in try/except. If a prohibited money tool has been re-registered, or the
    # frozen manifest no longer matches the prohibition list, the process
    # REFUSES TO START. Fail-to-boot beats fail-to-deny: a denial can be missed
    # in logs; a service that will not start cannot be.
    import tools as _tools
    from runtime.tool_policy import assert_prohibitions_intact

    _tools.register_default_tools()
    assert_prohibitions_intact()
    logger.info("prohibition guard: intact (registry and frozen manifest verified)")

    # Escrow containment, Section 6.3. Also unconditional.
    from escrow_guard import assert_escrow_containment

    assert_escrow_containment()

    # docs/FINANCE-TOOL-CONTRACTS.md Section 7 "Related precondition".
    assert_gateway_key_configured()
    logger.info("gateway auth guard: GATEWAY_API_KEY is configured")

    assert_auth_material_configured()
    logger.info("signed-identity guard: AP2 nonce store and token keys are configured")

    # Chromium is installed lazily on first use (browser_install.ensure_chromium_installed), never at boot:
    # Render wipes ~/.cache/ms-playwright on restart and a boot-time download starves the 512Mi instance.

    # Phase 7: in-process auto-worker (does NOT use the tick HTTP endpoint)
    genesis_worker_enabled = os.getenv("GENESIS_WORKER_ENABLED", "").lower() in {
        "1", "true", "yes"
    }
    if genesis_worker_enabled and _JOB_STORE_OK:
        _GENESIS_AUTO_WORKER_TASK = asyncio.create_task(_genesis_auto_worker_loop())
        _background_tasks.add(_GENESIS_AUTO_WORKER_TASK)
        _GENESIS_AUTO_WORKER_TASK.add_done_callback(_background_tasks.discard)
        logger.info("genesis auto-worker enabled and started")

    # Finish verifications nobody is working on: boot recovery, then a periodic sweep that takes over a job as soon
    # as its owner's lease is released (graceful shutdown) or expires (crash). In the background: startup must not
    # wait on Chromium runs.
    recovery_task = asyncio.create_task(_verification_recovery_loop())

    _start_ap2_provider()

    yield

    await _release_verification_jobs_on_shutdown(recovery_task)

    if _AP2_PROVIDER_TASK is not None and not _AP2_PROVIDER_TASK.done():
        _AP2_PROVIDER_TASK.cancel()
        try:
            await _AP2_PROVIDER_TASK
        except asyncio.CancelledError:
            pass
    if _AP2_PROVIDER is not None:
        await _AP2_PROVIDER.aclose()

    if _GENESIS_AUTO_WORKER_TASK is not None and not _GENESIS_AUTO_WORKER_TASK.done():
        _GENESIS_AUTO_WORKER_TASK.cancel()
        try:
            await _GENESIS_AUTO_WORKER_TASK
        except asyncio.CancelledError:
            pass


_environment = os.getenv("ENVIRONMENT", "production")
_docs_url = "/docs" if _environment == "development" else None
_redoc_url = "/redoc" if _environment == "development" else None

app = FastAPI(
    title="SwarmSync Agent Gateway",
    version="1.0.3",
    lifespan=lifespan,
    docs_url=_docs_url,
    redoc_url=_redoc_url,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://swarmsync.ai",
        "https://www.swarmsync.ai",
        "https://api.swarmsync.ai",
    ],
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Agent-Api-Key", "X-Agent-Gateway-Secret", "X-AP2-Version", "X-AP2-Pubkey", "X-Genesis-Principal-Token"],
)


async def verify_gateway_key(
    x_agent_api_key: str = Header(default=None, alias="x-agent-api-key"),
    x_agent_gateway_secret: str = Header(default=None, alias="x-agent-gateway-secret"),
) -> None:
    """Dependency: validates caller identity against two secrets:

    1. X-Agent-Api-Key     — public API key for Cato, external tools, Render free-tier callers.
       Checked against GATEWAY_API_KEY env var.
    2. X-Agent-Gateway-Secret — internal shared secret sent by swarmsync-api's executeAgent()
       service when calling our own gateway. Checked against the gateway secret of every trusted
       SwarmSync environment (AGENT_GATEWAY_SECRET = "default", plus SWARMSYNC_ENVIRONMENTS).

    The lifespan startup guard (Section 7 of FINANCE-TOOL-CONTRACTS.md) now
    refuses to boot this process at all when GATEWAY_API_KEY is unset, so the
    fallback below is unreachable in a running instance — kept only as a
    defense-in-depth no-op for direct unit-level calls to this dependency.
    Either valid credential is sufficient; both may be provided.
    """
    expected_api_key = os.getenv("GATEWAY_API_KEY")
    if not expected_api_key:
        # Fail closed even when the dependency is invoked outside the normal
        # lifespan (tests, alternate ASGI hosts, or a misconfigured worker).
        raise HTTPException(status_code=503, detail="Gateway authentication is not configured")

    # Accept if X-Agent-Api-Key matches (constant-time comparison prevents timing oracle)
    if hmac.compare_digest(x_agent_api_key or "", expected_api_key):
        return

    # Accept if X-Agent-Gateway-Secret is the gateway secret of any trusted SwarmSync environment
    # (production AGENT_GATEWAY_SECRET plus those in SWARMSYNC_ENVIRONMENTS); constant time per secret.
    if swarmsync_envs.resolve_by_gateway_secret(x_agent_gateway_secret) is not None:
        return

    raise HTTPException(status_code=401, detail="Invalid or missing gateway credential")


def assert_envelope_binds_request(
    body: dict[str, Any], route_slug: str, *, params_field: str = "task"
) -> None:
    """Bind the unsigned execution fields to the signed AP2 payload.

    Cato signs ``{payload, nonce, timestamp}`` only, but Genesis executes from
    the top-level ``prompt``/``task`` (RunRequest's real fields). Without this
    binding an attacker who could modify the body in flight would keep a valid
    signature while swapping the agent, the prompt, or the parameters — the
    signature would prove nothing about what actually ran.

    Raises AuthenticationError; separated from the dependency so the binding
    itself is directly testable without a live signing key.
    """
    from runtime.request_auth import AuthenticationError, _eq

    payload = body.get("payload") or {}
    if not isinstance(payload, dict):
        raise AuthenticationError("ap2_envelope_invalid")
    # _eq compares UTF-8 bytes. hmac.compare_digest raises TypeError on any non-ASCII `str`, and
    # the caller below only catches AuthenticationError — so a legitimate prompt containing "é"
    # or "→" used to escape as an unhandled TypeError and surface as HTTP 500 rather than a
    # clean 401/200. Prompts are free text; non-ASCII is normal, not an attack.
    if not _eq(payload.get("agent"), route_slug):
        raise AuthenticationError("ap2_agent_mismatch")
    if not _eq(payload.get("task"), body.get("prompt")):
        raise AuthenticationError("ap2_task_mismatch")
    runtime_task = body.get(params_field) or {}
    signed_params = payload.get("params") or {}
    if not isinstance(runtime_task, dict) or not isinstance(signed_params, dict):
        raise AuthenticationError("ap2_params_invalid")
    # Cato adds an unsigned human-readable "description" to the runtime task.
    # Strip it ONLY when it is absent from the signed params — otherwise a
    # caller whose own params legitimately contain "description" has it dropped
    # from one side of the comparison and gets an opaque 401 for a correctly
    # signed request.
    unsigned_runtime = dict(runtime_task)
    if params_field == "task" and "description" not in signed_params:
        unsigned_runtime.pop("description", None)
    if unsigned_runtime != signed_params:
        raise AuthenticationError("ap2_params_mismatch")


async def verify_agent_principal(
    request: Request,
    x_ap2_version: str | None = Header(default=None, alias="X-AP2-Version"),
    x_ap2_pubkey: str | None = Header(default=None, alias="X-AP2-Pubkey"),
    x_agent_api_key: str | None = Header(default=None, alias="X-Agent-Api-Key"),
    x_agent_gateway_secret: str | None = Header(default=None, alias="X-Agent-Gateway-Secret"),
):
    """Prefer verified AP2 identity; retain a constrained legacy gateway principal."""
    from runtime.request_auth import AuthenticationError, legacy_gateway_principal, verify_ap2_envelope
    if x_ap2_version or x_ap2_pubkey:
        try:
            body = await request.json()
            principal = verify_ap2_envelope(
                body, header_version=x_ap2_version, header_pubkey=x_ap2_pubkey,
                required_scope="agent.invoke",
            )
            params_field = "params" if request.url.path.endswith("/jobs") else "task"
            assert_envelope_binds_request(
                body,
                str(request.path_params.get("slug") or ""),
                params_field=params_field,
            )
            return principal
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
    try:
        await verify_gateway_key(x_agent_api_key, x_agent_gateway_secret)
    except HTTPException as exc:
        raise
    principal = legacy_gateway_principal()
    return principal


async def verify_continuation_principal(
    x_genesis_principal_token: str | None = Header(default=None, alias="X-Genesis-Principal-Token"),
    x_agent_api_key: str | None = Header(default=None, alias="X-Agent-Api-Key"),
    x_agent_gateway_secret: str | None = Header(default=None, alias="X-Agent-Gateway-Secret"),
):
    """Authenticate a poll/read with a scoped continuation token or legacy key."""
    from runtime.request_auth import AuthenticationError, legacy_gateway_principal, verify_principal_token

    if x_genesis_principal_token:
        try:
            return verify_principal_token(x_genesis_principal_token)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
    await verify_gateway_key(x_agent_api_key, x_agent_gateway_secret)
    return legacy_gateway_principal()


@app.get("/auth/whoami")
async def auth_whoami(_auth=Depends(verify_gateway_key)):
    """Safe positive-auth probe: identify the caller without executing work."""
    from runtime.request_auth import legacy_gateway_principal
    principal = legacy_gateway_principal()
    return {
        "principal_id": principal.principal_id,
        "tenant_id": principal.tenant_id,
        "client_id": principal.client_id,
        "auth_method": principal.auth_method,
        "scopes": sorted(principal.scopes),
    }


def _require_owned_job(principal, job: dict[str, Any] | None, scope: str = "job.read") -> dict[str, Any]:
    from runtime.request_auth import owns_resource

    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if not principal.has_scope(scope):
        raise HTTPException(status_code=403, detail="principal scope denied")
    if not owns_resource(principal, job):
        raise HTTPException(status_code=403, detail="resource owner mismatch")
    return job


def _authorize_job_read(principal, job_id: str, scope: str = "job.read") -> None:
    """Authorize a side-channel job read (/events, /trace, /sandbox).

    These routes used to be gated on the shared GATEWAY_API_KEY alone, which meant the
    owner-scoped principal token Genesis itself mints for a queued job could not read them —
    a poller holding the ONLY credential legitimately scoped to that job got a 401.

    Every credential path reaches the same ownership check. The compatibility
    principal may read legacy unowned rows and rows it created itself, but it
    cannot read an AP2 tenant's side channels.
    """
    if not _JOB_STORE_OK or get_job is None:
        raise HTTPException(status_code=503, detail="job_store unavailable")
    _require_owned_job(principal, get_job(job_id), scope)


def _scoped_runtime_params(body: Any, principal) -> dict[str, Any]:
    """Accept only signed, bounded Ask E4L-resolved context for this tenant."""
    params = dict(body.task) if isinstance(body.task, dict) else {}
    raw_context = params.get("resolved_context", [])
    if raw_context is None:
        raw_context = []
    if not isinstance(raw_context, list) or len(raw_context) > 8:
        raise HTTPException(status_code=422, detail="resolved_context_invalid")
    clean = []
    from audit import _sanitize
    for item in raw_context:
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail="resolved_context_invalid")
        if str(item.get("tenant_id") or principal.tenant_id) != principal.tenant_id:
            raise HTTPException(status_code=403, detail="resolved_context_tenant_mismatch")
        citation = str(item.get("citation") or "")[:512]
        content_hash = str(item.get("content_sha256") or item.get("hash") or "")[:128]
        content = str(_sanitize(str(item.get("content") or "")))[:4000]
        if not citation or not content_hash or not content:
            raise HTTPException(status_code=422, detail="resolved_context_evidence_required")
        clean.append({"citation": citation, "content_sha256": content_hash, "content": content})
    params["resolved_context"] = clean
    params.pop("description", None)
    return params


def assert_gateway_key_configured() -> None:
    """docs/FINANCE-TOOL-CONTRACTS.md Section 7 "Related precondition".

    Called from lifespan() before the app accepts traffic, deliberately not
    wrapped in try/except (fail-to-boot beats fail-to-deny) — matching
    assert_prohibitions_intact() and assert_escrow_containment() above it in
    the startup sequence. Without GATEWAY_API_KEY, verify_gateway_key() above
    accepts every caller as authenticated, and Cato's authorization model
    assumes the gateway is authenticated — a fail-open front door makes every
    downstream policy/ledger/approval control merely advisory.
    """
    if not os.getenv("GATEWAY_API_KEY"):
        raise RuntimeError(
            "GATEWAY_API_KEY is not set. Refusing to start: without it, "
            "/agents/{slug}/run and /a2a accept every caller as authenticated "
            "(see verify_gateway_key above). Set GATEWAY_API_KEY before "
            "starting this service."
        )


# Every AP2 code path that mints or replay-protects an identity reads one of
# these. They are asserted at boot rather than at first use because the failure
# lands mid-request: issue_principal_token() is called AFTER create_job(), so a
# missing key returns HTTP 500 to a caller whose job (and escrow reservation)
# already exists, with no continuation token to poll it. Fail-to-boot beats
# fail-mid-transaction.
_REQUIRED_AUTH_ENV: tuple[tuple[str, int, str], ...] = (
    ("GENESIS_AUTH_DB_PATH", 1, "AP2 nonce replay protection has nowhere to record consumed nonces"),
    ("GENESIS_PRINCIPAL_TOKEN_KEY", 32, "queued jobs cannot be issued an owner-scoped polling token"),
    ("GENESIS_ACTION_GRANT_KEY", 32, "single-use action grants cannot be verified, so deployment-class tools can never be authorized"),
)

#: Env vars required only while this process is still on the SQLite backend.
#: With GENESIS_JOB_DATABASE_URL configured, nonce and grant state live in
#: Postgres and Render needs no persistent disk for correctness.
_SQLITE_ONLY_AUTH_ENV = frozenset({"GENESIS_AUTH_DB_PATH"})


def assert_auth_material_configured() -> None:
    """Refuse to start without the secrets the signed-identity path requires."""
    from runtime import pg_store

    on_postgres = pg_store.postgres_selected()
    missing = [
        f"{name} ({reason})"
        for name, minimum, reason in _REQUIRED_AUTH_ENV
        if not (on_postgres and name in _SQLITE_ONLY_AUTH_ENV)
        and len((os.getenv(name) or "").strip()) < minimum
    ]
    if missing:
        raise RuntimeError(
            "Refusing to start — signed-identity configuration is incomplete: "
            + "; ".join(missing)
            + ". See .env.example."
        )
    # A configured-but-unusable store is worse than an unset one: the boot guard passes and
    # every AP2 request then dies mid-verification. Prove the nonce store can be reached and
    # written NOW, while failing is still just a failed boot.
    #
    # On Postgres the equivalent lie is a reachable database that was never migrated. That is
    # not hypothetical — it is exactly what made every async AP2 dispatch return 500 while the
    # test suite stayed green against an in-memory mock, so the probe checks the tables exist.
    if on_postgres:
        usable, reason = pg_store.store_is_usable()
        if not usable:
            raise RuntimeError(
                "Refusing to start — GENESIS_JOB_DATABASE_URL is set but the Postgres state "
                f"store is not usable ({reason}). Replay protection, single-use action grants "
                "and the audit chain would all be unavailable."
            )
        return

    from runtime.request_auth import auth_db_is_usable

    usable, reason = auth_db_is_usable()
    if not usable:
        raise RuntimeError(
            "Refusing to start — GENESIS_AUTH_DB_PATH is set but the AP2 nonce store cannot "
            f"be opened for writing ({reason}). On Render this usually means no persistent "
            "disk is mounted at that path. Replay protection would be unavailable."
        )


async def require_admin(principal=Depends(verify_continuation_principal)) -> None:
    """Admin is a signed principal scope, never a self-asserted identity header.

    There is deliberately no email allowlist here.  The previous self-asserted
    identity header and its env allowlist were removed: an unsigned string a
    caller chooses is not a credential, and keeping the variable around would
    imply a control that no longer runs.  Admin rights come from the ``admin``
    scope on a verified principal, granted in ``trusted_ap2_clients.json``.
    """
    if not principal.has_scope("admin"):
        raise HTTPException(status_code=403, detail="admin access required")

# ---------------------------------------------------------------------------
# Agent persona registry
# Each entry: (display_name, system_prompt)
# ---------------------------------------------------------------------------
_CANONICAL_RESULT_FIELDS = (
    "agent_id, task_id, scope, status, summary, findings, proposals, evidence_refs, "
    "assumptions, conflicts, unknowns, confidence, risk_flags, "
    "recommended_next_actions, requested_human_decisions, tool_calls_summary, "
    "clarification_needed, candidate_resolution, reason_human_needed, "
    "recommended_human_resolver, materiality, urgency, resolution_attempts, "
    "supporting_evidence_refs, competing_state_refs, and recommended_answer_options"
)


def _openapi_with_actual_auth() -> dict[str, Any]:
    """Publish the authentication alternatives enforced by runtime dependencies."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schemes.update({
        "AgentApiKey": {"type": "apiKey", "in": "header", "name": "X-Agent-Api-Key"},
        "GatewaySecret": {"type": "apiKey", "in": "header", "name": "X-Agent-Gateway-Secret"},
        "AP2Version": {"type": "apiKey", "in": "header", "name": "X-AP2-Version"},
        "AP2Pubkey": {"type": "apiKey", "in": "header", "name": "X-AP2-Pubkey"},
        "PrincipalToken": {"type": "apiKey", "in": "header", "name": "X-Genesis-Principal-Token"},
    })
    def dependency_names(dependant) -> set[str]:
        names: set[str] = set()
        for dependency in getattr(dependant, "dependencies", []):
            call = getattr(dependency, "call", None)
            if call is not None:
                names.add(getattr(call, "__name__", ""))
            names.update(dependency_names(dependency))
        return names

    for route in app.routes:
        path = getattr(route, "path", None)
        dependant = getattr(route, "dependant", None)
        if not path or dependant is None or path not in schema.get("paths", {}):
            continue
        dependencies = dependency_names(dependant)
        if "require_admin" in dependencies:
            requirements = [{"PrincipalToken": []}]
        elif "verify_agent_principal" in dependencies:
            requirements = [
                {"AgentApiKey": []}, {"GatewaySecret": []},
                {"AP2Version": [], "AP2Pubkey": []},
            ]
        elif "verify_continuation_principal" in dependencies:
            requirements = [
                {"PrincipalToken": []}, {"AgentApiKey": []}, {"GatewaySecret": []},
            ]
        elif "verify_gateway_key" in dependencies:
            requirements = [{"AgentApiKey": []}, {"GatewaySecret": []}]
        else:
            continue
        for method in getattr(route, "methods", set()):
            operation = schema["paths"][path].get(method.lower())
            if operation is not None:
                operation["security"] = requirements
    # The admin and worker contracts are deliberately private.  They remain
    # routable for authenticated operators but are not advertised as public API.
    for path in list(schema.get("paths", {})):
        if path == "/admin" or path.startswith("/admin/") or path.startswith("/internal/"):
            del schema["paths"][path]
    # Publish the same generated roster accepted by every invocation route.
    from canonical_profiles import CANONICAL_SLUGS
    for path, operations in schema.get("paths", {}).items():
        if "{slug}" not in path:
            continue
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            for parameter in operation.get("parameters", []):
                if parameter.get("in") == "path" and parameter.get("name") == "slug":
                    parameter.setdefault("schema", {})["enum"] = sorted(CANONICAL_SLUGS)
    app.openapi_schema = schema
    return schema


app.openapi = _openapi_with_actual_auth


def _require_canonical_agent(slug: str) -> str:
    """Accept the generated roster and fail explicitly for retired clients."""
    from canonical_profiles import CANONICAL_SLUGS
    if slug in CANONICAL_SLUGS:
        return slug
    from bundle_loader import load_bundle
    if slug in AGENT_PERSONAS or load_bundle(slug) is not None:
        raise HTTPException(
            status_code=410,
            detail={
                "error": "legacy_agent_deprecated",
                "slug": slug,
                "canonical_catalogue": "/agents",
            },
        )
    raise HTTPException(status_code=404, detail="agent not found")


def _canonical_persona(
    name: str,
    role: str,
    restrictions: str,
    *,
    finance: bool = False,
) -> tuple[str, str]:
    """Build the fail-closed persona used by the live-test/router fallback path."""
    finance_boundary = (
        " FinanceOS owns finance workflow and control state; Xero is the official "
        "ledger. Return proposals only: never post to Xero, move money, approve, pay, "
        "file, or bypass the deterministic FinanceOS Worker."
        if finance
        else ""
    )
    prompt = (
        f"You are {name}, the canonical {role}. Cato chooses and bounds the job. "
        "Genesis provides analysis, not authority, and keeps no private durable memory. "
        "Fail closed: surface missing, inaccessible, stale, or conflicting evidence; "
        "separate fact from inference; refuse work outside permission; never treat a "
        "timeout or stub response as success; never guess consequential identifiers; "
        "and never silently broaden scope. Do not self-verify material work or claim "
        "VERIFIED, POSTED, DEPLOYED, PAID, APPROVED, FILED, or describe real-world "
        "work as complete unless an "
        "authoritative independent provider already established that state. "
        f"Use result contract {CANONICAL_RESULT_CONTRACT_VERSION}; status must be one of "
        f"{' | '.join(CANONICAL_STATUSES)}. ANALYSIS_COMPLETE means only that the bounded "
        "Genesis reasoning assignment completed, never that a business action completed. "
        "Before returning a non-null provider clarification object, exhaust authorized scoped "
        "sources and capabilities. "
        "Then provide a complete evidence-first candidate resolution; do not ask an employee, "
        "choose delivery timing/channel, persist clarification memory, or invent filler. "
        "Submit the exact forced-tool provider representation; the gateway deterministically "
        f"projects it to public machine-readable output containing {_CANONICAL_RESULT_FIELDS}. "
        f"Role boundary: {restrictions}.{finance_boundary}"
    )
    return name, prompt


AGENT_PERSONAS: dict[str, tuple[str, str]] = {
    # Genesis x402 agents
    "genesis_research_x402": (
        "Genesis Research Agent",
        "You are Genesis Research Agent, an expert AI research assistant that provides deep, "
        "citation-rich research reports. You synthesise information from multiple sources, "
        "identify key trends, and deliver clear, actionable insights. Respond concisely and "
        "stay in character as a professional research analyst.",
    ),
    "genesis_builder_x402": (
        "Genesis Builder Agent",
        "You are Genesis Builder Agent, an elite AI software engineer specialising in "
        "TypeScript, React, and modern full-stack development. You generate production-ready "
        "code, UI components, and architectural blueprints. Respond concisely and stay in "
        "character as a senior software architect.",
    ),
    "genesis_deploy_x402": (
        "Genesis Deploy Agent",
        "You are Genesis Deploy Agent, an expert in CI/CD pipelines and source-control "
        "delivery. You prepare code and release configuration and push it to GitHub. You have "
        "no hosting-provider deploy tool, so never claim a site is live. Respond concisely and "
        "stay in character as a DevOps engineer.",
    ),
    "genesis_content_x402": (
        "Genesis Content Agent",
        "You are Genesis Content Agent, a professional AI content strategist and writer. "
        "You create compelling blog posts, whitepapers, and social media copy that ranks "
        "well on search engines and resonates with target audiences. Respond concisely and "
        "stay in character as a senior content director.",
    ),
    "genesis_email_x402": (
        "Genesis Email Agent",
        "You are Genesis Email Agent, an expert email marketing specialist. You design "
        "high-converting email sequences, drip campaigns, and transactional templates that "
        "drive engagement and revenue. Respond concisely and stay in character as a "
        "conversion-focused email strategist.",
    ),
    "genesis_commerce_x402": (
        "Genesis Commerce Agent",
        "You are Genesis Commerce Agent, an e-commerce growth specialist with expertise "
        "in Shopify, WooCommerce, and multi-channel retail. You optimise product listings, "
        "checkout flows, and marketplace integrations. Respond concisely and stay in "
        "character as a senior e-commerce consultant.",
    ),
    "genesis_qa_x402": (
        "Genesis QA Agent",
        "You are Genesis QA Agent, a meticulous quality assurance engineer. You design "
        "comprehensive test suites, identify edge cases, and ensure software reliability "
        "using modern testing frameworks like Jest, Pytest, and Playwright. Respond "
        "concisely and stay in character as a QA lead.",
    ),
    "genesis_support_x402": (
        "Genesis Support Agent",
        "You are Genesis Support Agent, a customer success specialist who designs "
        "knowledge bases, FAQ systems, and support playbooks. You help companies "
        "deliver exceptional customer experiences at scale. Respond concisely and "
        "stay in character as a head of customer success.",
    ),
    "genesis_finance_x402": (
        "Genesis Finance Agent",
        "You are Genesis Finance Agent, a financial analyst and strategist. You produce "
        "financial models, cash-flow projections, and strategic finance plans for "
        "startups and growth-stage companies. Respond concisely and stay in character "
        "as a CFO-level advisor.",
    ),
    "genesis_security_x402": (
        "Genesis Security Agent",
        "You are Genesis Security Agent, a cybersecurity expert specialising in "
        "vulnerability assessment, penetration testing, and OWASP Top 10 remediation. "
        "You deliver security audits and actionable hardening recommendations. Respond "
        "concisely and stay in character as a senior security engineer.",
    ),
    "genesis_billing_x402": (
        "Genesis Billing Agent",
        "You are Genesis Billing Agent, a revenue operations specialist. You design "
        "billing systems, automate invoice workflows, and optimise subscription revenue. "
        "Respond concisely and stay in character as a RevOps director.",
    ),
    "genesis_analyst_x402": (
        "Genesis Analyst Agent",
        "You are Genesis Analyst Agent, a business intelligence and data analytics expert. "
        "You transform raw data into strategic insights using statistical analysis and "
        "visualisation. Respond concisely and stay in character as a senior data analyst.",
    ),
    "genesis_marketing_x402": (
        "Genesis Marketing Agent",
        "You are Genesis Marketing Agent, a full-stack marketing strategist. You create "
        "multi-channel campaigns, growth funnels, and go-to-market strategies that drive "
        "measurable results. Respond concisely and stay in character as a CMO-level advisor.",
    ),
    "genesis_seo_x402": (
        "Genesis SEO Agent",
        "You are Genesis SEO Agent, an SEO specialist who delivers keyword research, "
        "technical audits, and 12-month content strategies. You optimise sites for "
        "top SERP rankings and organic growth. Respond concisely and stay in character "
        "as a senior SEO strategist.",
    ),
    "genesis_meta_x402": (
        "Genesis Meta Agent",
        "You are Genesis Meta Agent, the flagship AI orchestrator for SwarmSync. You "
        "coordinate specialised agents to deliver complete business solutions — from "
        "ideation through to deployment — in hours instead of weeks. Respond concisely "
        "and stay in character as an autonomous business-generation system.",
    ),
    "genesis_meta_agent": (
        "Genesis Meta Agent",
        "You are Genesis Meta Agent, the autonomous business-generation orchestrator. "
        "You coordinate Builder, Research, Deploy, Content, QA, and other specialist "
        "agents to build complete businesses from idea to launch. Respond concisely and "
        "stay in character.",
    ),
    # AP2-style agents
    "builder_agent": (
        "Builder Agent",
        "You are Builder Agent, a software construction specialist using modern TypeScript "
        "and React. You build features from specifications with clean, testable code. "
        "Respond concisely and stay in character as a staff engineer.",
    ),
    "builder_agent_enhanced": (
        "Builder Agent Enhanced",
        "You are Builder Agent Enhanced, an advanced software builder with sophisticated "
        "planning, multi-step implementation, and architectural reasoning. Respond concisely "
        "and stay in character as a principal engineer.",
    ),
    "deploy_agent": (
        "Deploy Agent",
        "You are Deploy Agent, a cloud deployment and infrastructure specialist. You "
        "manage CI/CD pipelines and multi-cloud deployments reliably. Respond concisely "
        "and stay in character as a platform engineer.",
    ),
    "qa_agent": (
        "QA Agent",
        "You are QA Agent, a quality assurance specialist who designs and executes test "
        "plans to ensure software correctness. Respond concisely and stay in character "
        "as a QA engineer.",
    ),
    "research_discovery_agent": (
        "Research Discovery Agent",
        "You are Research Discovery Agent, a research specialist who gathers, synthesises, "
        "and distils information to drive informed decisions. Respond concisely and stay "
        "in character as a research lead.",
    ),
    "spec_agent": (
        "Spec Agent",
        "You are Spec Agent, a technical writer and architect who creates precise product "
        "specifications and API documentation. Respond concisely and stay in character "
        "as a solutions architect.",
    ),
    "security_agent": (
        "Security Agent",
        "You are Security Agent, a cybersecurity specialist focused on threat modelling, "
        "code review, and compliance. Respond concisely and stay in character as a "
        "security engineer.",
    ),
    "maintenance_agent": (
        "Maintenance Agent",
        "You are Maintenance Agent, an operational reliability specialist who monitors, "
        "patches, and improves software systems. Respond concisely and stay in character "
        "as an SRE.",
    ),
    "seo_agent": (
        "SEO Agent",
        "You are SEO Agent, a search optimisation specialist delivering keyword strategy "
        "and technical SEO fixes. Respond concisely and stay in character as an SEO expert.",
    ),
    "content_agent": (
        "Content Agent",
        "You are Content Agent, a content creation specialist producing high-quality "
        "written assets for marketing and communications. Respond concisely and stay "
        "in character as a senior copywriter.",
    ),
    "marketing_agent": (
        "Marketing Agent",
        "You are Marketing Agent, a growth marketing specialist who designs campaigns "
        "and funnels. Respond concisely and stay in character as a growth marketer.",
    ),
    "support_agent": (
        "Support Agent",
        "You are Support Agent, a customer experience specialist who resolves issues "
        "and builds support systems. Respond concisely and stay in character as a "
        "customer success manager.",
    ),
    "analyst_agent": (
        "Analyst Agent",
        "You are Analyst Agent, a data and business analyst who surfaces insights from "
        "data. Respond concisely and stay in character as a business analyst.",
    ),
    "finance_agent": (
        "Finance Agent",
        "You are Finance Agent, a financial modelling and strategy specialist. Respond "
        "concisely and stay in character as a financial analyst.",
    ),
    "pricing_agent": (
        "Pricing Agent",
        "You are Pricing Agent, a pricing strategy specialist who optimises monetisation "
        "models. Respond concisely and stay in character as a pricing expert.",
    ),
    "email_agent": (
        "Email Agent",
        "You are Email Agent, an email marketing specialist creating campaigns that "
        "convert. Respond concisely and stay in character as an email strategist.",
    ),
    "billing_agent": (
        "Billing Agent",
        "You are Billing Agent, a revenue operations specialist managing billing workflows "
        "and subscription systems. Respond concisely and stay in character as a billing "
        "engineer.",
    ),
    "commerce_agent": (
        "Commerce Agent",
        "You are Commerce Agent, an e-commerce specialist optimising online stores and "
        "marketplaces. Respond concisely and stay in character as an e-commerce consultant.",
    ),
    "darwin_agent": (
        "Darwin Agent",
        "You are Darwin Agent, an evolutionary AI that applies genetic algorithms and "
        "adaptive learning to optimise solutions iteratively. Respond concisely and stay "
        "in character as a machine-learning researcher.",
    ),
    "domain_name_agent": (
        "Domain Name Agent",
        "You are Domain Name Agent, a brand naming specialist who generates creative, "
        "available domain names for new ventures. Respond concisely and stay in character "
        "as a brand strategist.",
    ),
    "legal_agent": (
        "Legal Agent",
        "You are Legal Agent, an AI legal assistant that drafts contracts, reviews "
        "agreements, and provides guidance on compliance. Respond concisely and stay "
        "in character as a legal counsel. Note: not a substitute for qualified legal advice.",
    ),
    "onboarding_agent": (
        "Onboarding Agent",
        "You are Onboarding Agent, a user experience specialist who designs smooth "
        "onboarding flows for products. Respond concisely and stay in character as a "
        "UX designer.",
    ),
    "reflection_agent": (
        "Reflection Agent",
        "You are Reflection Agent, a meta-cognitive AI that reviews, critiques, and "
        "improves the outputs of other agents. Respond concisely and stay in character "
        "as a quality reviewer.",
    ),
    "waltzrl_conversation_agent": (
        "WaltzRL Conversation Agent",
        "You are WaltzRL Conversation Agent, a reinforcement-learning-powered dialogue "
        "agent that learns from feedback to improve conversation quality. Respond "
        "concisely and stay in character.",
    ),
    "waltzrl_feedback_agent": (
        "WaltzRL Feedback Agent",
        "You are WaltzRL Feedback Agent, a reward modelling specialist that evaluates "
        "agent outputs and provides structured feedback for reinforcement learning training. "
        "For every evaluation you receive, produce a structured report with these sections:\n"
        "1. **Quality Score** (1-10) with a one-line justification\n"
        "2. **Strengths** – 2-3 specific things the output did well\n"
        "3. **Critical Issues** – ranked list of problems with severity (BLOCKING/HIGH/MEDIUM/LOW)\n"
        "4. **Suggested Fix** – concrete, actionable rewrite or correction for the top issue\n"
        "5. **Affected User Segment** – who is most impacted by the failure\n"
        "6. **RL Training Signal** – reward delta recommendation (positive/negative float, e.g. -0.4)\n"
        "Minimum response: 150 words. Never give vague feedback like 'could be improved' — "
        "name the exact flaw and prescribe the exact fix.",
    ),
    "se_darwin_agent": (
        "SE Darwin Agent",
        "You are SE Darwin Agent, a software-engineering evolutionary optimiser that "
        "applies Darwin-inspired improvement cycles to code and architecture. Respond "
        "concisely and stay in character.",
    ),
    "ring1t_reasoning_agent": (
        "Ring1T Reasoning Agent",
        "You are Ring1T Reasoning Agent, an advanced chain-of-thought reasoning specialist "
        "that decomposes complex problems into structured solution paths. Respond concisely "
        "and stay in character.",
    ),
    "business_idea_generator": (
        "Business Idea Generator",
        "You are Business Idea Generator, an innovative ideation agent that creates "
        "novel, market-validated business ideas. You analyse trends and opportunities "
        "to surface high-potential ventures. Respond concisely and stay in character.",
    ),
    # Marketplace agents
    "genesis-ai-vision-api": (
        "Genesis AI Vision API",
        "You are Genesis AI Vision API, an image analysis and computer vision agent "
        "that processes images, extracts information, and generates visual insights. "
        "Respond concisely and stay in character as a computer vision specialist.",
    ),
    "genesis-workflow-automator": (
        "Genesis Workflow Automator",
        "You are Genesis Workflow Automator, a process automation specialist that "
        "designs and implements business workflow automations. When a platform is "
        "not specified, default to n8n and return an importable workflow artifact "
        "instead of asking a follow-up question. Respond concisely.",
    ),
    "genesis-data-pipeline-agent": (
        "Genesis Data Pipeline Agent",
        "You are Genesis Data Pipeline Agent, a data engineering specialist that "
        "designs ETL pipelines and data architectures. Respond concisely.",
    ),
    "unit-test-generator": (
        "Unit Test Generator",
        "You are Unit Test Generator, an AI that writes comprehensive unit tests "
        "for any codebase. Respond concisely and stay in character.",
    ),
    "api-documentation-generator": (
        "API Documentation Generator",
        "You are API Documentation Generator, an AI that produces clear, complete "
        "API documentation from code. Respond concisely and stay in character.",
    ),
    "social-media-scheduler": (
        "Social Media Scheduler",
        "You are Social Media Scheduler, an AI that plans and optimises social media "
        "content calendars. Respond concisely and stay in character.",
    ),
    "web-scraper-pro": (
        "Web Scraper Pro",
        "You are Web Scraper Pro, an AI specialist in web data extraction and "
        "structuring. Respond concisely and stay in character.",
    ),
    "meeting-summarizer": (
        "Meeting Summarizer",
        "You are Meeting Summarizer, an AI that transforms meeting transcripts into "
        "structured, decision-ready summaries. Always produce output in this exact format:\n\n"
        "## Meeting Summary\n"
        "**Date/Context:** [extract from transcript or note 'not specified']\n\n"
        "### Decisions Made\n"
        "- [Decision 1 — one sentence, definitive]\n"
        "- [Decision 2]\n\n"
        "### Action Items\n"
        "| Owner | Action | Due Date |\n"
        "|-------|--------|----------|\n"
        "| [Name] | [Specific task] | [Date or 'TBD'] |\n\n"
        "### Key Discussion Points\n"
        "- [Important point discussed, not decided]\n\n"
        "### Next Steps\n"
        "- [What happens next, in order]\n\n"
        "If the input is not a meeting transcript, ask for one. "
        "Never output a flat paragraph — always use the table/section format above.",
    ),
    "expense-tracker": (
        "Expense Tracker",
        "You are Expense Tracker, an AI finance assistant that categorises expenses "
        "and generates spending reports. Respond concisely and stay in character.",
    ),
    "review-responder": (
        "Review Responder",
        "You are Review Responder, an AI that crafts professional, empathetic responses "
        "to customer reviews. Respond concisely and stay in character.",
    ),
    "onboarding-automation": (
        "Onboarding Automation",
        "You are Onboarding Automation, an AI specialist in designing automated "
        "employee and customer onboarding sequences. Respond concisely and stay in character.",
    ),
    "image-optimizer": (
        "Image Optimizer",
        "You are Image Optimizer, an AI that advises on image compression, format "
        "selection, and performance optimisation strategies. Respond concisely.",
    ),
    "backup-manager": (
        "Backup Manager",
        "You are Backup Manager, an AI that designs robust backup and disaster recovery "
        "strategies for data and infrastructure. Respond concisely.",
    ),
    # CHUNK_2_REGISTRY: reconcile the 3 bundles that existed on disk
    # (skill_bundles/genesis-{domain,maintenance,pricing}.json) but had no
    # catalogued slug. Text below is excerpted verbatim from each bundle's own
    # system_prompt, not invented.
    "genesis_domain": (
        "Genesis Domain Agent",
        "You are the Genesis Domain Agent. You help buyers find, register, and configure "
        "domain names for new businesses. You generate CREATIVE, memorable domain "
        "candidates (10-20 per request) — never obvious keyword-stuffed names like "
        "'bestplumber.com'. Think like a brand strategist: use portmanteaus, invented "
        "words, metaphors, unexpected word pairings, and cross-industry borrowing.",
    ),
    "genesis_maintenance": (
        "Genesis Maintenance Agent",
        "You are a system maintenance and operations specialist. Monitor system health, "
        "perform routine maintenance, optimize performance, manage backups, and ensure "
        "high availability. Track uptime, resource utilization, database health, and "
        "infrastructure costs. Implement proactive monitoring and automated remediation. "
        "Use UptimeRobot API for uptime monitoring.",
    ),
    "genesis_pricing": (
        "Genesis Pricing Agent",
        "You are the Genesis Pricing Agent. You run price-elasticity experiments, "
        "purchase market datasets, deploy pricing updates, and report on pricing "
        "performance for the buyer's business. Your charter is experimentation: identify "
        "the price points most likely to lift revenue, test them, measure them, and "
        "write back results.",
    ),
    "genesis-e4l-revenue": (
        "Genesis E4L Revenue",
        "You are genesis-e4l-revenue. Prove revenue completeness from source through "
        "Xero. Cato is boss. Read/propose only. Host Xero MCP is the Xero path.",
    ),
    "genesis-e4l-shopify": (
        "Genesis E4L Shopify",
        "You are genesis-e4l-shopify. Explain Shopify D2C vs Xero Shopify accounts. "
        "A2X is not live. Cato is boss. Read/propose only.",
    ),
    "genesis-e4l-stripe": (
        "Genesis E4L Stripe",
        "You are genesis-e4l-stripe. Reconcile Stripe to Xero Stripe banks. Cato is "
        "boss. Read/propose only. Host Xero MCP is the Xero path.",
    ),
    "genesis-e4l-cash": (
        "Genesis E4L Cash",
        "You are genesis-e4l-cash. Bank, card, clearing, transfer, FX integrity per "
        "entity. Cato is boss. Read/propose only.",
    ),
    "genesis-e4l-ap": (
        "Genesis E4L AP",
        "You are genesis-e4l-ap. Open bills, coding, aging. Never pay. Cato is boss. "
        "Read/propose only.",
    ),
    "genesis-e4l-ar": (
        "Genesis E4L AR",
        "You are genesis-e4l-ar. Open invoices, unapplied cash, aging. Cato is boss. "
        "Read/propose only.",
    ),
    "genesis-e4l-cogs-cm": (
        "Genesis E4L COGS & CM",
        "You are genesis-e4l-cogs-cm. COGS, royalties, inventory, contribution margin. "
        "Cato is boss. Read/propose only.",
    ),
    "genesis-e4l-commissions": (
        "Genesis E4L Commissions",
        "You are genesis-e4l-commissions. Affiliate and practitioner commissions. "
        "Never pay affiliates. Cato is boss. Read/propose only.",
    ),
    "genesis-e4l-intercompany": (
        "Genesis E4L Intercompany",
        "You are genesis-e4l-intercompany. Match due-to/due-from across the 6 Xero "
        "orgs. Cato is boss. Read/propose only.",
    ),
    "genesis-e4l-close": (
        "Genesis E4L Close",
        "You are genesis-e4l-close. Close checklist owner. Never declare books closed. "
        "Cato is boss. Propose only.",
    ),
    "genesis-e4l-journals": (
        "Genesis E4L Journals",
        "You are genesis-e4l-journals. Prepare balanced draft journals. Never post. "
        "Never plug. Never confirm=True on live orgs. Cato is boss.",
    ),
    "genesis-e4l-fs-integrity": (
        "Genesis E4L FS Integrity",
        "You are genesis-e4l-fs-integrity. Detect economically wrong-but-balancing "
        "books. Cato is boss. Read only.",
    ),
    "genesis-e4l-controller": (
        "Genesis E4L Controller",
        "You are genesis-e4l-controller. Adversarial review. Never silently pick a "
        "side. Cato is boss. Read only.",
    ),
    "genesis-e4l-treasury": (
        "Genesis E4L Treasury",
        "You are genesis-e4l-treasury. Massey Kraken/IB and later investment accounts. "
        "Cato is boss. Read/propose only. Never trade.",
    ),
    # Canonical Master Company OS logical profiles. These remain additive so
    # legacy public slugs continue to resolve exactly as before.
    "genesis-knowledge-evidence": _canonical_persona(
        "Genesis Knowledge & Evidence",
        "Institutional Knowledge & Evidence Analyst",
        "read only; do not invent policy, silently resolve conflicts, alter canon, "
        "write live business systems, or declare operational completion",
    ),
    "genesis-execution-control": _canonical_persona(
        "Genesis Execution Control",
        "Project, Requirement & Execution Control Analyst",
        "recommend state transitions only; do not close tasks without proof, override "
        "owners or approvals, deploy, or mutate Monday, GitHub, or Azure",
    ),
    "genesis-automation-architect": _canonical_persona(
        "Genesis Automation Architect",
        "Automation & Business Process Architect",
        "design only; do not deploy, create credentials, bypass approval, duplicate a "
        "domain control runtime, or make an automation authoritative",
    ),
    "genesis-integration-architect": _canonical_persona(
        "Genesis Integration Architect",
        "Integration, API & Capability Architect",
        "do not expose secrets, broaden OAuth scopes, create hidden backchannels, "
        "bypass a domain runtime, or directly write financial systems",
    ),
    "genesis-platform-engineer": _canonical_persona(
        "Genesis Platform Engineer",
        "Software, Platform & DevOps Engineer",
        "work only on an authorized branch or PR; do not self-approve, claim deployment "
        "from local tests, suppress failures, change canon, or write finance systems",
    ),
    "genesis-reliability": _canonical_persona(
        "Genesis Reliability",
        "Reliability, Incident & Root-Cause Analyst",
        "read and propose by default; do not opportunistically mutate production, delete "
        "evidence, hide a failure, or close an incident from symptom disappearance",
    ),
    "genesis-data-intelligence": _canonical_persona(
        "Genesis Data Intelligence",
        "Data, Analytics & Decision Intelligence Analyst",
        "read only; do not silently redefine KPIs, treat correlation as causation, write "
        "transactions, or expose restricted row-level data",
    ),
    "genesis-security-identity": _canonical_persona(
        "Genesis Security & Identity",
        "Security, Identity & Privacy Analyst",
        "never disclose secret values, elevate privilege, weaken controls, use break-glass "
        "outside policy, or approve your own exception",
    ),
    "genesis-legal-risk": _canonical_persona(
        "Genesis Legal & Risk",
        "Legal, Risk & Compliance Analyst",
        "do not sign, file, send binding notices, make final legal determinations, or "
        "expose privileged material outside the authorized matter",
    ),
    "genesis-commercial-growth": _canonical_persona(
        "Genesis Commercial Growth",
        "Commercial Growth & Customer Intelligence Analyst",
        "do not launch spend, send customer communications, change pricing, make "
        "unsupported claims, or override canon and brand rules",
    ),
    "genesis-people-operations": _canonical_persona(
        "Genesis People Operations",
        "People, Organizational Operations & SOP Analyst",
        "minimize personal data; do not rank employees, infer sensitive traits, make "
        "employment decisions, or expose private HR data",
    ),
    "genesis-canon-steward": _canonical_persona(
        "Genesis Canon Steward",
        "Canon, Policy & Institutional Alignment Steward",
        "do not bless canon, alter canon directly on main, silently rewrite policy, or "
        "treat raw Drive content as automatically superior to approved canon",
    ),
    # Canonical FinanceOS logical profiles. Finance profiles are proposal-only;
    # the deterministic FinanceOS Worker retains every posting/control action.
    "genesis-fin-revenue": _canonical_persona(
        "Genesis Finance Revenue", "Revenue, Subscription & Commerce Accounting",
        "do not invent revenue policy, post invoices or journals, issue refunds, or "
        "recognize revenue while policy is unresolved", finance=True,
    ),
    "genesis-fin-ap": _canonical_persona(
        "Genesis Finance AP", "Accounts Payable & Vendor Accounting",
        "do not approve your own invoice, pay, post, alter vendor bank details, or "
        "bypass missing approvals", finance=True,
    ),
    "genesis-fin-ar": _canonical_persona(
        "Genesis Finance AR", "Accounts Receivable, Billing & Collections",
        "do not forgive debt, issue credit, send legal demands, post receipts, or change "
        "customer terms", finance=True,
    ),
    "genesis-fin-reconciliation": _canonical_persona(
        "Genesis Finance Reconciliation", "Bank, Processor & Cash Reconciliation",
        "do not fabricate matches, hide unresolved variance, move money, or mark an item "
        "reconciled without FinanceOS confirmation", finance=True,
    ),
    "genesis-fin-treasury": _canonical_persona(
        "Genesis Finance Treasury", "Treasury, Liquidity & Cash Forecasting",
        "do not transfer funds, approve payments, access bank credentials, change bank "
        "instructions, or hide forecast uncertainty", finance=True,
    ),
    "genesis-fin-cogs": _canonical_persona(
        "Genesis Finance COGS", "Inventory, Landed Cost, COGS & Contribution Margin",
        "do not change physical inventory, invent allocation policy, post entries, or "
        "bury unexplained valuation differences", finance=True,
    ),
    "genesis-fin-commissions": _canonical_persona(
        "Genesis Finance Commissions", "Commissions, Affiliate & Partner Accounting",
        "require an effective-dated approved plan; do not invent plan terms, pay partners, "
        "alter contracts, or post entries", finance=True,
    ),
    "genesis-fin-intercompany": _canonical_persona(
        "Genesis Finance Intercompany", "Intercompany, Entity & Group Accounting",
        "do not assume ownership or control, merge similarly named entities, offset "
        "unrelated entities, invent tax policy, or post entries", finance=True,
    ),
    "genesis-fin-close": _canonical_persona(
        "Genesis Finance Close", "Close, Journals & Financial Statement Integrity",
        "do not post journals, lock periods, approve your own close, suppress unreconciled "
        "balances, or declare the books closed", finance=True,
    ),
    "genesis-fin-fpa": _canonical_persona(
        "Genesis Finance FP&A", "FP&A, Budgeting, Forecasting & Management Reporting",
        "do not overwrite actuals, silently change assumptions, present forecasts as "
        "booked results, or post journals", finance=True,
    ),
    "genesis-fin-payroll-tax": _canonical_persona(
        "Genesis Finance Payroll & Tax", "Payroll Accounting, Tax & Compliance Coordination",
        "use least-necessary payroll access; do not run payroll, expose payroll detail, "
        "file tax returns, invent tax positions, or post entries", finance=True,
    ),
    "genesis-fin-controller": _canonical_persona(
        "Genesis Finance Controller", "Controller, Accounting Policy & Finance Arbitration",
        "prefer HOLD to unsupported certainty; do not invent policy, self-approve "
        "consequential accounting, post, pay, or declare Xero writes verified", finance=True,
    ),
}

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class NegotiateRequest(BaseModel):
    event: str
    negotiation_id: str
    requester_agent_id: str
    requester_agent_name: str
    responder_agent_id: str
    requested_service: str
    budget: float
    requirements: dict = {}
    notes: Optional[str] = None
    category: Optional[str] = None
    callback_url: str


class NegotiateResponse(BaseModel):
    negotiation_id: str
    responder_agent_id: str
    status: str  # ACCEPTED, REJECTED, COUNTERED
    price: float
    notes: str
    estimatedDelivery: str


class RunRequest(BaseModel):
    prompt: Optional[str] = None
    # Workflows send structured JSON objects; accept dict/list (BUG-01 / HTTP 422).
    input: Optional[Any] = None
    task: Optional[Any] = None
    testContext: Optional[bool] = False
    mode: Optional[str] = None
    require_artifact: Optional[bool] = False
    # External escrow integration: when the marketplace owns the escrow,
    # it passes the escrow id here so the gateway skips its own
    # initiate / complete / release flow. The caller is then responsible
    # for settle/refund based on the response.
    escrow_id: Optional[str] = None
    # For async jobs (job_mode=async): URL the worker will POST results to
    # on DELIVERED / FAILED / EXPIRED so the marketplace can settle/refund.
    callback_url: Optional[str] = None
    # Explicit flag — useful for tests/direct callers that want to skip
    # internal escrow without passing an external id.
    skip_internal_escrow: Optional[bool] = False
    # Optional FinanceOS V5 governed-data envelope. Finance specialists consume
    # present fields only; absent fields are omitted, never invented.
    financeos_context: Optional[Any] = None
    # Optional live-fetch request: {"entity_id": "...", "data_product_id": "...",
    # "division_id": "...", "channel_id": "...", "period_start_from": "...",
    # "period_end_to": "..."}. When present AND financeos_context is absent,
    # Genesis calls FinanceOS's live governed read layer
    # (GET /api/v1/data-products/revenue[/:id]) instead of relying only on a
    # caller-supplied local/test envelope. Ignored (never merged) when
    # financeos_context is also supplied -- see extract_raw_context's
    # "first explicit envelope, do not merge" rule, which this preserves.
    financeos_live_fetch: Optional[Any] = None


class RunResponse(BaseModel):
    response: str
    agentSlug: Optional[str] = None
    agentName: Optional[str] = None


# ---------------------------------------------------------------------------
# LLM router helper
# ---------------------------------------------------------------------------

def _llm_api_key() -> str:
    """Read the LLM API key at call time. Prefers LLM_API_KEY (used when
    routing through SwarmSync). Per-call read lets Render env-var rotations
    take effect without a manual redeploy."""
    for name in ("LLM_API_KEY", "SWARMSYNC_ROUTING_API_KEY", "ROUTING_API_KEY"):
        val = (os.getenv(name) or "").strip()
        if val:
            return val
    return ""


def _llm_api_url() -> str:
    """Read the LLM API endpoint at call time. Defaults to SwarmSync routing."""
    return os.getenv("LLM_API_URL", "https://api.swarmsync.ai/v1/chat/completions")




# Legacy persona path model. Bundle-backed Genesis agents use AgentRuntime's
# SwarmSync model default instead.
FREE_MODEL = os.getenv("LEGACY_PERSONA_MODEL", "minimax/minimax-m2.5")
PERSONA_ROUTER_MODEL = os.getenv("GENESIS_LLM_MODEL", "auto").strip()
PERSONA_PRIMARY_MODEL = PERSONA_ROUTER_MODEL
X402_STUB_MARKER = "x402 HTTP service"
ROUTER_FALLBACK_MODELS = [
    PERSONA_PRIMARY_MODEL,
    "openai/gpt-5-mini",
    "openai/gpt-5.1",
]


def _coerce_prompt_field(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value).strip()


def _build_user_prompt(body: RunRequest) -> str:
    """Combine prompt, input, and task so role-specific tasks are not dropped."""
    prompt = _coerce_prompt_field(body.prompt)
    inp = _coerce_prompt_field(body.input)
    task_raw = body.task
    parts: list[str] = []
    if prompt:
        parts.append(prompt)
    if inp and inp not in parts:
        parts.append(inp)
    if task_raw is not None:
        if isinstance(task_raw, dict):
            task_text = _coerce_prompt_field(
                task_raw.get("description") or task_raw.get("text") or task_raw.get("task") or task_raw
            )
        else:
            task_text = _coerce_prompt_field(task_raw)
        if task_text and task_text not in parts:
            parts.append(f"Task:\n{task_text}")
    return "\n\n".join(parts).strip()


def _is_x402_stub_response(text: str) -> bool:
    return X402_STUB_MARKER in (text or "")


def _prefer_sync_bundle_run(body: RunRequest) -> bool:
    return bool(body.testContext) or (body.mode or "").strip() == "live_test"


def _router_result_payload(router_result: Any) -> tuple[str, dict[str, Any] | None]:
    if isinstance(router_result, dict):
        return (
            str(router_result.get("text") or ""),
            router_result.get("swarmsync") if isinstance(router_result.get("swarmsync"), dict) else None,
        )
    return str(router_result or ""), None


def _runtime_error_status(result: dict[str, Any]) -> int:
    """Map AgentRuntime failure payloads to honest HTTP statuses."""
    error = str(result.get("error", "agent_failure"))
    message = str(result.get("message", ""))
    combined = f"{error} {message}".lower()

    if error == "timeout":
        return 504
    if "429" in combined or "too many requests" in combined or "rate limit" in combined:
        return 429
    if "402" in combined or "credit" in combined or "payment required" in combined:
        return 402
    if error in {"success_criteria_failed"}:
        return 422
    if error in {"tool_not_allowed", "file_write_limit_exceeded", "token_budget_exceeded", "llm_call_limit_exceeded"}:
        return 429
    return 502


def _raise_for_runtime_failure(result: dict[str, Any], slug: str) -> None:
    """Raise when AgentRuntime returns an explicit failure payload.

    Older behavior wrapped these failures inside HTTP 200 `response` strings,
    which made live monitoring and the Genesis evaluator report false success
    at the transport layer.
    """
    if result.get("ok", True):
        return

    status_code = _runtime_error_status(result)
    error = result.get("error", "agent_failure")
    message = str(result.get("message") or result.get("error") or "agent_failure").strip()
    detail: dict[str, Any] = {
        "error": error,
        "message": message or "agent_failure",
        "errorMessage": message or "agent_failure",
        "type": result.get("type"),
        "agentSlug": slug,
    }
    
    # For output_contract_failed, extract validation details from output_contract_eval
    if error == "output_contract_failed":
        eval_data = result.get("output_contract_eval", {})
        if eval_data:
            # Include reason (could be judge_reason or deterministic_reason)
            reason = eval_data.get("reason") or eval_data.get("deterministic_reason") or eval_data.get("judge_reason")
            if reason:
                detail["reason"] = reason
            
            # Include missing_fields if available
            missing_fields = eval_data.get("missing_fields") or eval_data.get("deterministic_missing_fields")
            if missing_fields:
                detail["missing_fields"] = list(missing_fields) if not isinstance(missing_fields, list) else missing_fields
            
            # Include failing_fields if available
            failing_fields = eval_data.get("failing_fields") or eval_data.get("deterministic_failing_fields")
            if failing_fields:
                detail["failing_fields"] = list(failing_fields) if not isinstance(failing_fields, list) else failing_fields
            
            # Include ok status for completeness
            detail["contract_ok"] = eval_data.get("ok", False)
    
    raise HTTPException(
        status_code=status_code,
        detail=detail,
    )

async def call_llm_router(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """Route a persona request through the configured model provider."""
    return await _call_llm_router_inner(system_prompt, user_prompt)

async def _call_llm_router_inner(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """Call SwarmSync /v1/chat/completions; return text + swarmsync routing metadata."""
    api_key = _llm_api_key()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="LLM API key not configured (set LLM_API_KEY)",
        )
    url = _llm_api_url()

    router_headers: dict[str, str] = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://swarmsync.ai",
        "X-Title": "SwarmSync Agent Gateway",
    }
    gateway_secret = os.getenv("AGENT_GATEWAY_SECRET", "").strip()
    if gateway_secret:
        router_headers["X-Agent-Gateway-Secret"] = gateway_secret

    # A router blip (the upstream restarting: 502/503/504, a refused or timed-out connection) is transient, not a
    # decision. Retry with backoff before giving up, so a seller does not turn a few seconds of upstream downtime
    # into a permanent "LLM evaluation unavailable" rejection of a buyer's offer.
    backoffs = [2.0, 6.0, 12.0]
    last_exc: HTTPException | None = None
    models = list(dict.fromkeys(m for m in ROUTER_FALLBACK_MODELS if m))

    attempt_count = 0
    router_down = False
    for model_index, model_id in enumerate(models):
        if router_down:
            break
        for attempt, _ in enumerate([()] * (len(backoffs) + 1)):
            attempt_count += 1
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(
                        url,
                        headers=router_headers,
                        json={
                            "model": model_id,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                            "max_tokens": 2048,
                        },
                    )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = HTTPException(
                    status_code=502,
                    detail=f"LLM router unreachable: {type(exc).__name__}",
                )
                if attempt < len(backoffs):
                    logger.warning(
                        "LLM router unreachable model=%s attempt %d (%s); sleeping %.1fs",
                        model_id,
                        attempt + 1,
                        type(exc).__name__,
                        backoffs[attempt],
                    )
                    await asyncio.sleep(backoffs[attempt])
                    continue
                router_down = True
                break

            if resp.status_code == 429:
                last_exc = HTTPException(
                    status_code=429,
                    detail="LLM router rate limit exceeded. Please retry shortly.",
                )
                if attempt < len(backoffs):
                    logger.warning(
                        "LLM router 429 model=%s attempt %d; sleeping %.1fs",
                        model_id,
                        attempt + 1,
                        backoffs[attempt],
                    )
                    await asyncio.sleep(backoffs[attempt])
                    continue
                break

            if not (200 <= resp.status_code < 300):
                detail_text = resp.text[:500]
                combined = detail_text.lower()
                if resp.status_code in (400, 402) or any(
                    x in combined for x in ("402", "credit", "balance", "quota", "payment")
                ):
                    logger.warning(
                        "LLM router %s for model=%s; trying next model",
                        resp.status_code,
                        model_id,
                    )
                    break
                if resp.status_code in (500, 502, 503, 504):
                    last_exc = HTTPException(
                        status_code=502,
                        detail=f"LLM router returned {resp.status_code}: {detail_text}",
                    )
                    if attempt < len(backoffs):
                        logger.warning(
                            "LLM router %s model=%s attempt %d; sleeping %.1fs",
                            resp.status_code,
                            model_id,
                            attempt + 1,
                            backoffs[attempt],
                        )
                        await asyncio.sleep(backoffs[attempt])
                        continue
                    router_down = True
                    break
                raise HTTPException(
                    status_code=502,
                    detail=f"LLM router returned {resp.status_code}: {detail_text}",
                )

            data = resp.json()
            content = str(data.get("choices", [{}])[0].get("message", {}).get("content") or "")
            if not content.strip():
                last_exc = HTTPException(
                    status_code=502,
                    detail=f"LLM router returned empty content for model={model_id}",
                )
                if attempt < len(backoffs):
                    logger.warning(
                        "LLM router returned empty content for model=%s attempt %d; sleeping %.1fs",
                        model_id,
                        attempt + 1,
                        backoffs[attempt],
                    )
                    await asyncio.sleep(backoffs[attempt])
                    continue
                logger.warning("LLM router returned empty content for model=%s; trying next model", model_id)
                break

            swarmsync = data.get("swarmsync") if isinstance(data.get("swarmsync"), dict) else None
            return {
                "text": content,
                "swarmsync": swarmsync,
                "usage": data.get("usage"),
                "model": data.get("model"),
                "_genesis_observability": {
                    "attempt_count": attempt_count,
                    "retry_count": max(0, attempt_count - 1),
                    "fallback_used": model_index > 0,
                },
            }

    if last_exc:
        raise last_exc
    raise HTTPException(
        status_code=502,
        detail="LLM router failed for all fallback models (check LLM_API_KEY credits)",
    )


async def _run_loaded_agent(agent: Any, user_prompt: str, slug: str) -> Optional[str]:
    """
    Best-effort execution for dynamically loaded agents.

    Tries, in order:
    - agent.run(prompt)
    - agent.execute(prompt)
    - module-level run(prompt)
    - module-level execute(prompt)

    Returns the response text on success, or None to signal the caller to fall
    back to the SwarmSync router persona path.
    """
    import inspect
    import types

    candidates: list[tuple[Any, str]] = []

    # Instance methods on the loaded agent object
    for name in ("run", "execute"):
        if hasattr(agent, name):
            candidates.append((getattr(agent, name), f"{type(agent).__name__}.{name}"))

    # If the "agent" is actually a module, also look for module-level entrypoints
    if isinstance(agent, types.ModuleType):
        for name in ("run", "execute"):
            if hasattr(agent, name):
                candidates.append((getattr(agent, name), f"{agent.__name__}.{name}"))

    for func, label in candidates:
        if not callable(func):
            continue
        try:
            logger.info("Executing loaded agent via %s for slug=%s", label, slug)
            result = func(user_prompt)
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                continue
            if not isinstance(result, str):
                result = str(result)
            return result
        except Exception as exc:
            logger.error(
                "Agent entrypoint %s for slug=%s failed: %s",
                label,
                slug,
                exc,
                exc_info=True,
            )
            continue

    logger.warning(
        "Loaded agent for slug=%s has no usable 'run' or 'execute' entrypoint; falling back to persona",
        slug,
    )
    return None


# ---------------------------------------------------------------------------
# Verification system configuration
# ---------------------------------------------------------------------------

INTERNAL_SECRET = os.getenv("INTERNAL_SECRET", "")
SWARMSYNC_API_URL = os.getenv("SWARMSYNC_API_URL", "https://api.swarmsync.ai")
AGENT_GATEWAY_SECRET = os.getenv("AGENT_GATEWAY_SECRET", "")

if not AGENT_GATEWAY_SECRET:
    logger.warning("AGENT_GATEWAY_SECRET is not set — negotiate callbacks will be sent without authentication")

_GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY")
if not _GATEWAY_API_KEY:
    logger.warning(
        "GATEWAY_API_KEY is not set — protected routes fail closed and lifespan "
        "startup will refuse service"
    )

# Verification jobs are durable: Postgres table genesis_verification_jobs (migration 006), keyed
# (kind, environment, verification_id). The gateway is OOM-killed and redeployed; an in-memory dict lost every
# in-flight verification and SwarmSync never got its callback. Only the set of jobs THIS process is working on
# stays in memory (so a duplicate POST or a recovery sweep never double-runs a live job).
_active_verification_jobs: set[str] = set()

#: Back-off before callback retries 1..3 (so up to four POSTs). Only 5xx / 408 / 429 / network errors retry.
_VERIFY_CALLBACK_RETRY_DELAYS_S: tuple[float, ...] = (2.0, 5.0, 15.0)
#: How often a worker renews the lease on the job it is running (lease length: GENESIS_VERIFY_LEASE_SECONDS, 30s).
_VERIFY_HEARTBEAT_INTERVAL_S = 10.0
_VERIFY_RECOVERY_INTERVAL_DEFAULT_S = 15.0
#: Verification runs/deliveries started by this process. Tracked (not FastAPI BackgroundTasks) so that shutdown can
#: cancel them and hand their leases over instead of waiting for them to finish.
_verification_tasks: set[asyncio.Task] = set()


def _spawn_verification_task(coro: Any) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _verification_tasks.add(task)
    task.add_done_callback(_verification_tasks.discard)
    return task


def _verification_recovery_interval_s() -> float:
    try:
        value = float(os.getenv("GENESIS_VERIFY_RECOVERY_INTERVAL_SECONDS", ""))
    except ValueError:
        return _VERIFY_RECOVERY_INTERVAL_DEFAULT_S
    return value if value > 0 else _VERIFY_RECOVERY_INTERVAL_DEFAULT_S


def reset_in_memory_state_for_tests() -> None:
    """Forget which jobs this process is working on (what a process restart does)."""
    _active_verification_jobs.clear()


def _internal_secret() -> str:
    """Return the current internal callback secret from the environment."""
    return os.getenv("INTERNAL_SECRET", "")


def _require_internal_secret_env(x_internal_secret: str | None) -> "swarmsync_envs.SwarmSyncEnvironment":
    """Authenticate an inbound SwarmSync job (/verify, arbitrage verification) and return *its* environment.

    The presented internal secret identifies which SwarmSync environment is calling; the job's callback is
    then derived from that environment alone (never from a process-global URL).
    """
    if not swarmsync_envs.any_internal_secret_configured():
        raise HTTPException(status_code=503, detail="INTERNAL_SECRET not configured on this service")
    env = swarmsync_envs.resolve_by_internal_secret(x_internal_secret)
    if env is None:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Internal-Secret")
    return env


def _require_internal_secret(x_internal_secret: str | None) -> None:
    """Raise 401 if the X-Internal-Secret header is missing or wrong."""
    expected_secret = _internal_secret()
    if not expected_secret:
        # If not configured, reject all calls — prevents open endpoints in production
        raise HTTPException(status_code=503, detail="INTERNAL_SECRET not configured on this service")
    if x_internal_secret != expected_secret:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Internal-Secret")


def _verification_callback_url(kind: str, env: "swarmsync_envs.SwarmSyncEnvironment", verification_id: str) -> str:
    if kind == verification_job_store.KIND_ARBITRAGE:
        return f"{env.api_url}/internal/arbitrage/verification-results"
    return f"{env.api_url}/conduit/verifications/{verification_id}/callback"


async def _deliver_verification_callback(
    job_id: str,
    kind: str,
    env: "swarmsync_envs.SwarmSyncEnvironment",
    verification_id: str,
    body: dict[str, Any],
) -> bool:
    """POST the stored result to SwarmSync, retrying transport trouble. Returns True once SwarmSync accepted it.

    Retries (2s, 5s, 15s) only on network errors, 5xx, 408 and 429, always with the identical body. A transport
    failure never changes the verdict: the job stays ``callback_pending`` and is re-sent by recovery or by the next
    dispatch of the same verification. Any other 4xx is final (``rejected``) and is not re-sent.
    """
    url = _verification_callback_url(kind, env, verification_id)
    headers = {"Content-Type": "application/json", "X-Internal-Secret": env.internal_secret}
    attempts = len(_VERIFY_CALLBACK_RETRY_DELAYS_S) + 1
    for attempt in range(attempts):
        await asyncio.to_thread(verification_job_store.record_callback_attempt, job_id)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=body, headers=headers)
            if resp.status_code in (200, 201):
                await asyncio.to_thread(
                    verification_job_store.set_callback_state, job_id, verification_job_store.CALLBACK_DELIVERED
                )
                return True
            if resp.status_code < 500 and resp.status_code not in (408, 429):
                logger.error(
                    "Verification callback %s rejected by SwarmSync: %d %s", job_id, resp.status_code, resp.text[:300]
                )
                await asyncio.to_thread(
                    verification_job_store.set_callback_state, job_id, verification_job_store.CALLBACK_REJECTED
                )
                return False
            detail = f"HTTP {resp.status_code} {resp.text[:200]}"
        except httpx.HTTPError as exc:
            detail = f"{type(exc).__name__}: {exc}"
        logger.warning("Verification callback %s attempt %d/%d failed: %s", job_id, attempt + 1, attempts, detail)
        if attempt < attempts - 1:
            await asyncio.sleep(_VERIFY_CALLBACK_RETRY_DELAYS_S[attempt])
    logger.error("Verification callback %s still undelivered after %d attempts; kept as callback_pending", job_id, attempts)
    return False


async def _run_under_lease(job_id: str, work: Any) -> None:
    """Run ``work()`` while renewing this process's lease on the job every _VERIFY_HEARTBEAT_INTERVAL_S.

    If a renewal reports the lease is gone (another worker took the job over), the work is cancelled and nothing more
    is written or sent for it. A renewal that merely fails (database unreachable) is retried at the next beat; if the
    outage outlasts the lease the job is taken over and the next successful renewal reports the loss.
    """
    task = asyncio.current_task()
    lost = False

    async def beat() -> None:
        nonlocal lost
        while True:
            await asyncio.sleep(_VERIFY_HEARTBEAT_INTERVAL_S)
            try:
                alive = await asyncio.to_thread(verification_job_store.heartbeat, job_id)
            except Exception as exc:
                logger.warning("Verification job %s lease renewal failed: %s", job_id, exc)
                continue
            if not alive:
                lost = True
                logger.error("Verification job %s: lease lost to another owner; abandoning this run", job_id)
                task.cancel()
                return

    beater = asyncio.create_task(beat())
    try:
        await work()
    except asyncio.CancelledError:
        if not lost:
            raise
        task.uncancel()
    finally:
        beater.cancel()


async def _execute_verification_job(
    job_id: str,
    kind: str,
    env: "swarmsync_envs.SwarmSyncEnvironment",
    verification_id: str,
    compute: Any,
) -> None:
    """Take the lease, run ``compute``, persist its outcome, deliver the callback.

    Skips a job this process already works on or one another live owner holds. If the lease is lost mid-run the
    outcome is not stored and no callback is sent.
    """
    if job_id in _active_verification_jobs:
        return
    _active_verification_jobs.add(job_id)
    try:
        if await asyncio.to_thread(verification_job_store.claim_for_run, job_id) is None:
            logger.info("Verification job %s not claimed: finished, or leased by another owner", job_id)
            return

        async def work() -> None:
            status, result, error, callback_body = await compute()
            stored = await asyncio.to_thread(
                verification_job_store.store_outcome,
                job_id,
                status=status,
                result=result,
                error=error,
                callback_body=callback_body,
            )
            if not stored:
                logger.error("Verification job %s: lease lost before the result was stored; not sending a callback", job_id)
                return
            await _deliver_verification_callback(job_id, kind, env, verification_id, callback_body)

        await _run_under_lease(job_id, work)
    finally:
        _active_verification_jobs.discard(job_id)


async def _redeliver_verification_job(job: dict[str, Any], env: "swarmsync_envs.SwarmSyncEnvironment") -> None:
    """Re-send the stored result of a finished job whose callback SwarmSync has not accepted (under the lease)."""
    job_id = job["job_id"]
    if job_id in _active_verification_jobs:
        return
    _active_verification_jobs.add(job_id)
    try:
        claimed = await asyncio.to_thread(verification_job_store.claim_for_delivery, job_id)
        if claimed is None:
            logger.info("Verification job %s callback not claimed: delivered, or leased by another owner", job_id)
            return
        await _run_under_lease(
            job_id,
            lambda: _deliver_verification_callback(job_id, claimed["kind"], env, claimed["verification_id"], claimed["callback_body"]),
        )
    finally:
        _active_verification_jobs.discard(job_id)


async def _compute_verify_outcome(job_id: str, request: VerifyRequest) -> tuple[str, Any, Any, dict[str, Any]]:
    try:
        result: VerificationResult = await run_verification(
            session_id=job_id,
            spec=request.spec,
            context=request.context,
        )
    except Exception as exc:
        logger.error("Verification job %s failed: %s", job_id, exc, exc_info=True)
        return "failed", None, str(exc), {
            "passed": False,
            "failureReason": f"Gateway error: {exc}",
            "actionLog": [],
        }
    callback_body = {
        "passed": result.passed,
        "proofHash": result.proof_hash,
        "conduitSessionSig": result.conduit_session_sig,
        "extractedContent": result.extracted_content,
        "failureReason": result.failure_reason,
        "actionLog": [al.model_dump() for al in result.action_log],
        # Conduit proof bundle + engine provenance (NestJS DTO accepts both)
        "proofBundleRef": result.proof_bundle_ref,
        "clientContext": result.client_context,
        # v2 fields — present when Track 1 or Track 2 ran
        "evalResultHash": result.eval_result_hash,
        "verificationTrack": result.verification_track,
        "requestId": result.request_id,
    }
    return "completed", result.model_dump(), None, callback_body


async def _run_and_callback(
    job_id: str,
    request: VerifyRequest,
    env: "swarmsync_envs.SwarmSyncEnvironment | None" = None,
) -> None:
    """
    Background task: run the Conduit verification, persist the outcome, then POST the result back to the
    NestJS callback endpoint (retrying transport failures; see _deliver_verification_callback).
    """
    env = env or swarmsync_envs.default_environment()
    await _execute_verification_job(
        job_id,
        verification_job_store.KIND_VERIFY,
        env,
        request.context.verification_id,
        lambda: _compute_verify_outcome(job_id, request),
    )


def _extract_arbitrage_spec(request: ArbitrageVerificationRequest) -> VerificationSpec:
    payload = request.request_payload or {}
    policy = request.policy or {}

    verification_url = (
        payload.get("verificationUrl")
        or payload.get("deliveryUrl")
        or payload.get("url")
        or policy.get("url")
    )

    if not isinstance(verification_url, str) or not verification_url.strip():
        raise ValueError("Arbitrage verification requires request_payload.verificationUrl or deliveryUrl")

    selector = payload.get("selector") or policy.get("selector")
    expected_content = payload.get("expectedContent") or policy.get("expectedContent")
    fingerprint_delta = payload.get("fingerprintDelta")
    if fingerprint_delta is None:
        fingerprint_delta = policy.get("fingerprintDelta", False)

    expected_hash = payload.get("expectedHash") or policy.get("expectedHash")
    rubric_json = payload.get("rubricJson") or policy.get("rubricJson")
    rubric_hash = payload.get("rubricHash") or policy.get("rubricHash")
    request_id = payload.get("requestId") or request.verification_run_id

    return VerificationSpec(
        url=verification_url.strip(),
        selector=selector if isinstance(selector, str) and selector.strip() else None,
        expectedContent=(
            expected_content if isinstance(expected_content, str) and expected_content.strip() else None
        ),
        fingerprintDelta=bool(fingerprint_delta),
        expected_hash=expected_hash if isinstance(expected_hash, str) and expected_hash.strip() else None,
        rubric_json=rubric_json if isinstance(rubric_json, dict) else None,
        rubric_hash=rubric_hash if isinstance(rubric_hash, str) and rubric_hash.strip() else None,
        request_id=request_id if isinstance(request_id, str) and request_id.strip() else None,
    )


async def _compute_arbitrage_outcome(
    job_id: str, request: ArbitrageVerificationRequest
) -> tuple[str, Any, Any, dict[str, Any]]:
    try:
        spec = _extract_arbitrage_spec(request)
        context = VerificationContext(
            marketplace="SwarmSync.AI",
            purpose="arbitrage_verification",
            escrow_ref=request.transaction_id,
            verification_id=request.verification_run_id,
            request_id=request.verification_run_id,
        )

        result: VerificationResult = await run_verification(
            session_id=job_id,
            spec=spec,
            context=context,
        )

        checks = [
            {
                "name": "conduit_verification",
                "passed": result.passed,
                "track": result.verification_track or 0,
            }
        ]
        if result.eval_result_hash:
            checks.append(
                {
                    "name": "artifact_hash",
                    "passed": result.passed if result.verification_track == 1 else None,
                    "hash": result.eval_result_hash,
                }
            )
        if result.rubric_result:
            checks.append(
                {
                    "name": "rubric_evaluation",
                    "passed": result.rubric_result.get("rubric_pass"),
                    "summary": result.rubric_result,
                }
            )

        callback_body = ArbitrageVerificationResult(
            verificationRunId=request.verification_run_id,
            transactionId=request.transaction_id,
            status="PASSED" if result.passed else "FAILED",
            passed=result.passed,
            score=1.0 if result.passed else 0.0,
            failureReason=result.failure_reason,
            method=request.method,
            checks=checks,
            evidenceUrls=[spec.url],
            resultPayload={
                "proofHash": result.proof_hash,
                "proofBundleRef": result.proof_bundle_ref,
                "conduitSessionSig": result.conduit_session_sig,
                "verificationTrack": result.verification_track,
                "evalResultHash": result.eval_result_hash,
                "rubricResult": result.rubric_result,
                "actionLog": [al.model_dump() for al in result.action_log],
                "extractedContent": result.extracted_content,
                "evidenceUrl": spec.url,
            },
        ).model_dump()
        return "completed", callback_body, None, callback_body
    except Exception as exc:
        logger.error("Arbitrage verification job %s failed: %s", job_id, exc, exc_info=True)
        failed_body = ArbitrageVerificationResult(
            verificationRunId=request.verification_run_id,
            transactionId=request.transaction_id,
            status="ERROR",
            passed=False,
            score=0.0,
            failureReason=str(exc),
            method=request.method,
            checks=[],
            evidenceUrls=[],
            resultPayload={"gatewayError": str(exc)},
        ).model_dump()
        return "failed", failed_body, str(exc), failed_body


async def _run_arbitrage_verification_and_callback(
    job_id: str,
    request: ArbitrageVerificationRequest,
    env: "swarmsync_envs.SwarmSyncEnvironment | None" = None,
) -> None:
    env = env or swarmsync_envs.default_environment()
    await _execute_verification_job(
        job_id,
        verification_job_store.KIND_ARBITRAGE,
        env,
        request.verification_run_id,
        lambda: _compute_arbitrage_outcome(job_id, request),
    )


def _verification_job_environment(job: dict[str, Any]) -> "swarmsync_envs.SwarmSyncEnvironment | None":
    return next((e for e in swarmsync_envs.environments() if e.name == job["environment"]), None)


async def _resume_verification_job(job: dict[str, Any], env: "swarmsync_envs.SwarmSyncEnvironment") -> None:
    """Finish whatever a persisted job still owes: run it, or only re-send its stored callback."""
    if job["status"] in ("pending", "running"):
        if job["kind"] == verification_job_store.KIND_ARBITRAGE:
            await _run_arbitrage_verification_and_callback(
                job["job_id"], ArbitrageVerificationRequest.model_validate(job["request"]), env
            )
        else:
            await _run_and_callback(job["job_id"], VerifyRequest.model_validate(job["request"]), env)
    else:
        await _redeliver_verification_job(job, env)


async def recover_verification_jobs() -> None:
    """Finish every verification nobody is working on. Safe to call repeatedly; returns when done.

    * callbacks SwarmSync never accepted are re-sent first (cheap, no Chromium);
    * runs that were pending/running are re-run, one at a time (the gateway is memory-constrained).
    A job another live owner holds the lease on (for example the old container during a rolling restart) is skipped;
    it becomes claimable the moment its owner releases it or its lease expires. Jobs this process is already working
    on are skipped. A job for an environment that is no longer configured is left untouched.
    """
    jobs = await asyncio.to_thread(verification_job_store.outstanding)
    jobs.sort(key=lambda j: j["status"] in ("pending", "running"))  # deliveries before re-runs
    for job in jobs:
        if job["held_by_other"]:
            logger.info(
                "skipping job %s: leased by %s until %s", job["job_id"], job["owner_id"], job["lease_expires_at"]
            )
            continue
        env = _verification_job_environment(job)
        if env is None:
            logger.error(
                "Verification job %s belongs to unknown environment %s; not recovered", job["job_id"], job["environment"]
            )
            continue
        if job["job_id"] in _active_verification_jobs:
            continue
        logger.info(
            "Recovering verification job %s (%s, status=%s, callback=%s)",
            job["job_id"], job["kind"], job["status"], job["callback_state"],
        )
        await _resume_verification_job(job, env)


async def _verification_recovery_loop() -> None:
    """Boot recovery (first pass, immediately) and then a periodic sweep, so a job whose owner crashed is taken over
    as soon as its lease expires. Every pass is lease-bounded: nothing here can hold a job forever."""
    while True:
        try:
            await recover_verification_jobs()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Verification job recovery pass failed: %s", exc, exc_info=True)
        await asyncio.sleep(_verification_recovery_interval_s())


async def _release_verification_jobs_on_shutdown(recovery_task: "asyncio.Task | None") -> None:
    """Graceful shutdown: stop recovering, cancel our in-flight runs and hand their leases over at once, so the next
    container can take them immediately. A cancelled run stores no result; the job stays running."""
    if recovery_task is not None:
        recovery_task.cancel()
    pending = list(_verification_tasks) + ([recovery_task] if recovery_task is not None else [])
    for task in _verification_tasks:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    try:
        released = await asyncio.to_thread(verification_job_store.release_all_mine)
        if released:
            logger.info("Released %d verification job lease(s) for the next container", released)
    except Exception as exc:
        logger.error("Could not release verification job leases at shutdown (they will expire): %s", exc)


async def _accept_verification_job(
    kind: str,
    env: "swarmsync_envs.SwarmSyncEnvironment",
    verification_id: str,
    request_dump: dict[str, Any],
) -> str:
    """Write-before-ack. Persist (or find) the job, start work only if none is in hand, return the jobId.

    A repeated dispatch of the same (environment, verification_id) returns the existing jobId and never starts a
    second run. If the existing job is unfinished (or its result is undelivered) and this process is not already
    working on it, the work is attempted under the lease: it only proceeds when no other live owner holds the job.
    """
    job, created = await asyncio.to_thread(verification_job_store.claim, kind, env.name, verification_id, request_dump)
    job_id = job["job_id"]
    outstanding = (
        job["status"] in ("pending", "running") or job["callback_state"] == verification_job_store.CALLBACK_PENDING
    )
    if outstanding and job_id not in _active_verification_jobs:
        _spawn_verification_task(_resume_verification_job(job, env))
    return job_id


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# CHUNK_4_RETRIEVAL: E4L Retrieval Contract route (master spec §9, Phase E). Kept in its own
# module (retrieval_route.py) rather than inline here — main.py is already 3,550+ lines.
# Mounted with the same GATEWAY_API_KEY guard every other non-public /agents/* route uses;
# dependency is applied at mount time (not inside retrieval_route.py) to avoid a circular
# import, since verify_gateway_key lives in this module.
from retrieval_route import router as retrieval_router  # noqa: E402

app.include_router(retrieval_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "swarmsync-agent-gateway"}


@app.get("/health/browser")
async def health_browser():
    """Report browser/Patchright/Conduit readiness with detailed diagnostics."""
    cache_dir = os.path.expanduser("~/.cache/ms-playwright")
    chromium_dirs: list[str] = []
    chromium_installed = False
    executable_path = None

    if os.path.isdir(cache_dir):
        for entry in os.scandir(cache_dir):
            if entry.is_dir() and "chromium" in entry.name.lower():
                chromium_dirs.append(entry.name)
                for root, _dirs, files in os.walk(entry.path):
                    for fname in files:
                        if fname in ("chrome", "chromium", "chrome.exe", "chromium.exe", "headless_shell"):
                            executable_path = os.path.join(root, fname)
                            chromium_installed = True
                            break
                    if chromium_installed:
                        break

    # conduit_browser package importability
    conduit_package_importable = False
    conduit_bridge_startable = False
    startup_error = None
    try:
        from conduit_browser import ConduitBridge  # noqa: F401
        conduit_package_importable = True
        try:
            ConduitBridge(session_id="health_check", budget_cents=0)
            conduit_bridge_startable = True
        except Exception as inst_err:
            startup_error = f"ConduitBridge() instantiation failed: {inst_err}"
    except ImportError as imp_err:
        startup_error = f"import conduit_browser failed: {imp_err}"

    # Memory check (Linux /proc/meminfo)
    memory_warning = None
    try:
        with open("/proc/meminfo") as _mf:
            for _line in _mf:
                if _line.startswith("MemAvailable:"):
                    available_kb = int(_line.split()[1])
                    available_mb = available_kb // 1024
                    if available_mb < 300:
                        memory_warning = (
                            f"Only {available_mb} MB RAM available — Chromium needs ~300+ MB; "
                            "OOM likely. Upgrade to Standard 2 GB plan."
                        )
                    break
    except Exception:
        pass

    render_instance_type = (
        os.getenv("RENDER_INSTANCE_TYPE")
        or os.getenv("RENDER_SERVICE_TYPE")
        or "unknown"
    )

    return {
        "chromium_installed": chromium_installed,
        "executable_path": executable_path,
        "chromium_dirs": chromium_dirs,
        "conduit_package_importable": conduit_package_importable,
        "conduit_bridge_startable": conduit_bridge_startable,
        "startup_error": startup_error,
        "memory_warning": memory_warning,
        "render_instance_type": render_instance_type,
        "smoke_test_url": "/health/conduit/smoke",
    }


@app.get("/health/conduit/smoke")
async def health_conduit_smoke():
    """Live Conduit smoke test: launch Chromium, navigate to example.com, read title, close."""
    import time
    start = time.monotonic()
    try:
        from patchright.async_api import async_playwright  # type: ignore[import]
        await ensure_chromium_installed()
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            page = await browser.new_page()
            await page.goto("https://example.com", wait_until="domcontentloaded", timeout=30000)
            title = await page.title()
            await browser.close()
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "ok": True,
            "title": title,
            "elapsed_ms": elapsed_ms,
            "note": "Chromium launched successfully on this Render instance",
        }
    except MemoryError as mem_err:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "ok": False,
            "error": "MemoryError",
            "message": str(mem_err),
            "elapsed_ms": elapsed_ms,
            "recommendation": "Upgrade to Standard 2 GB plan — Chromium requires ~300+ MB free RAM",
        }
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        err_type = type(exc).__name__
        msg = str(exc)
        recommendation = None
        if "chromium" in msg.lower() or "executable" in msg.lower():
            recommendation = "Add 'python -m patchright install chromium' to Render build command"
        elif "memory" in msg.lower() or "oom" in msg.lower() or "killed" in msg.lower():
            recommendation = "Upgrade to Standard 2 GB plan — Chromium OOM on current instance"
        return {
            "ok": False,
            "error": err_type,
            "message": msg,
            "elapsed_ms": elapsed_ms,
            "recommendation": recommendation,
        }


@app.get("/health/worker")
async def health_worker():
    """Report Genesis job worker status."""
    try:
        from worker import _worker_state
        state = dict(_worker_state)
        # Convert timestamp to ISO string if present
        if state.get("last_tick_at") is not None:
            import datetime as _dt
            state["last_tick_at"] = _dt.datetime.fromtimestamp(
                state["last_tick_at"], tz=_dt.timezone.utc
            ).isoformat()
    except Exception:
        state = {"enabled": False, "error": "worker_module_unavailable"}

    queue_depth = 0
    stale_count = 0
    if _JOB_STORE_OK:
        try:
            from job_store import _conn
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM genesis_jobs WHERE status = 'QUEUED'")
                row = cur.fetchone()
                queue_depth = int(row["n"]) if row else 0
                cur.execute(
                    "SELECT COUNT(*) AS n FROM genesis_jobs WHERE status = 'RUNNING' "
                    "AND (\"lastHeartbeatAt\" IS NULL OR \"lastHeartbeatAt\" < NOW() - INTERVAL '5 minutes')"
                )
                row = cur.fetchone()
                stale_count = int(row["n"]) if row else 0
        except Exception as e:
            logger.warning("health_worker queue query failed: %s", e)

    from runtime.worker_topology import worker_topology

    topology = worker_topology()
    return {
        **state,
        "queue_depth": queue_depth,
        "stale_job_count": stale_count,
        **topology,
        "last_error": state.get("last_error"),
        "commit": os.getenv("RENDER_GIT_COMMIT") or os.getenv("GENESIS_COMMIT"),
    }


@app.get("/health/sandbox")
async def health_sandbox():
    """Report the active shell sandbox isolation tier (Phase 2).

    isolation="bwrap" means real kernel isolation (filesystem + network
    namespaces). isolation="process" means the hardened process sandbox with
    static escape guards (no kernel FS namespace — bwrap not installed).
    """
    try:
        from runtime.sandbox_manager import sandbox_readiness, _ALLOW_NETWORK
        readiness = sandbox_readiness()
        return {
            "ok": readiness["available"],
            "ready": readiness["available"],
            "status": readiness["status"],
            "isolation": "bwrap" if readiness["available"] else "unavailable",
            "bwrap_available": readiness["available"],
            "network_allowed": _ALLOW_NETWORK,
            "reason": readiness["evidence"]["reason"],
            "note": "run_code is callable only when a functional bwrap boundary is present.",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/agents/jobs/{job_id}/artifacts")
async def get_job_artifacts(job_id: str, principal=Depends(verify_continuation_principal)):
    """List artifacts for a completed job.

    Prefers the durable genesis_artifacts metadata (sha256 / size / mime /
    signed_url) recorded at upload time; refreshes signed URLs and falls back to
    a live storage listing if no durable rows exist.
    """
    from artifact_store import list_artifacts, get_signed_url
    if not _JOB_STORE_OK or get_job is None:
        raise HTTPException(status_code=503, detail="job_store unavailable")
    _require_owned_job(principal, get_job(job_id), "artifact.read")

    # Phase 4: durable metadata path (includes integrity checksums).
    try:
        import durable_store
        rows = durable_store.artifacts_by_job(job_id)
    except Exception:
        rows = []
    if rows:
        artifacts = []
        for r in rows:
            name = r.get("path") or r.get("filename")
            fresh = get_signed_url(job_id=job_id, name=name)
            artifacts.append({
                "name": name,
                "filename": r.get("filename"),
                "size_bytes": int(r["sizeBytes"]) if r.get("sizeBytes") is not None else None,
                "sha256": r.get("sha256"),
                "mime_type": r.get("mimeType"),
                "backend": r.get("storageBackend"),
                "uri": r.get("uri"),
                "signed_url": fresh.get("signed_url") or r.get("signedUrl"),
                "expires_in_seconds": fresh.get("expires_in_seconds"),
                "non_durable": r.get("storageBackend") == "local",
                "created_at": r["createdAt"].isoformat() if r.get("createdAt") and hasattr(r["createdAt"], "isoformat") else r.get("createdAt"),
            })
        return {"job_id": job_id, "backend": rows[0].get("storageBackend"),
                "artifacts": artifacts, "count": len(artifacts), "source": "durable"}

    result = list_artifacts(job_id=job_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "artifacts_not_found"))

    items = result.get("items", [])
    enriched = []
    for item in items:
        name = item.get("name", "")
        url_result = get_signed_url(job_id=job_id, name=name)
        enriched.append({
            "name": name,
            "size_bytes": item.get("size", 0),
            "signed_url": url_result.get("signed_url"),
            "expires_in_seconds": url_result.get("expires_in_seconds"),
            "backend": result.get("backend"),
            "non_durable": result.get("backend") == "local",
        })

    return {
        "job_id": job_id,
        "backend": result.get("backend"),
        "artifacts": enriched,
        "count": len(enriched),
    }


@app.get("/agents/{slug}/capabilities")
async def agent_capabilities(slug: str):
    """Return detailed capability and status metadata for an agent."""
    _require_canonical_agent(slug)
    from capability_cards import card_for
    from tools import get_tool, _TOOL_SCHEMAS
    from runtime.tool_policy import is_tool_allowed
    from tools.sandbox_tool import platform_tool_readiness

    try:
        from bundle_loader import load_bundle
        bundle = load_bundle(slug)
    except Exception:
        bundle = None

    if bundle is None:
        raise HTTPException(status_code=404, detail="agent not found")

    tools_advertised = bundle.get("tools_advertised", [])
    tool_verification = {}
    readiness = platform_tool_readiness()
    for t in tools_advertised:
        registered = get_tool(t) is not None
        has_schema = t in _TOOL_SCHEMAS
        dependency = readiness.get(t)
        tool_verification[t] = {
            "registered": registered,
            "has_schema": has_schema,
            "policy_callable": is_tool_allowed(slug, t),
            "dependency_ready": dependency["available"] if dependency else None,
            "live_verified": False,
        }

    all_registered = all(
        v["registered"] and v["has_schema"] for v in tool_verification.values()
    )

    runtime_level = bundle.get("runtime_level", "skill_bundle")
    artifact_support = "file_write" in tools_advertised
    browser_required = "conduit" in tools_advertised or bundle.get("browser_required", False)

    card = card_for(slug)

    return {
        "slug": slug,
        "name": bundle.get("name"),
        "runtime_level": runtime_level,
        "tool_schemas_registered": all_registered,
        "tools_live_verified": False,
        "tool_verification": tool_verification,
        "artifact_support": artifact_support,
        "browser_required": browser_required,
        "job_mode": bundle.get("job_mode", "sync"),
        "is_orchestrator": bundle.get("is_orchestrator", False),
        "eval_pass_rate": bundle.get("eval_pass_rate"),
        "last_verified_at": bundle.get("last_verified_at"),
        "pricing": card.get("pricing") if card else None,
        "reputation": card.get("reputation") if card else None,
    }


@app.get("/agents")
async def list_agents():
    from canonical_profiles import canonical_agent_inventory
    return {
        "agents": list(canonical_agent_inventory())
    }


async def _resolve_financeos_governed_context(canonical_slug: str, body: "RunRequest"):
    """Resolve the FinanceOS V5 governed-context envelope for a finance
    specialist run, then bind it through the one shared validator.

    Precedence, deliberately NOT a merge (matches `extract_raw_context`'s own
    "first explicit envelope" rule): an explicit `financeos_context` from the
    pre-existing local/test path always wins outright. `financeos_live_fetch`
    only runs FinanceOS's live governed read layer when no explicit envelope
    was supplied AND the caller opted in. Both paths terminate in the exact
    same `bind_financeos_governed_context` call -- there is no second, looser
    gate for live data.
    """
    raw_context = extract_raw_context(body)
    live_request = getattr(body, "financeos_live_fetch", None)
    if raw_context is None and isinstance(live_request, Mapping) and live_request:
        raw_context = await fetch_live_financeos_context(
            entity_id=live_request.get("entity_id"),
            data_product_id=live_request.get("data_product_id"),
            division_id=live_request.get("division_id"),
            channel_id=live_request.get("channel_id"),
            period_start_from=live_request.get("period_start_from"),
            period_end_to=live_request.get("period_end_to"),
        )
    return bind_financeos_governed_context(canonical_slug, raw_context)


# Standing footer appended to every canonical contract correction (phase 7):
# a retry triggered by ANY deterministic reason can regenerate a candidate
# that newly omits the clarification key, so the clarification representation
# requirement is restated unconditionally instead of only in the per-reason
# hint for provider_clarification_representation_invalid.
_CLARIFICATION_REPRESENTATION_FOOTER = (
    "Standing requirement for every resubmission: include the key "
    "clarification exactly once with value null and every other required "
    "field; never emit clarification_needed, candidate_resolution, or any "
    "other flat clarification field."
)


@app.post("/agents/{slug}/run", response_model=RunResponse)
async def run_agent(slug: str, body: RunRequest, principal=Depends(verify_agent_principal)):
    import library_agents

    if library_agents.is_library_slug(slug):
        user_prompt = _build_user_prompt(body)
        if not user_prompt:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "prompt_required",
                    "message": "prompt_required",
                    "errorMessage": "prompt_required",
                },
            )
        raw = await call_llm_router(library_agents.system_prompt(slug), user_prompt)
        text, _ = _router_result_payload(raw)
        return RunResponse(
            response=text,
            agentSlug=slug,
            agentName=library_agents.display_name(slug),
        )
    canonical_slug = _require_canonical_agent(slug)
    from bundle_loader import load_bundle
    canonical_bundle = load_bundle(canonical_slug)
    if canonical_bundle is None:  # startup/source self-check should make this unreachable
        raise HTTPException(status_code=503, detail="canonical_bundle_unavailable")
    display_name = canonical_bundle["name"]
    system_prompt = canonical_bundle["system_prompt"]

    user_prompt = _build_user_prompt(body)
    if not user_prompt:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "prompt_required",
                "message": "prompt_required",
                "errorMessage": "prompt_required",
            },
        )
    if canonical_slug in FINANCE_AGENT_IDS:
        governed = await _resolve_financeos_governed_context(canonical_slug, body)
        if not governed.ok:
            raise HTTPException(
                status_code=422,
                detail={"error": governed.reason or "financeos_governed_context_invalid"},
            )
        if governed.prompt_block:
            user_prompt = (
                f"{user_prompt}\n\n{governed.prompt_block}" if user_prompt else governed.prompt_block
            )
    bundle_slug = slug
    try:
        from bundle_loader import resolve_bundle_slug

        bundle_slug = resolve_bundle_slug(slug)
    except Exception:
        bundle_slug = slug
    # Structured capability constraints are authorization inputs, not prose.
    # Refuse prohibited names before bundle loading or any LLM call.
    structured_task = body.task if isinstance(body.task, dict) else {}
    constraints = structured_task.get("constraints") if isinstance(structured_task, dict) else {}
    requested_tools = constraints.get("allowed_tools", []) if isinstance(constraints, dict) else []
    if isinstance(requested_tools, list):
        from runtime.tool_policy import is_prohibited
        prohibited = sorted(str(name) for name in requested_tools if is_prohibited(str(name), bundle_slug))
        if prohibited:
            raise HTTPException(status_code=403, detail={"error": "prohibited_capability", "tools": prohibited})

    # Persona entries are discovery copy, not executable security principals.
    # Every runnable slug must resolve to a reviewed bundle so it cannot bypass
    # the runtime's tool allowlist, budgets, escrow guard, and evidence checks.
    if not _RUNTIME_IMPORT_OK or load_bundle is None:
        raise HTTPException(status_code=503, detail="bundle_runtime_unavailable")
    try:
        executable_bundle = load_bundle(bundle_slug)
    except Exception as exc:
        logger.error("load_bundle failed for slug=%s: %s", slug, exc, exc_info=True)
        raise HTTPException(status_code=503, detail="bundle_runtime_unavailable") from exc
    if executable_bundle is None:
        raise HTTPException(status_code=404, detail="agent_bundle_required")

    # External-escrow mode: when the marketplace (or any external coordinator)
    # is the escrow owner, the gateway must skip its OWN escrow init / complete /
    # release path. The caller settles or refunds via swarmsync-api directly.
    external_escrow_id = body.escrow_id
    skip_internal_escrow = bool(external_escrow_id) or bool(body.skip_internal_escrow)
    if external_escrow_id:
        logger.info(
            "run_agent slug=%s external escrow_id=%s — skipping internal escrow logic",
            slug,
            external_escrow_id,
        )

    logger.info(
        "run_agent slug=%s testContext=%s prompt_len=%d skip_internal_escrow=%s",
        slug,
        body.testContext,
        len(user_prompt),
        skip_internal_escrow,
    )

    # ------------------------------------------------------------------
    # 0) Phase 2 — consolidated agent runtime backed by skill bundles
    # ------------------------------------------------------------------
    # If a skill bundle exists for this slug, route through the multi-turn
    # AgentRuntime (system prompt + tool whitelist from the bundle). This
    # supersedes the legacy persona path for any slug that has a bundle.
    if _RUNTIME_IMPORT_OK and load_bundle is not None:
        try:
            bundle = load_bundle(bundle_slug)
        except Exception as exc:  # pragma: no cover - ultra-defensive
            logger.error("load_bundle failed for slug=%s: %s", slug, exc, exc_info=True)
            bundle = None

        if bundle is not None:
            bundle_name = bundle.get("name", display_name)

            # Live-test / testContext probes should not spin AgentRuntime for any
            # agent — doing so triggers ConduitBridge (Patchright browser) startup
            # which takes 35-50s on Render free tier, exceeding the 30s proxy
            # timeout. Route all live_test / testContext calls through the fast
            # persona LLM path instead of the full AgentRuntime.
            if _prefer_sync_bundle_run(body):
                logger.info(
                    "run_agent slug=%s live_test/testContext — bypassing AgentRuntime, using persona router",
                    slug,
                )
                display_name = str(bundle_name)
                system_prompt = str(bundle.get("system_prompt") or system_prompt)
                from accounting.specialists import is_e4l_specialist

                live_slug = str(bundle.get("slug") or bundle_slug)
                if is_e4l_specialist(live_slug):
                    try:
                        from accounting.runtime_context import enrich_system_prompt

                        live_params = body.task if isinstance(body.task, dict) else {}
                        system_prompt = enrich_system_prompt(
                            system_prompt, live_slug, live_params, user_prompt
                        )
                    except Exception:
                        logger.exception("accounting entity pack load failed for live_test slug=%s", slug)
                        raise HTTPException(status_code=500, detail="accounting_entity_load_failed")
                bundle = None

        if bundle is not None:
            # Phase 4 — async / long-running orchestrators and conduit-heavy
            # agents (Builder, Deploy, QA, Research, Meta) get persisted via
            # job_store.create_job and a job_id is returned immediately. The
            # worker picks it up, keeping Render's proxy out of the browser
            # startup path.
            if bundle.get("job_mode") == "async" and not _prefer_sync_bundle_run(body):
                if not _JOB_STORE_OK or create_job is None:
                    raise HTTPException(status_code=503, detail="job_store_unavailable")

                async_params = _scoped_runtime_params(body, principal)
                async_prompt = user_prompt or (
                    json.dumps(body.task) if isinstance(body.task, dict) else ""
                )

                # Phase 6 — if buyer + price are supplied, escrow funds before
                # we hand the job to the worker. Agent wallet comes from the
                # bundle (`wallet_id`). Free-tier (Cato) callers omit these.
                price_tier_cents = async_params.get("price_tier_cents") or bundle.get("price_tier_default_cents")
                buyer_wallet_id = async_params.get("buyer_wallet_id")
                agent_wallet_id = bundle.get("wallet_id")

                escrow_id_async: str | None = None
                # If the caller passed an external escrow id, use it as the
                # job's escrow reference and skip internal escrow init.
                # We still persist it in genesis_jobs.escrowId so the worker
                # can echo it back in the callback payload.
                if external_escrow_id:
                    escrow_id_async = external_escrow_id
                # ESCROW BLOCKER 1/11 — escrow_client.initiate_escrow.
                # PERMANENTLY_PROHIBITED (contract 3.8 / 6.1 item 16). Unreachable
                # unless GENESIS_DEPLOYMENT_PROFILE=swarmsync-marketplace, which
                # leaves _ESCROW_OK False. Quarantined rather than deleted: this
                # is SwarmSync marketplace behaviour that Genesis does not own.
                elif (
                    _ESCROW_OK
                    and not skip_internal_escrow
                    and price_tier_cents
                    and buyer_wallet_id
                    and agent_wallet_id
                ):
                    init = await initiate_escrow(
                        source_wallet_id=buyer_wallet_id,
                        destination_wallet_id=agent_wallet_id,
                        amount_cents=int(price_tier_cents),
                        memo=f"Genesis {slug}",
                        metadata={"slug": slug},
                    )
                    if not init.get("ok"):
                        raise HTTPException(
                            status_code=402,
                            detail=f"escrow_initiate_failed: {init.get('error')}",
                        )
                    escrow_id_async = init.get("escrow_id")

                try:
                    if body.callback_url:
                        _assert_not_ssrf(body.callback_url)
                    job = create_job(
                        agent_slug=slug,
                        prompt=async_prompt,
                        params=async_params,
                        price_tier_cents=int(price_tier_cents) if price_tier_cents else None,
                        buyer_wallet_id=buyer_wallet_id,
                        escrow_id=escrow_id_async,
                        webhook_url=body.callback_url,
                        tenant_id=principal.tenant_id,
                        owner_principal_id=principal.principal_id,
                    )
                except Exception as exc:
                    # If WE reserved the escrow (not the marketplace) and we
                    # failed to enqueue, refund. When external_escrow_id is set,
                    # the marketplace owns the refund decision.
                    # ESCROW BLOCKER 2/11 — escrow_client.release_escrow (refund on
                    # enqueue failure). PERMANENTLY_PROHIBITED (6.1 item 18): a
                    # refund is still automation moving money. Unreachable while
                    # _ESCROW_OK is False. Quarantined, not deleted.
                    if escrow_id_async and _ESCROW_OK and not external_escrow_id:
                        try:
                            await release_escrow(
                                escrow_id=escrow_id_async,
                                reason="job_create_failed",
                            )
                        except Exception:
                            logger.exception("escrow release after job_create_failed raised")
                    raise HTTPException(status_code=500, detail=f"job_create_failed: {exc}")

                queued_payload = {
                    "status": job.get("status", "QUEUED"),
                    "slug": bundle.get("slug", bundle_slug),
                    "job_id": job["id"],
                    "poll_url": f"/agents/jobs/{job['id']}",
                    "idempotent_hit": job.get("idempotent_hit", False),
                }
                if principal.auth_method == "ap2":
                    from runtime.request_auth import issue_principal_token
                    queued_payload["principal_token"] = issue_principal_token(principal)
                if escrow_id_async:
                    queued_payload["escrow_id"] = escrow_id_async
                if external_escrow_id:
                    queued_payload["external_escrow_id"] = external_escrow_id
                return RunResponse(
                    response=json.dumps(queued_payload),
                    agentSlug=slug,
                    agentName=bundle_name,
                )

            runtime = _get_runtime()
            if runtime is not None:
                params = _scoped_runtime_params(body, principal)

                # Phase 6 — escrow buyer funds before sync execution if a
                # buyer wallet + price are supplied AND the bundle declares
                # an agent wallet. Free-tier callers (Cato) skip this.
                sync_price_cents = params.get("price_tier_cents") if isinstance(params, dict) else None
                sync_buyer_wallet = params.get("buyer_wallet_id") if isinstance(params, dict) else None
                sync_agent_wallet = bundle.get("wallet_id")

                escrow_id_sync: str | None = None
                # External-escrow mode: the marketplace already initiated the
                # escrow. We do NOT call initiate / complete / release ourselves.
                # The caller settles or refunds based on the response body.
                # ESCROW BLOCKER 3/11 — escrow_client.initiate_escrow (sync path).
                # PERMANENTLY_PROHIBITED (6.1 item 16). Unreachable while
                # _ESCROW_OK is False. Quarantined, not deleted.
                if (
                    _ESCROW_OK
                    and not skip_internal_escrow
                    and sync_price_cents
                    and sync_buyer_wallet
                    and sync_agent_wallet
                ):
                    init_sync = await initiate_escrow(
                        source_wallet_id=sync_buyer_wallet,
                        destination_wallet_id=sync_agent_wallet,
                        amount_cents=int(sync_price_cents),
                        memo=f"Genesis {slug}",
                        metadata={"slug": slug},
                    )
                    if not init_sync.get("ok"):
                        raise HTTPException(
                            status_code=402,
                            detail=f"escrow_initiate_failed: {init_sync.get('error')}",
                        )
                    escrow_id_sync = init_sync.get("escrow_id")

                try:
                    try:
                        result = await runtime.execute_agent(bundle_slug, user_prompt, params)
                    except Exception as inner_exc:
                        # ESCROW BLOCKER 4/11 — escrow_client.release_escrow
                        # (refund on runtime exception). PERMANENTLY_PROHIBITED
                        # (6.1 item 18). Unreachable while _ESCROW_OK is False.
                        if escrow_id_sync and _ESCROW_OK and not external_escrow_id:
                            try:
                                await release_escrow(
                                    escrow_id=escrow_id_sync,
                                    reason=f"runtime_exception:{type(inner_exc).__name__}",
                                )
                            except Exception:
                                logger.exception("escrow release after runtime raise failed")
                        raise

                    # Escrow finalization after runtime returns — only when WE
                    # own the escrow. When external_escrow_id is set, the
                    # marketplace API performs settle/refund itself.
                    # ESCROW BLOCKERS 5/11, 6/11 and 7/11 —
                    #   escrow_client.complete_escrow  (6.1 item 17) settles funds
                    #   escrow_client.calculate_split  (6.1 item 19) constructs the
                    #     disbursement amount; also truncates with
                    #     int(total_cents * pct), losing a minor unit on every
                    #     split where the fee is not a whole number of cents
                    #   escrow_client.release_escrow   (6.1 item 18) refunds
                    # All PERMANENTLY_PROHIBITED and unreachable while _ESCROW_OK
                    # is False. Quarantined, not deleted: settle/refund is
                    # SwarmSync marketplace behaviour Genesis does not own.
                    if escrow_id_sync and _ESCROW_OK and isinstance(result, dict):
                        if result.get("ok"):
                            comp = await complete_escrow(
                                escrow_id=escrow_id_sync,
                                status="SETTLED",
                            )
                            result["escrow"] = {
                                "escrow_id": escrow_id_sync,
                                "status": "SETTLED",
                                "split": calculate_split(int(sync_price_cents)),
                                "settle_response_ok": comp.get("ok", False),
                            }
                        else:
                            rel = await release_escrow(
                                escrow_id=escrow_id_sync,
                                reason=str(result.get("error", "agent_failure")),
                            )
                            result["escrow"] = {
                                "escrow_id": escrow_id_sync,
                                "status": "REFUNDED",
                                "release_response_ok": rel.get("ok", False),
                            }

                    # Echo the external escrow id back so the marketplace can
                    # correlate the response with its own escrow record.
                    if external_escrow_id and isinstance(result, dict):
                        result["external_escrow_id"] = external_escrow_id

                    if isinstance(result, dict):
                        _raise_for_runtime_failure(result, slug)

                    return RunResponse(
                        response=json.dumps(result),
                        agentSlug=slug,
                        agentName=bundle_name,
                    )
                except HTTPException:
                    raise
                except Exception as exc:  # pragma: no cover - ultra-defensive
                    logger.error(
                        "canonical AgentRuntime failed slug=%s type=%s",
                        slug, type(exc).__name__,
                    )
                    raise HTTPException(
                        status_code=502,
                        detail={"error": "agent_runtime_failed", "reason": "canonical_candidate_transport_failed"},
                    ) from None

    # ------------------------------------------------------------------
    # 1) Canonical live-test/persona path: the same exact forced Haiku wire.
    # ------------------------------------------------------------------
    llm_url = _llm_api_url()
    llm_key = _llm_api_key()
    declared_provider = os.getenv("GENESIS_LLM_PROVIDER")
    transport_name = TRANSPORT_NATIVE

    class _ContractRejection(Exception):
        """A fail-closed contract-stage rejection (never a transport failure).

        Carries the exact HTTP 502 detail the caller would have received, so
        the pipeline can be retried once with validator feedback before the
        rejection is surfaced. The gate itself is unchanged: a result is only
        returned after submission, judge, deterministic, and immutability
        checks all pass.
        """

        def __init__(self, stage: str, detail: dict[str, Any]) -> None:
            super().__init__(stage)
            self.stage = stage
            self.detail = detail

    def _contract_correction(rejection: "_ContractRejection") -> str:
        reason = str(rejection.detail.get("reason") or "unknown")
        fields = rejection.detail.get("failing_fields") or []
        field_text = ", ".join(str(f) for f in fields) if fields else "a narrative field"
        hints = {
            "finance_execution_claim": (
                "The validator rejects narrative that asserts or directs "
                "finance execution (a finance action verb -- pay, post, issue, "
                "execute, run, file, submit, transfer, move, refund, approve "
                "-- joined to a finance object -- payment, invoice, journal, "
                "bill, funds, refund, payroll, tax filing, Xero) when it is "
                "not framed as a proposal, recommendation, or plan step, and "
                "it rejects every immediacy directive ('now', 'immediately', "
                "'do not wait'). Frame each action step explicitly as a "
                "recommendation or as a step of a stated plan with no "
                "immediacy wording, or use nouns only with no action verb "
                "when an item must mention a finance object (safe pattern: "
                "'UNKNOWN: Invoice and payment terms absent from the "
                "request.' / 'NEXT: Extraction fee estimate, for requester "
                "review.'). Never state that a finance action occurred."
            ),
            "real_world_completion_claim": (
                "Do not state or imply that any posting, payment, filing, "
                "approval, verification, or other external action occurred or "
                "was completed."
            ),
            "private_durable_memory": (
                "Never propose storing or retaining anything in private or "
                "durable memory, ledgers, or databases."
            ),
            "direct_human_contact": (
                "Never instruct anyone to contact, message, email, or notify "
                "a person directly; route clarification through Cato's "
                "Clarification Engine."
            ),
            "unsupported_human_question": (
                "State unknowns as concise declarative missing-input facts, "
                "not as broad questions directed at a human."
            ),
            "provider_clarification_representation_invalid": (
                "The previous submission used an invalid clarification "
                "representation: it omitted required keys from the result "
                "object (including the clarification key), so the "
                "submission could not be projected. Resubmit exactly one "
                "complete result object with every required field present, "
                "including the key clarification exactly once with value "
                "null, and never emit clarification_needed, "
                "candidate_resolution, or any other flat clarification "
                "field; put gaps, ambiguity, and questions for a human in "
                "unknowns, assumptions, or requested_human_decisions "
                "instead."
            ),
        }
        hint = hints.get(
            reason,
            "Resubmit a fully compliant Genesis V5 result: prefix every "
            "narrative string and array item with its exact intent prefix, "
            "keep every required field, and keep all narrative analysis-only.",
        )
        return (
            "CORRECTION: your previous result submission was rejected by the "
            f"deterministic Genesis V5 contract validator (reason: {reason}; "
            f"field: {field_text}). {hint} Submit exactly one complete, "
            f"compliant V5 result tool call. {_CLARIFICATION_REPRESENTATION_FOOTER}"
        )

    async def _canonical_pipeline(extra_system: str | None):
        """One candidate -> submission -> judge -> deterministic pass.

        Transport failures raise HTTPException exactly as before. Contract-
        stage rejections raise _ContractRejection with the exact 502 detail.
        """
        nonlocal transport_name
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        if extra_system:
            messages.append({"role": "system", "content": extra_system})
        try:
            try:
                require_canonical_native_anthropic_config(
                    api_url=llm_url,
                    api_key=llm_key,
                    declared_provider=declared_provider,
                )
                native_ready = True
            except RuntimeError:
                native_ready = False
            if native_ready:
                router_result = await call_exact_anthropic_candidate(
                    api_url=llm_url,
                    api_key=llm_key,
                    slug=canonical_slug,
                    messages=messages,
                    max_tokens=4096,
                    declared_provider=declared_provider,
                )
            elif router_fallback_allowed(
                api_url=llm_url, api_key=llm_key, declared_provider=declared_provider,
            ):
                transport_name = "swarmsync-router"
                router_result = await call_router_canonical_candidate(
                    api_url=llm_url,
                    api_key=llm_key,
                    slug=canonical_slug,
                    messages=messages,
                    max_tokens=4096,
                )
            else:
                raise config_rejected(REASON_CANDIDATE_TRANSPORT_FAILED)
        except ExactAnthropicTransportError as exc:
            logger.error("canonical candidate phase failed stage=%s", exc.stage)
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "output_contract_failed",
                    "reason": str(exc) or REASON_CANDIDATE_TRANSPORT_FAILED,
                    "message": str(exc) or REASON_CANDIDATE_TRANSPORT_FAILED,
                    "errorMessage": str(exc) or REASON_CANDIDATE_TRANSPORT_FAILED,
                    "stage": exc.stage,
                    "elapsed_ms": exc.elapsed_ms,
                    "http_status_class": exc.http_status_class,
                },
            ) from None
        except Exception as exc:
            logger.error("canonical candidate phase failed type=%s", type(exc).__name__)
            raise HTTPException(
                status_code=502,
                detail={"error": "output_contract_failed", "reason": "canonical_candidate_transport_failed"},
            ) from None
        submission = consume_canonical_result_submission(canonical_slug, router_result)
        if not submission.ok or submission.value is None:
            raise _ContractRejection(
                "submission",
                {"error": "output_contract_failed", "reason": submission.reason},
            )
        candidate_value = submission.value
        candidate_sha_before = canonical_sha256(candidate_value)

        async def _judge_transport(request: dict[str, Any]) -> dict[str, Any]:
            try:
                require_canonical_native_anthropic_config(
                    api_url=llm_url, api_key=llm_key, declared_provider=declared_provider,
                )
                native_judge = True
            except RuntimeError:
                native_judge = False
            if native_judge:
                return await call_exact_anthropic_judge(
                    request, api_url=llm_url, api_key=llm_key
                )
            if router_fallback_allowed(
                api_url=llm_url, api_key=llm_key, declared_provider=declared_provider,
            ):
                return await call_router_judge(request, api_url=llm_url, api_key=llm_key)
            raise config_rejected("semantic_judge_transport_failed")

        judgment = await judge_candidate_semantics(
            candidate_value, transport=_judge_transport
        )
        deterministic = None
        deterministic_reason = None
        deterministic_failing_fields: tuple[str, ...] = ()
        if judgment.ok:
            deterministic = validate_canonical_result_semantics(
                canonical_slug, candidate_value,
            )
            deterministic_reason = deterministic.reason
            deterministic_failing_fields = deterministic.failing_fields
        candidate_sha_after = canonical_sha256(candidate_value)
        if candidate_sha_after != candidate_sha_before:
            deterministic_reason = "canonical_candidate_mutated"
        disagreement = bool(judgment.ok and deterministic_reason is not None)
        if not judgment.ok:
            raise _ContractRejection(
                "judge",
                {
                    "error": "output_contract_failed",
                    "reason": judgment.reason,
                    "agentSlug": canonical_slug,
                },
            )
        if deterministic_reason is not None:
            raise _ContractRejection(
                "deterministic",
                {
                    "error": "output_contract_failed",
                    "reason": deterministic_reason,
                    "failing_fields": list(deterministic_failing_fields) if deterministic_failing_fields else [],
                    "agentSlug": canonical_slug,
                },
            )
        return (
            router_result,
            submission,
            candidate_value,
            judgment,
            deterministic,
            candidate_sha_after,
            disagreement,
        )

    # A single stochastic candidate can trip a fail-closed contract stage on
    # phrasing alone (e.g. a finance action verb near a finance noun in a
    # next-actions item). That used to surface as an unlogged HTTP 502 and a
    # zero score for a legitimate answer. Retry exactly once with the
    # validator's own feedback appended; the retry must pass every stage
    # unchanged, and a second rejection returns the identical 502.
    try:
        (
            router_result,
            submission,
            candidate_value,
            judgment,
            deterministic,
            candidate_sha_after,
            disagreement,
        ) = await _canonical_pipeline(None)
    except _ContractRejection as first_rejection:
        logger.warning(
            "canonical contract rejection slug=%s stage=%s reason=%s attempt=1; "
            "retrying once with validator feedback",
            canonical_slug,
            first_rejection.stage,
            first_rejection.detail.get("reason"),
        )
        try:
            (
                router_result,
                submission,
                candidate_value,
                judgment,
                deterministic,
                candidate_sha_after,
                disagreement,
            ) = await _canonical_pipeline(_contract_correction(first_rejection))
        except _ContractRejection as final_rejection:
            logger.warning(
                "canonical contract rejection slug=%s stage=%s reason=%s attempt=2; "
                "returning 502",
                canonical_slug,
                final_rejection.stage,
                final_rejection.detail.get("reason"),
            )
            raise HTTPException(
                status_code=502, detail=final_rejection.detail
            ) from None
    response_text = json.dumps(
        candidate_value,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    payload: dict[str, Any] = {
        "ok": True,
        "slug": bundle_slug,
        "response": response_text,
        "transport": router_result.get("transport") if isinstance(router_result, dict) and router_result.get("transport") else transport_name,
    }
    # Publish the same validation evidence consumed by Cato on runtime results.
    # This block is reached only after forced-schema, judge, deterministic and
    # candidate-immutability checks passed. It proves bounded analysis only.
    payload["output_contract_eval"] = {
        "required": True,
        "ok": submission.ok and judgment.ok and deterministic is not None and deterministic.ok,
        "structure_ok": submission.ok,
        "deterministic_ok": deterministic is not None and deterministic.ok,
        "judge_decision": "ALLOW" if judgment.ok else "DENY",
        "candidate_sha256": candidate_sha_after,
        "semantic_disagreement": disagreement,
    }
    if isinstance(router_result, dict):
        if router_result.get("usage") is not None:
            payload["usage"] = router_result.get("usage")
        if router_result.get("model") is not None:
            payload["model"] = router_result.get("model")

    return RunResponse(
        response=json.dumps(payload),
        agentSlug=slug,
        agentName=display_name,
    )


# ============================================================================
# Phase 8 — async job state machine endpoints
# ============================================================================
# `/agents/{slug}/run` remains the synchronous path. The endpoints below let
# callers submit a durable async job (inserted into Postgres `genesis_jobs`,
# picked up by worker.py) and poll for status. Subsequent phases (4 async meta,
# 6 escrow, 11 disputes) attach state transitions on top of this layer.
# ============================================================================

@app.post("/agents/{slug}/jobs")
async def submit_job(slug: str, body: dict, principal=Depends(verify_agent_principal)):
    """Submit an async job. Returns job_id immediately; client polls /agents/jobs/{job_id}.

    External-escrow integration: when `escrow_id` is in the body, the gateway
    persists it on the job and skips its own escrow initiate. The worker
    will fire `webhook_url` on DELIVERED / FAILED so the marketplace can
    settle or refund the marketplace-owned escrow.
    """
    _require_canonical_agent(slug)
    if not _JOB_STORE_OK or create_job is None:
        raise HTTPException(status_code=503, detail="job_store unavailable")

    prompt = body.get("prompt", "")
    if not str(prompt or "").strip() and not body.get("input") and not body.get("task"):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "prompt_required",
                "message": "prompt_required",
                "errorMessage": "prompt_required",
            },
        )
    params = dict(body.get("params", {}) or {})
    idempotency_key = body.get("idempotency_key")
    webhook_url = body.get("webhook_url") or body.get("callback_url")
    if webhook_url:
        _assert_not_ssrf(webhook_url)
    price_tier_cents = body.get("price_tier_cents")
    external_escrow_id = body.get("escrow_id")

    # buyer_client_id from a request header or body
    buyer_client_id = body.get("client_id")

    try:
        job = create_job(
            agent_slug=slug,
            prompt=prompt,
            params=params,
            price_tier_cents=price_tier_cents,
            idempotency_key=idempotency_key,
            webhook_url=webhook_url,
            buyer_client_id=buyer_client_id,
            escrow_id=external_escrow_id,
            tenant_id=principal.tenant_id,
            owner_principal_id=principal.principal_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"job_create_failed: {e}")

    payload = {
        "job_id": job["id"],
        "status": job["status"],
        "poll_url": f"/agents/jobs/{job['id']}",
        "idempotent_hit": job.get("idempotent_hit", False),
    }
    if external_escrow_id:
        payload["external_escrow_id"] = external_escrow_id
    if principal.auth_method == "ap2":
        from runtime.request_auth import issue_principal_token
        payload["principal_token"] = issue_principal_token(principal)

    if not job.get("idempotent_hit"):
        try:
            from trigger_dispatch import dispatch_genesis_job

            dispatch_genesis_job(job["id"])
        except Exception:
            logger.exception("trigger dispatch failed for job %s", job.get("id"))

    return payload


@app.get("/agents/jobs/{job_id}")
async def get_job_status(job_id: str, principal=Depends(verify_continuation_principal)):
    """Poll a job's status + result."""
    if not _JOB_STORE_OK or get_job is None:
        raise HTTPException(status_code=503, detail="job_store unavailable")

    job = get_job(job_id)
    _require_owned_job(principal, job)
    # Serialize datetimes
    for k in ("createdAt", "updatedAt", "startedAt", "completedAt", "lastHeartbeatAt"):
        if job.get(k):
            job[k] = job[k].isoformat() if hasattr(job[k], "isoformat") else str(job[k])
    return job


@app.get("/agents/jobs/{job_id}/events")
async def get_job_events(job_id: str, principal=Depends(verify_continuation_principal)):
    """Return persisted runtime events for a job (Phase 4 observability).

    Events are written to /tmp/jobs/{job_id}/logs/events.jsonl during execution.
    Returns [] if the job has no events yet (job not started or events file missing).
    """
    _authorize_job_read(principal, job_id)
    try:
        from runtime.observability import get_events
        events = get_events(job_id)
        return {"job_id": job_id, "events": events, "count": len(events)}
    except Exception as exc:
        logger.exception("get_job_events failed job=%s", job_id)
        raise HTTPException(status_code=500, detail=f"events_read_failed: {exc}")


@app.get("/agents/jobs/{job_id}/sandbox")
async def get_job_sandbox(job_id: str, principal=Depends(verify_continuation_principal)):
    """Return sandbox lifecycle state + isolation tier for a job (Phase 2/6).

    States: CREATED → ACTIVE → FINALIZING → UPLOADED → CLEANED → FAILED.
    Includes the active isolation tier ("bwrap" for real kernel isolation,
    "process" for the hardened process sandbox). 404 if never registered.
    """
    _authorize_job_read(principal, job_id)
    try:
        from runtime.workspace_manager import get_workspace
        from runtime.sandbox_manager import sandbox_status
        ws = get_workspace(job_id)
        if ws is None:
            raise HTTPException(
                status_code=404,
                detail=f"No sandbox registered for job_id={job_id!r}. "
                "Job may not exist or predates hardening.",
            )
        return sandbox_status(job_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("get_job_sandbox failed job=%s", job_id)
        raise HTTPException(status_code=500, detail=f"sandbox_read_failed: {exc}")


@app.post("/agents/jobs/{job_id}/sandbox", dependencies=[Depends(verify_gateway_key)])
async def create_job_sandbox(job_id: str, body: dict | None = None):
    """Explicitly provision a sandbox for a job (Phase 2 lifecycle)."""
    try:
        from runtime.sandbox_manager import create_sandbox
        session_id = (body or {}).get("session_id")
        return create_sandbox(job_id, session_id)
    except Exception as exc:
        logger.exception("create_job_sandbox failed job=%s", job_id)
        raise HTTPException(status_code=500, detail=f"sandbox_create_failed: {exc}")


@app.post("/agents/jobs/{job_id}/sandbox/destroy", dependencies=[Depends(verify_gateway_key)])
async def destroy_job_sandbox(job_id: str, body: dict | None = None):
    """Tear down a job sandbox (Phase 2 lifecycle).

    Body: {"cleanup_policy": "retain_debug" | "purge"}. Default retain_debug.
    """
    try:
        from runtime.sandbox_manager import destroy_sandbox
        policy = (body or {}).get("cleanup_policy", "retain_debug")
        result = destroy_sandbox(job_id, cleanup_policy=policy)
        if not result.get("ok") and result.get("error") == "sandbox_not_found":
            raise HTTPException(status_code=404, detail=result)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("destroy_job_sandbox failed job=%s", job_id)
        raise HTTPException(status_code=500, detail=f"sandbox_destroy_failed: {exc}")


def _serialize_row(row: dict | None) -> dict | None:
    """ISO-serialize any datetime values in a DB row dict."""
    if not row:
        return row
    out = dict(row)
    for k, v in list(out.items()):
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
    return out


@app.get("/agents/sessions/{session_id}", dependencies=[Depends(verify_gateway_key)])
async def get_session(session_id: str):
    """Return a durable agent session record + its child delegations (Phase 3/6).

    Survives service restart — read from Postgres (genesis_agent_sessions),
    not /tmp. 404 if the session was never persisted.
    """
    try:
        import durable_store
        session = durable_store.session_get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
        job_id = session.get("jobId")
        rels = durable_store.relationships_by_parent(job_id) if job_id else []
        children = [
            r for r in rels if r.get("parentSessionId") in (session_id, None)
        ]
        return {
            "session": _serialize_row(session),
            "children": [_serialize_row(r) for r in children],
            "child_count": len(children),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("get_session failed session=%s", session_id)
        raise HTTPException(status_code=500, detail=f"session_read_failed: {exc}")


@app.get("/agents/jobs/{job_id}/trace")
async def get_job_trace(job_id: str, principal=Depends(verify_continuation_principal)):
    """Return the full parent→child trace tree for a job (Phase 6).

    Aggregates the durable job record, its session(s), lifecycle events, and
    every child job/session spawned via genesis_call. Proves real delegation
    linkage that survives restart.
    """
    _authorize_job_read(principal, job_id)
    try:
        import durable_store
        job = None
        if _JOB_STORE_OK and get_job is not None:
            try:
                job = get_job(job_id)
            except Exception:
                job = None
        sessions = durable_store.sessions_by_job(job_id)
        rels = durable_store.relationships_by_parent(job_id)
        try:
            from runtime.observability import get_events
            events = get_events(job_id)
        except Exception:
            events = []

        children = []
        for r in rels:
            child_job_id = r.get("childJobId")
            child_job = None
            if child_job_id and _JOB_STORE_OK and get_job is not None:
                try:
                    child_job = get_job(child_job_id)
                except Exception:
                    child_job = None
            children.append({
                "relationship": _serialize_row(r),
                "child_job": _serialize_row(child_job),
                "child_sessions": [
                    _serialize_row(s) for s in durable_store.sessions_by_job(child_job_id)
                ] if child_job_id else [],
            })

        if job is None and not sessions and not rels and not events:
            raise HTTPException(status_code=404, detail=f"no trace found for job: {job_id}")

        return {
            "job_id": job_id,
            "job": _serialize_row(job),
            "sessions": [_serialize_row(s) for s in sessions],
            "events": events,
            "event_count": len(events),
            "children": children,
            "child_count": len(children),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("get_job_trace failed job=%s", job_id)
        raise HTTPException(status_code=500, detail=f"trace_read_failed: {exc}")


@app.post("/internal/genesis-worker/tick")
async def genesis_worker_tick(
    body: dict | None = None,
    x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
):
    """Run one Genesis job-worker poll cycle (legacy fallback)."""
    _require_internal_secret(x_internal_secret)

    if not _JOB_STORE_OK:
        raise HTTPException(status_code=503, detail="job_store unavailable")

    limit = 3
    if body and body.get("limit") is not None:
        try:
            limit = max(0, min(10, int(body["limit"])))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="limit must be an integer 0-10")

    try:
        from worker import run_tick
        import asyncio

        # Fire-and-forget: launch in background so client doesn't timeout waiting
        # for long-running jobs (genesis-meta can take 60-120s+).
        # Store task in _background_tasks so asyncio doesn't GC it before completion.
        _tick_limit = limit

        async def _tick_logged():
            try:
                result = await run_tick(limit=_tick_limit, expire_stale=True)
                logger.info("genesis_worker_tick_complete result=%s", result)
            except Exception:
                logger.exception("genesis_worker_tick_background_error limit=%d", _tick_limit)

        task = asyncio.create_task(_tick_logged())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return {"ok": True, "claimed": -1, "processed": -1, "job_ids": [], "dispatched": True}
    except Exception as e:
        logger.exception("genesis_worker_tick failed")
        raise HTTPException(status_code=500, detail=f"genesis_worker_tick_failed: {e}")


@app.post("/internal/genesis-worker/jobs/{job_id}/execute")
async def genesis_worker_execute_job(
    job_id: str,
    x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
):
    """Claim and execute one QUEUED genesis job (Trigger.dev genesis-job-process)."""
    _require_internal_secret(x_internal_secret)

    if not _JOB_STORE_OK:
        raise HTTPException(status_code=503, detail="job_store unavailable")

    try:
        from worker import execute_job_by_id

        result = await execute_job_by_id(job_id)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("genesis_worker_execute_job failed job=%s", job_id)
        raise HTTPException(status_code=500, detail=f"genesis_worker_execute_failed: {e}")


# ============================================================================
# Phase 11 - dispute filing, agent reputation, admin queue
# ============================================================================

@app.post("/jobs/{job_id}/dispute", dependencies=[Depends(verify_gateway_key)])
async def file_dispute(job_id: str, body: dict):
    """Buyer files a dispute. Marks the job DISPUTED and records evidence.

    Escrow is not auto-released; admin review (via /admin/disputes) decides
    refund vs release. If the escrow has already SETTLED, this records the
    dispute for after-the-fact arbitration.
    """
    reason = (body or {}).get("reason", "").strip()
    evidence = (body or {}).get("evidence", {})
    if not reason:
        raise HTTPException(status_code=400, detail="reason required")
    if not _JOB_STORE_OK:
        raise HTTPException(status_code=503, detail="job_store_unavailable")

    from job_store import update_job_status, _conn, _gen_id

    # Confirm the job exists first so we don't lose dispute data on a typo.
    existing = get_job(job_id) if get_job else None
    if not existing:
        raise HTTPException(status_code=404, detail="job not found")

    # Status transition: only move to DISPUTED if it isn't already terminal-
    # disputed. Other terminal states (DELIVERED/SETTLED/FAILED/REFUNDED) are
    # allowed - a buyer can dispute after settlement.
    try:
        # Human-initiated: a buyer may dispute an already-terminal job, so this is one of
        # the three call sites permitted to move a terminal row. The worker never is.
        update_job_status(job_id, "DISPUTED", allow_terminal_override=True)
    except Exception:
        logger.exception("dispute status update failed for %s", job_id)

    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO genesis_job_events
                  (id, "jobId", "eventType", payload, "createdAt")
                VALUES (%s, %s, 'dispute_filed', %s::jsonb, NOW())
                """,
                (_gen_id(), job_id, json.dumps({"reason": reason, "evidence": evidence})),
            )
            conn.commit()
    except Exception:
        logger.exception("dispute event insert failed for %s", job_id)

    return {"ok": True, "job_id": job_id, "status": "DISPUTED"}


@app.get("/agents/{slug}/reputation")
async def agent_reputation(slug: str):
    """Read an agent's 30-day reputation stats. Falls back to seed values."""
    _require_canonical_agent(slug)
    try:
        from capability_cards import _reputation_for
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=503, detail=f"reputation_unavailable: {exc}")
    return {"slug": slug, "reputation": _reputation_for(slug)}


@app.get(
    "/admin/disputes",
    dependencies=[Depends(verify_gateway_key), Depends(require_admin)],
)
async def list_disputes():
    """Admin view: all DISPUTED jobs with their most recent dispute payload.

    Requires BOTH the shared gateway credential and a verified principal
    carrying the ``admin`` scope (see require_admin).  The shared key alone
    resolves to the legacy principal, which holds no admin scope and is
    therefore rejected with 403.
    """
    if not _JOB_STORE_OK:
        raise HTTPException(status_code=503, detail="job_store_unavailable")
    from job_store import _conn
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT j.*,
                       (SELECT payload FROM genesis_job_events e
                        WHERE e."jobId" = j.id AND e."eventType" = 'dispute_filed'
                        ORDER BY e."createdAt" DESC LIMIT 1) AS dispute_payload
                FROM genesis_jobs j
                WHERE j.status = 'DISPUTED'
                ORDER BY j."updatedAt" DESC
                LIMIT 100
                """,
            )
            rows = cur.fetchall()
            # Serialize datetimes
            for r in rows:
                for k in ("createdAt", "updatedAt", "startedAt", "completedAt", "lastHeartbeatAt"):
                    if r.get(k) and hasattr(r[k], "isoformat"):
                        r[k] = r[k].isoformat()
            return {"items": rows}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/admin",
    response_class=HTMLResponse,
    dependencies=[Depends(verify_gateway_key), Depends(require_admin)],
)
async def admin_ui():
    """Fail closed until a signed, HttpOnly admin session flow exists.

    A browser must never receive the service-wide gateway credential: any
    page script or XSS could exfiltrate a key that authorizes every protected
    gateway route. Programmatic admin APIs remain available only with both
    the gateway credential and the configured admin-email role assertion.
    """
    raise HTTPException(
        status_code=503,
        detail="admin_ui_disabled_pending_signed_identity",
    )


@app.get(
    "/admin/",
    include_in_schema=False,
    dependencies=[Depends(verify_gateway_key), Depends(require_admin)],
)
async def admin_ui_legacy_redirect():
    """Method-preserving migration from the retired trailing-slash URL."""
    return RedirectResponse(url="/admin", status_code=308)


@app.post(
    "/admin/disputes/{job_id}/refund",
    dependencies=[Depends(verify_gateway_key), Depends(require_admin)],
)
async def admin_refund(job_id: str):
    """Admin issues refund — releases the escrow back to the buyer.

    If the job has an associated escrow id and the escrow_client wrappers
    imported successfully, the escrow is released with reason
    `admin_refund`. The job row transitions to REFUNDED and an
    `admin_refund` event row is appended to genesis_job_events.
    """
    if not _JOB_STORE_OK:
        raise HTTPException(status_code=503, detail="job_store_unavailable")
    from job_store import get_job as _get_job, update_job_status, _conn, _gen_id

    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    escrow_released: Any = None
    # ESCROW BLOCKER 8/11 — escrow_client.release_escrow (admin refund endpoint).
    # PERMANENTLY_PROHIBITED (6.1 item 18). Unreachable while _ESCROW_OK is
    # False; the job row still transitions to REFUNDED and the admin_refund
    # event is still written, so the record-keeping half of the endpoint keeps
    # working while the money movement does not. Quarantined, not deleted.
    if job.get("escrowId") and _ESCROW_OK and release_escrow is not None:
        try:
            escrow_released = await release_escrow(
                escrow_id=job["escrowId"], reason="admin_refund"
            )
        except Exception:
            logger.exception("escrow release failed for admin refund of %s", job_id)

    try:
        update_job_status(job_id, "REFUNDED", allow_terminal_override=True)  # admin-initiated
    except Exception:
        logger.exception("admin_refund status update failed for %s", job_id)

    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO genesis_job_events
                  (id, "jobId", "eventType", payload, "createdAt")
                VALUES (%s, %s, 'admin_refund', %s::jsonb, NOW())
                """,
                (_gen_id(), job_id, json.dumps({"escrow": escrow_released})),
            )
            conn.commit()
    except Exception:
        logger.exception("admin_refund event insert failed for %s", job_id)

    return {
        "ok": True,
        "job_id": job_id,
        "status": "REFUNDED",
        "escrow": escrow_released,
    }


@app.post(
    "/admin/disputes/{job_id}/resolve",
    dependencies=[Depends(verify_gateway_key), Depends(require_admin)],
)
async def admin_resolve(job_id: str):
    """Admin marks dispute resolved without refund — closes the case as-is.

    Useful when the buyer's evidence does not warrant a refund (e.g. the
    delivery met spec). Job transitions to SETTLED and an
    `admin_resolve` event row is appended to genesis_job_events.
    """
    if not _JOB_STORE_OK:
        raise HTTPException(status_code=503, detail="job_store_unavailable")
    from job_store import get_job as _get_job, update_job_status, _conn, _gen_id

    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    try:
        update_job_status(job_id, "SETTLED", allow_terminal_override=True)  # admin-initiated
    except Exception:
        logger.exception("admin_resolve status update failed for %s", job_id)

    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO genesis_job_events
                  (id, "jobId", "eventType", payload, "createdAt")
                VALUES (%s, %s, 'admin_resolve', %s::jsonb, NOW())
                """,
                (_gen_id(), job_id, json.dumps({"resolved_by": "admin"})),
            )
            conn.commit()
    except Exception:
        logger.exception("admin_resolve event insert failed for %s", job_id)

    return {"ok": True, "job_id": job_id, "status": "RESOLVED"}


@app.post(
    "/agents/{slug}/negotiate",
    status_code=202,
    dependencies=[Depends(verify_gateway_key)],
)
async def negotiate_agent(
    slug: str,
    request: NegotiateRequest,
    background_tasks: BackgroundTasks,
    x_agent_gateway_secret: str | None = Header(default=None, alias="x-agent-gateway-secret"),
) -> dict:
    """
    POST /agents/{slug}/negotiate

    Receives an inbound AP2 negotiation from the SwarmSync API.
    Returns 202 immediately; LLM evaluation and callback happen in the background.

    The callback is POSTed to `request.callback_url` (that SwarmSync environment's /ap2/gateway/respond)
    with the agent's decision. The calling environment is the one whose gateway secret authenticated the
    request (or, for a Genesis API-key caller, the registered environment owning the callback origin); the
    callback must target that environment's origin and carries only that environment's secret.

    Marketplace slugs are allowed here. /run still requires a canonical agent. An accepted marketplace
    negotiation is performed and delivered back to the same environment that authenticated this call.
    """
    caller_env = swarmsync_envs.resolve_by_gateway_secret(x_agent_gateway_secret)
    try:
        env = swarmsync_envs.assert_callback_allowed(request.callback_url, caller_env)
    except ValueError as exc:
        logger.error("negotiate refused: %s (slug=%s)", exc, slug)
        raise HTTPException(status_code=400, detail="untrusted_callback_origin") from exc

    background_tasks.add_task(_evaluate_and_respond, slug, request, env)
    return {
        "status": "evaluating",
        "negotiation_id": request.negotiation_id,
        "agent": slug,
    }


class ProcureRequest(BaseModel):
    need: str
    budget: float
    query: str
    requirements: dict = {}
    environment: Optional[str] = None


class VoucherRedeemRequest(BaseModel):
    voucher_token: str
    nonce: str
    run_id: str
    environment: Optional[str] = None


def _require_ap2_provider() -> ap2_provider.Ap2Provider:
    if _AP2_PROVIDER is None:
        raise HTTPException(status_code=503, detail="ap2_provider_disabled")
    return _AP2_PROVIDER


@app.get("/ap2/provider/status", dependencies=[Depends(verify_gateway_key)])
async def ap2_provider_status() -> dict:
    """Provider state for operators and verifiers: credentials (no keys), runtime identity, ledger."""
    if _AP2_PROVIDER is None:
        return {"enabled": False}
    return _AP2_PROVIDER.status()


@app.post("/agents/{slug}/ap2/procure", dependencies=[Depends(verify_gateway_key)])
async def ap2_procure(slug: str, body: ProcureRequest) -> dict:
    """This Genesis agent buys a service: discover a seller, negotiate with its own key and an explicit offer."""
    provider = _require_ap2_provider()
    _require_canonical_agent(slug)
    try:
        return await provider.procure(
            slug,
            need=body.need,
            budget=body.budget,
            query=body.query,
            requirements=body.requirements,
            environment=body.environment,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/agents/{slug}/ap2/wallet", dependencies=[Depends(verify_gateway_key)])
async def ap2_wallet(slug: str, environment: Optional[str] = None) -> dict:
    """This agent's own wallet balance, read through its own marketplace credential."""
    provider = _require_ap2_provider()
    _require_canonical_agent(slug)
    try:
        return await provider.wallet(slug, environment)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/agents/{slug}/ap2/redeem-voucher", dependencies=[Depends(verify_gateway_key)])
async def ap2_redeem_voucher(slug: str, body: VoucherRedeemRequest) -> dict:
    """Redeem a sponsor voucher into this agent's own wallet, signed by this runtime's Ed25519 key."""
    provider = _require_ap2_provider()
    _require_canonical_agent(slug)
    try:
        return await provider.redeem_voucher(
            slug,
            voucher_token=body.voucher_token,
            nonce=body.nonce,
            run_id=body.run_id,
            environment=body.environment,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# AP2 Negotiate helpers
# ---------------------------------------------------------------------------

async def evaluate_negotiation(slug: str, request: NegotiateRequest) -> dict:
    """
    Ask the agent's LLM persona to evaluate an inbound AP2 negotiation.
    Returns a dict with keys: status, price, notes, counter_budget (optional).
    Defaults to ACCEPTED at the requested budget on any parse failure.
    """
    persona = AGENT_PERSONAS.get(slug)
    import library_agents

    library_persona = library_agents.persona(slug)
    if library_persona is not None:
        _, system_prompt = library_persona
    elif persona:
        _, system_prompt = persona
    else:
        display_name = slug.replace("_", " ").replace("-", " ").title()
        system_prompt = (
            f"You are {display_name}, a specialised AI agent on the SwarmSync marketplace. "
            "Respond helpfully and concisely in character."
        )

    requirements_str = str(request.requirements) if request.requirements else "None"
    notes_str = request.notes or "None"

    negotiation_prompt = (
        "You have received an inbound service request via the AP2 protocol.\n\n"
        f"Requester: {request.requester_agent_name}\n"
        f"Service requested: {request.requested_service}\n"
        f"Budget offered: ${request.budget}\n"
        f"Requirements: {requirements_str}\n"
        f"Notes: {notes_str}\n\n"
        "Evaluate this request based on your capabilities and the budget offered.\n"
        "Respond with ONLY a valid JSON object (no markdown, no extra text):\n"
        "{\n"
        '  "status": "ACCEPTED" or "REJECTED" or "COUNTERED",\n'
        '  "price": <number — your accepted or counter price>,\n'
        '  "notes": "<brief reasoning, max 2 sentences>",\n'
        '  "counter_budget": <number, only if COUNTERED — your preferred price>\n'
        "}\n\n"
        "Decision criteria:\n"
        "- ACCEPTED: the service matches your capabilities and the budget is fair\n"
        "- COUNTERED: the service matches but you need a different price (set counter_budget)\n"
        "- REJECTED: the service is outside your capabilities entirely"
    )

    default_decision = {
        "status": "REJECTED",
        "price": request.budget,
        "notes": "LLM evaluation unavailable — auto-rejected to prevent unintended commitments.",
    }

    try:
        raw = await call_llm_router(system_prompt, negotiation_prompt)
    except HTTPException as e:
        logger.error(
            "LLM router error for negotiate slug=%s status=%s: %s",
            slug,
            e.status_code,
            e.detail,
        )
        return default_decision
    except Exception as exc:
        logger.error("Unexpected error calling LLM for negotiate slug=%s: %s", slug, exc)
        return default_decision

    raw_text, _ = _router_result_payload(raw)
    # Strip markdown code fences if the model wrapped the JSON
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    import json as _json
    try:
        decision = _json.loads(stripped)
        if "status" not in decision or decision["status"] not in ("ACCEPTED", "REJECTED", "COUNTERED"):
            logger.warning("LLM returned unrecognised status for slug=%s — defaulting to REJECTED", slug)
            return default_decision
        return decision
    except Exception as parse_exc:
        logger.warning(
            "Failed to parse LLM JSON for negotiate slug=%s (%s) — defaulting to REJECTED. Raw: %.200s",
            slug,
            parse_exc,
            raw_text,
        )
        return default_decision


def _assert_callback_url_trusted(
    callback_url: str,
    env: "swarmsync_envs.SwarmSyncEnvironment | None" = None,
) -> "swarmsync_envs.SwarmSyncEnvironment":
    """
    SSRF guard: reject any callback_url whose origin is not the authenticated SwarmSync environment's own
    (or, with no environment, a registered one). This prevents a malicious negotiate payload from
    redirecting the gateway's authenticated POST (which carries that environment's gateway secret) to an
    arbitrary host, and keeps one environment's secret from ever reaching another environment's origin.
    Raises ValueError; returns the environment whose secret may be attached.
    """
    return swarmsync_envs.assert_callback_allowed(callback_url, env)


async def send_negotiate_callback(
    callback_url: str,
    payload: dict,
    env: "swarmsync_envs.SwarmSyncEnvironment | None" = None,
) -> bool:
    """
    POST the negotiation decision back to the SwarmSync API callback URL.
    Returns True on 2xx response, False otherwise.

    The callback_url is validated against the calling environment's origin before any network call is
    made to prevent SSRF via a crafted negotiate payload.
    """
    try:
        env = _assert_callback_url_trusted(callback_url, env)
    except ValueError as ssrf_exc:
        logger.error("SSRF guard blocked callback to '%s': %s", callback_url, ssrf_exc)
        return False

    headers = {
        "Content-Type": "application/json",
        "x-agent-gateway-secret": env.gateway_secret,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(callback_url, json=payload, headers=headers)
        if 200 <= resp.status_code < 300:
            logger.info("Negotiate callback succeeded: %d %s", resp.status_code, callback_url)
            return True
        elif 400 <= resp.status_code < 500:
            # 4xx means the API rejected this request permanently (e.g. "Negotiation no
            # longer actionable" after a prior call already succeeded).  Retrying would
            # create duplicate escrows, so treat this as a terminal non-retriable outcome.
            # None tells the caller to stop without delivering work.
            logger.warning(
                "Negotiate callback returned 4xx (terminal — will not retry): %d %s — body: %.200s",
                resp.status_code,
                callback_url,
                resp.text,
            )
            return None
        else:
            logger.warning(
                "Negotiate callback returned 5xx: %d %s — body: %.200s",
                resp.status_code,
                callback_url,
                resp.text,
            )
            return False
    except Exception as exc:
        logger.error("Negotiate callback request failed for %s: %s", callback_url, exc)
        return False


def _price_cents(value: Any) -> Optional[int]:
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError):
        return None


async def _evaluate_and_respond(
    slug: str,
    request: NegotiateRequest,
    env: "swarmsync_envs.SwarmSyncEnvironment | None" = None,
) -> None:
    """Background task: evaluate negotiation via LLM and POST decision to callback URL."""
    try:
        decision = await evaluate_negotiation(slug, request)

        status = decision.get("status", "ACCEPTED")
        if status == "COUNTERED":
            resolved_price = decision.get("counter_budget", request.budget)
        else:
            resolved_price = decision.get("price", request.budget)

        # SwarmSync lets the seller accept the buyer's offer only as made. An "accept" at any other price
        # is, commercially, a counter, so say so instead of sending an accept the marketplace must refuse.
        if status == "ACCEPTED" and _price_cents(resolved_price) != _price_cents(request.budget):
            status = "COUNTERED"

        payload = {
            "negotiationId": request.negotiation_id,
            "responderAgentId": request.responder_agent_id,
            "status": status,
            "price": resolved_price,
            "notes": decision.get("notes", "Auto-evaluated by agent gateway"),
            "estimatedDelivery": (datetime.utcnow() + timedelta(hours=48)).isoformat() + "Z",
        }

        success = await send_negotiate_callback(request.callback_url, payload, env)
        if success is not True:
            if success is False:
                logger.warning(
                    "Negotiate callback failed for slug=%s negotiation_id=%s",
                    slug,
                    request.negotiation_id,
                )
            return

        # Deliver only a real accept, and only for marketplace agents this gateway can perform.
        # Canonical agents keep their own fulfillment path. A price mismatch was rewritten to
        # COUNTERED above, so it must not be delivered.
        from canonical_profiles import CANONICAL_SLUGS
        import library_agents

        if (
            status == "ACCEPTED"
            and env is not None
            and slug not in CANONICAL_SLUGS
            and not library_agents.is_library_slug(slug)
        ):
            await _execute_and_deliver(
                slug=slug,
                negotiation_id=request.negotiation_id,
                responder_agent_id=request.responder_agent_id,
                requested_service=request.requested_service,
                requirements=request.requirements,
                env=env,
            )
    except Exception as exc:
        logger.error("Error in _evaluate_and_respond for slug=%s: %s", slug, exc, exc_info=True)


async def _execute_and_deliver(
    slug: str,
    negotiation_id: str,
    responder_agent_id: str,
    requested_service: str,
    requirements: dict | None,
    env: "swarmsync_envs.SwarmSyncEnvironment",
) -> None:
    """Perform bounded marketplace work and POST /ap2/deliver to the calling environment only."""
    from ap2_delivery import (
        execute_agent_work,
        deliver_to_swarmsync,
        mark_delivered,
        is_already_delivered,
        WorkResult,
        MAX_TIMEOUT_SECONDS,
    )

    if not env.gateway_secret:
        logger.error("Negotiation %s has no gateway secret for %s; not delivering", negotiation_id, env.name)
        return

    if is_already_delivered(negotiation_id):
        logger.warning("Negotiation %s already delivered - skipping duplicate", negotiation_id)
        return

    result: WorkResult | None = None
    is_success = False
    failure_reason: str | None = None

    try:
        logger.info(
            "Starting bounded work for %s negotiation %s: %s",
            slug,
            negotiation_id,
            requested_service[:200],
        )
        llm_caller = None
        if "research" in slug.lower():
            async def llm_wrapper(system: str, user: str) -> str:
                raw = await call_llm_router(system, user)
                text, _ = _router_result_payload(raw)
                return text
            llm_caller = llm_wrapper

        result = await execute_agent_work(
            slug=slug,
            task=requested_service,
            requirements=requirements,
            llm_caller=llm_caller,
            timeout_seconds=MAX_TIMEOUT_SECONDS,
        )
        is_success = True
    except asyncio.TimeoutError:
        logger.error("Work timed out for negotiation %s after %ss", negotiation_id, MAX_TIMEOUT_SECONDS)
        failure_reason = f"Work timed out after {MAX_TIMEOUT_SECONDS} seconds"
    except Exception as exc:
        logger.error("Work failed for negotiation %s: %s", negotiation_id, exc, exc_info=True)
        failure_reason = f"Work execution error: {type(exc).__name__}: {exc}"

    try:
        delivery_result = await deliver_to_swarmsync(
            negotiation_id=negotiation_id,
            responder_agent_id=responder_agent_id,
            result=result,
            api_url=env.api_url,
            gateway_secret=env.gateway_secret,
            is_success=is_success,
            failure_reason=failure_reason,
        )
    except Exception as exc:
        logger.error("Failed to deliver results for negotiation %s: %s", negotiation_id, exc, exc_info=True)
        return

    if not delivery_result.success:
        logger.error("Delivery failed for negotiation %s: %s", negotiation_id, delivery_result.error)
        return

    mark_delivered(
        negotiation_id=negotiation_id,
        responder_agent_id=responder_agent_id,
        content_hash=result.content_hash if result else None,
        delivery_id=delivery_result.delivery_id,
        is_success=is_success,
        failure_reason=failure_reason,
    )


# ---------------------------------------------------------------------------
# Conduit Verification Endpoints
# ---------------------------------------------------------------------------


# RFC-1918 and loopback prefixes that must never be fetched server-side
_SSRF_BLOCKED_PREFIXES = (
    "localhost",
    "127.",
    "0.",
    "10.",
    "192.168.",
    # 172.16.0.0/12 covers 172.16.x.x through 172.31.x.x
    "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.",
    "172.24.", "172.25.", "172.26.", "172.27.",
    "172.28.", "172.29.", "172.30.", "172.31.",
    "::1",
    "fe80:",
)


def _assert_not_ssrf(url: str) -> None:
    """Raise HTTPException 400 if url resolves to a private or loopback address."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    for blocked in _SSRF_BLOCKED_PREFIXES:
        if host == blocked.rstrip(".") or host.startswith(blocked):
            raise HTTPException(
                status_code=400,
                detail=f"Blocked: URL host '{host}' resolves to a private/loopback address",
            )


@app.get("/verify/hash", dependencies=[Depends(verify_gateway_key)])
async def verify_hash(url: str) -> dict:
    """
    GET /verify/hash?url=<artifact_url>

    Server-side fallback for artifact hashing when the browser-side
    crypto.subtle.digest eval is blocked by CORS.  Returns the SHA-256
    of the artifact bytes fetched from the given URL.

    SSRF protection: rejects any URL whose host matches RFC-1918 or
    loopback prefixes (localhost, 127.x, 10.x, 192.168.x, 172.16-31.x).
    """
    import hashlib as _hashlib
    _assert_not_ssrf(url)
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
            resp = await client.get(url)
            if resp.is_redirect:
                raise HTTPException(
                    status_code=400,
                    detail="Redirects not followed — submit the final URL directly",
                )
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Artifact fetch returned HTTP {exc.response.status_code}",
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Artifact fetch failed: {exc}",
        )
    sha256 = _hashlib.sha256(resp.content).hexdigest()
    return {
        "sha256": sha256,
        "byte_count": len(resp.content),
        "http_status": resp.status_code,
    }


@app.post("/verify", status_code=202)
async def start_verification(
    request: VerifyRequest,
    x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret"),
) -> dict[str, str]:
    """
    POST /verify

    Starts an async Conduit verification job and returns 202 Accepted with
    the jobId immediately.  When the job completes, the result is POSTed back
    to the calling SwarmSync environment's /conduit/verifications/{verificationId}/callback.

    Authentication: X-Internal-Secret header. The presented secret identifies the calling SwarmSync
    environment (production or certification staging, see swarmsync_environments.py); the callback goes to
    that environment's origin with that environment's secret, never to a process-global URL.
    """
    env = _require_internal_secret_env(x_internal_secret)

    job_id = await _accept_verification_job(
        verification_job_store.KIND_VERIFY,
        env,
        request.context.verification_id,
        request.model_dump(mode="json"),
    )
    logger.info(
        "Verification job %s accepted for negotiation %s verification %s",
        job_id,
        request.negotiationId,
        request.context.verification_id,
    )
    return {"jobId": job_id}


@app.post("/internal/arbitrage/verification-jobs", status_code=202)
async def start_arbitrage_verification(
    request: ArbitrageVerificationRequest,
    x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret"),
) -> dict[str, str]:
    """
    POST /internal/arbitrage/verification-jobs

    Starts an async arbitrage verification job and callbacks the NestJS
    arbitrage endpoint of the calling environment when the Conduit run completes.
    """
    env = _require_internal_secret_env(x_internal_secret)

    job_id = await _accept_verification_job(
        verification_job_store.KIND_ARBITRAGE,
        env,
        request.verification_run_id,
        request.model_dump(mode="json"),
    )
    logger.info(
        "Arbitrage verification job %s accepted for transaction %s verification %s",
        job_id,
        request.transaction_id,
        request.verification_run_id,
    )
    return {"jobId": job_id}


@app.get("/verify/{job_id}", response_model=VerifyJobStatus)
async def get_verification_status(
    job_id: str,
    x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret"),
) -> VerifyJobStatus:
    """
    GET /verify/:job_id

    Poll the status of a verification job.

    Authentication: X-Internal-Secret header (jobs are visible only to the environment that created them).
    """
    env = _require_internal_secret_env(x_internal_secret)

    job = await asyncio.to_thread(verification_job_store.get, verification_job_store.KIND_VERIFY, job_id)
    if job is None or job["environment"] != env.name:
        raise HTTPException(status_code=404, detail=f"Verification job {job_id} not found")

    result_data = job.get("result")
    result_model: Optional[VerificationResult] = None
    if result_data:
        result_model = VerificationResult(**result_data)

    return VerifyJobStatus(
        job_id=job_id,
        status=job["status"],
        result=result_model,
        created_at=job["created_at"],
        completed_at=job.get("completed_at"),
        error=job.get("error"),
    )


@app.get("/internal/arbitrage/verification-jobs/{job_id}")
async def get_arbitrage_verification_status(
    job_id: str,
    x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret"),
) -> dict[str, Any]:
    env = _require_internal_secret_env(x_internal_secret)

    job = await asyncio.to_thread(verification_job_store.get, verification_job_store.KIND_ARBITRAGE, job_id)
    if job is None or job["environment"] != env.name:
        raise HTTPException(status_code=404, detail=f"Arbitrage verification job {job_id} not found")

    return {
        "job_id": job["job_id"],
        "environment": job["environment"],
        "status": job["status"],
        "result": job["result"],
        "created_at": job["created_at"],
        "completed_at": job["completed_at"],
        "error": job["error"],
        "callback_state": job["callback_state"],
    }


# ---------------------------------------------------------------------------
# SwarmSync Commerce Demo Agent — A2A public registry entry
#
# Serves the agent card at /.well-known/agent.json so a2aregistry.org can
# verify ownership, then handles inbound A2A JSON-RPC and demo flows.
# ---------------------------------------------------------------------------

COMMERCE_DEMO_AGENT_CARD: dict[str, Any] = {
    "id": "swarmsync-commerce-demo",
    "name": "SwarmSync Commerce Demo Agent",
    "description": "Public demo agent showing how AI agents become commerce-ready: create paid tasks, hold funds in escrow, verify delivery, build portable trust with SwarmScore, and release or refund payment safely.",
    "url": "https://swarmsync-agents.onrender.com/a2a",
    "wellKnownURI": "https://swarmsync.ai/.well-known/agent.json",
    "protocolVersion": "0.3.0",
    "version": "1.0.2",
    "author": "SwarmSync.AI",
    "provider": {
        "organization": "SwarmSync.AI",
        "url": "https://swarmsync.ai",
    },
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
        "stateTransitionHistory": True,
    },
    "skills": [
        {
            "id": "escrow-demo",
            "name": "Escrow-Protected Payment Demo",
            "description": "Simulates an escrow-protected agent-to-agent payment flow.",
            "tags": ["escrow", "ap2", "commerce", "demo", "agent payments"],
            "examples": [
                "Show me a paid agent task.",
                "How does escrow work between two AI agents?",
            ],
            "documentationUrl": "https://swarmsync.ai",
            "endpoint": "https://swarmsync-agents.onrender.com/demo/escrow-flow",
        },
        {
            "id": "trust-badge-demo",
            "name": "SwarmScore Trust Badge Demo",
            "description": "Returns a sample portable trust badge for an AI agent.",
            "tags": ["swarmscore", "trust", "badge", "demo", "portable trust"],
            "examples": [
                "What is SwarmScore?",
                "Show me a trust badge for an agent.",
            ],
            "documentationUrl": "https://swarmsync.ai",
            "endpoint": "https://swarmsync-agents.onrender.com/demo/trust-badge/example-agent",
        },
        {
            "id": "task-verification-demo",
            "name": "Task Verification Demo",
            "description": "Simulates task verification and payment release.",
            "tags": ["verification", "task", "commerce", "demo", "payment release"],
            "examples": [
                "How does task verification work?",
                "Show me how payment is released after delivery.",
            ],
            "documentationUrl": "https://swarmsync.ai",
            "endpoint": "https://swarmsync-agents.onrender.com/demo/task-verify",
        },
    ],
    "defaultInputModes": ["text", "application/json"],
    "defaultOutputModes": ["text", "application/json"],
    "tags": ["swarmsync", "commerce", "demo", "a2a", "escrow", "swarmscore", "ap2"],
    "contactEmail": "support@swarmsync.ai",
}


@app.get("/.well-known/agent.json")
async def agent_card() -> dict[str, Any]:
    """A2A agent card — required for a2aregistry.org ownership verification."""
    return COMMERCE_DEMO_AGENT_CARD


@app.get("/a2a/health")
async def a2a_health() -> dict[str, Any]:
    """A2A health probe — some registries and crawlers check /a2a/health."""
    return {"status": "ok", "service": COMMERCE_DEMO_AGENT_CARD["name"]}


@app.get("/a2a")
async def a2a_discovery() -> dict[str, Any]:
    """
    GET returns 200 so registry health probes — which often GET the agent URL — do not see
    405 Method Not Allowed (some proxies surface that as 502 to end users).

    Conversation uses POST JSON-RPC only (see POST /a2a).
    """
    return {
        "service": COMMERCE_DEMO_AGENT_CARD["name"],
        "protocolVersion": COMMERCE_DEMO_AGENT_CARD["protocolVersion"],
        "usage": "POST JSON-RPC 2.0 to this same path",
        "methods": ["tasks/send", "message/send", "messages/send"],
        "documentation": COMMERCE_DEMO_AGENT_CARD.get("provider", {}).get("url"),
    }


@app.post("/a2a", dependencies=[Depends(verify_gateway_key)])
async def a2a_handler(request: Request) -> dict[str, Any]:
    """
    A2A JSON-RPC endpoint (v0.3.0).
    Handles tasks/send and message/send from other agents.
    """
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error — body must be valid JSON"},
        }

    method = body.get("method", "")
    req_id = body.get("id") or str(uuid.uuid4())
    params: dict[str, Any] = body.get("params") or {}

    if method == "tasks/send":
        task = params.get("task") or params
        task_type = task.get("type", "unknown") if isinstance(task, dict) else "unknown"
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "taskId": str(uuid.uuid4()),
                "status": "submitted",
                "type": task_type,
                "message": (
                    "Task received by SwarmSync Commerce Demo Agent. "
                    "Connect at https://swarmsync.ai to run live AP2 escrow flows."
                ),
                "agent": COMMERCE_DEMO_AGENT_CARD["id"],
                "demoEndpoints": {
                    "escrow_flow": "POST /demo/escrow-flow",
                    "trust_badge": "GET /demo/trust-badge/{agent_id}",
                    "task_verify": "POST /demo/task-verify",
                },
            },
        }

    if method in ("message/send", "messages/send"):
        msg = params.get("message") or {}
        parts_text = " ".join(
            p.get("text", "") for p in (msg.get("parts") or []) if isinstance(p, dict) and p.get("kind") == "text"
        ).strip()
        query = parts_text.lower()

        if any(kw in query for kw in ["swarmscore", "trust badge", "trust score", "reputation", "badge", "score", "tier", "rating"]):
            reply_text = (
                "SwarmScore is a portable trust badge for AI agents.\n\n"
                "It helps other agents, marketplaces, and platforms evaluate whether an agent is reliable before hiring it. A SwarmScore can include signals like completed tasks, dispute rate, on-time delivery, verification history, and signed proof records.\n\n"
                "The goal is simple: agents should be able to carry trust with them across the agent economy.\n\n"
                "Demo endpoint: GET https://swarmsync-agents.onrender.com/demo/trust-badge/{your-agent-id}\n"
                "→ Go live: https://swarmsync.ai"
            )
        elif any(kw in query for kw in ["what does swarm", "what is swarm", "what do you do", "tell me about swarm", "explain swarm"]):
            reply_text = (
                "SwarmSync is the trust and transaction layer for autonomous agents.\n\n"
                "It helps AI agents safely pay and get paid by creating paid tasks, holding funds in escrow, verifying delivery, applying portable SwarmScore trust badges, and releasing or refunding payment after the work is complete.\n\n"
                "→ https://swarmsync.ai"
            )
        elif any(kw in query for kw in ["show me", "paid task", "paid agent", "sample task", "example task", "transaction"]):
            reply_text = (
                "Sample paid agent transaction:\n\n"
                "  Buyer agent:         research-buyer-agent\n"
                "  Seller agent:        market-research-agent\n"
                "  Task:                Produce a competitor summary for a SaaS landing page\n"
                "  Price:               45 USDC\n"
                "  Escrow status:       funded\n"
                "  Verification method: delivery proof checked by SwarmSync\n"
                "  Final result:        work verified, payment released\n\n"
                "Flow: Buyer agent creates task → seller accepts → escrow funded → work delivered → verification passed → payment released.\n\n"
                "→ Demo: POST https://swarmsync-agents.onrender.com/demo/escrow-flow\n"
                "→ Go live: https://swarmsync.ai\n\n"
                "Note: This is simulated demo data. No real funds are moved."
            )
        elif any(kw in query for kw in ["escrow", "how does pay", "how does fund", "locked", "hold fund", "ap2", "usdc"]):
            reply_text = (
                "Escrow protects both sides of an agent transaction.\n\n"
                "The buyer agent's funds are held before work begins. The seller agent can see that payment is secured, but cannot collect it until the task is verified. If the work passes verification, payment is released. If the work fails, the funds can be refunded.\n\n"
                "Demo endpoint: POST https://swarmsync-agents.onrender.com/demo/escrow-flow\n"
                "→ Go live: https://swarmsync.ai"
            )
        elif any(kw in query for kw in ["get paid", "receive payment", "earn", "my agent get paid", "how can my"]):
            reply_text = (
                "Your agent can get paid through SwarmSync by becoming commerce-ready.\n\n"
                "The setup flow is:\n"
                "  1. Publish an agent card.\n"
                "  2. Register or claim your agent on SwarmSync.\n"
                "  3. Create a service listing.\n"
                "  4. Set pricing.\n"
                "  5. Enable escrow-backed paid tasks.\n"
                "  6. Complete work.\n"
                "  7. Pass verification.\n"
                "  8. Receive payment after release.\n\n"
                "SwarmSync handles the trust, escrow, verification, and payment-release flow.\n\n"
                "→ https://swarmsync.ai"
            )
        elif any(kw in query for kw in ["commerce-ready", "commerce ready", "make my agent", "ready to sell", "ready to earn", "setup", "set up agent"]):
            reply_text = (
                "To make your agent commerce-ready:\n"
                "  1. Give your agent a public identity.\n"
                "  2. Publish a valid agent card.\n"
                "  3. Add clear skills and services.\n"
                "  4. Connect the agent to SwarmSync.\n"
                "  5. Generate or attach a SwarmScore trust badge.\n"
                "  6. Configure pricing.\n"
                "  7. Enable escrow-backed task acceptance.\n"
                "  8. Test a demo transaction.\n"
                "  9. Move to live paid tasks.\n\n"
                "Commerce-ready means your agent can be discovered, trusted, hired, verified, and paid.\n\n"
                "For examples and docs: https://github.com/swarmsync-ai/commerce-demo-agent\n\n"
                "→ https://swarmsync.ai"
            )
        else:
            reply_text = (
                "SwarmSync Commerce Demo Agent — ask me anything:\n\n"
                "  • \"What does SwarmSync do?\"\n"
                "  • \"Show me a paid agent task.\"\n"
                "  • \"How does escrow work?\"\n"
                "  • \"What is SwarmScore?\"\n"
                "  • \"How can my agent get paid through SwarmSync?\"\n"
                "  • \"How do I make my agent commerce-ready?\"\n\n"
                "→ https://swarmsync.ai"
            )

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "kind": "message",
                "messageId": str(uuid.uuid4()),
                "role": "agent",
                "parts": [{"kind": "text", "text": reply_text}],
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method '{method}' not found"},
    }


@app.get("/demo/commerce")
async def demo_commerce_info() -> dict[str, Any]:
    """Public overview of the Commerce Demo Agent capabilities."""
    return {
        "agent": "SwarmSync Commerce Demo Agent",
        "purpose": "Demonstrates how AI agents become commerce-ready through trust, escrow, verification, and payment.",
        "suggested_questions": [
            "What does SwarmSync do?",
            "Show me a paid agent task.",
            "How does escrow work?",
            "What is SwarmScore?",
            "How can my agent get paid through SwarmSync?",
            "How do I make my agent commerce-ready?",
        ],
        "demo_endpoints": [
            {
                "method": "POST",
                "url": "https://swarmsync-agents.onrender.com/demo/escrow-flow",
                "description": "Simulated AP2 escrow flow: create → fund → verify → release.",
            },
            {
                "method": "GET",
                "url": "https://swarmsync-agents.onrender.com/demo/trust-badge/{agent_id}",
                "description": "Sample SwarmScore portable trust badge for an AI agent.",
            },
            {
                "method": "POST",
                "url": "https://swarmsync-agents.onrender.com/demo/task-verify",
                "description": "Simulated task verification and payment release.",
            },
        ],
        "next_step": "Register at https://swarmsync.ai to run real transactions.",
        "github_repo": "https://github.com/swarmsync-ai/commerce-demo-agent",
        "demo_note": "All demo data is simulated. No real funds are moved.",
    }


@app.post("/demo/escrow-flow")
async def demo_escrow_flow() -> dict[str, Any]:
    """Simulated AP2 escrow flow: create → fund → verify → release."""
    escrow_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    return {
        "demo": "AP2 Escrow Flow",
        "escrow_id": escrow_id,
        "status": "completed",
        "steps": [
            {"step": 1, "name": "create", "status": "ok", "ts": now,
             "detail": "Escrow created between buyer and seller agents"},
            {"step": 2, "name": "fund", "status": "ok", "ts": now,
             "detail": "USDC locked in AP2 smart contract on Base"},
            {"step": 3, "name": "verify", "status": "ok", "ts": now,
             "detail": "Conduit browser verified delivery at target URL"},
            {"step": 4, "name": "release", "status": "ok", "ts": now,
             "detail": "Funds released to seller agent wallet on Base"},
        ],
        "about": "This demo shows an AP2-style escrow flow for agent-to-agent commerce.",
        "next_step": "Make your agent commerce-ready at https://swarmsync.ai",
        "demo_note": "This is a simulated demo. No real funds are moved.",
        "buyer_agent": "research-buyer-agent",
        "seller_agent": "market-research-agent",
        "task": "Produce a competitor summary for a SaaS landing page",
        "amount_usdc": 45.00,
        "swarmscore_impact": "Successful completion increases seller SwarmScore.",
    }


@app.get("/demo/trust-badge/{agent_id}")
async def demo_trust_badge(agent_id: str) -> dict[str, Any]:
    """Simulated SwarmScore trust badge for an agent."""
    return {
        "demo": "SwarmScore Trust Badge",
        "agent_id": agent_id,
        "swarmscore": {
            "score": 847,
            "max_score": 1000,
            "tier": "Gold",
            "completed_tasks": 142,
            "dispute_rate": 0.007,
            "on_time_rate": 0.97,
            "badge_url": f"https://swarmsync.ai/badges/{agent_id}",
            "verified": True,
        },
        "about": (
            "SwarmScore is a portable trust badge for AI agents. It helps agents, "
            "marketplaces, registries, and platforms evaluate reliability before paid work begins."
        ),
        "meaning": "This badge shows how a third party could evaluate agent trust before hiring.",
        "next_step": "Make your agent commerce-ready at https://swarmsync.ai",
        "portable": True,
        "verified_by": "SwarmScore",
        "verification_url": f"https://swarmsync.ai/verify/{agent_id}",
        "display_recommendation": "Show this badge on your agent card to increase hire rate.",
        "demo_note": "This is a simulated trust badge. Real scores are computed from live transaction history.",
    }


@app.post("/demo/task-verify")
async def demo_task_verify() -> dict[str, Any]:
    """Simulated task verification and payment release."""
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    return {
        "demo": "Task Verification + Payment Release",
        "job_id": job_id,
        "result": {
            "verification_status": "PASSED",
            "proof_hash": f"sha256:a3f8c2{job_id[:8]}",
            "verified_at": now,
            "payment_released": True,
            "amount_usdc": 45.00,
            "recipient_agent": "example-seller-agent",
        },
        "about": "The demo shows how SwarmSync ties payment release to verified delivery.",
        "meaning": "Payment was released because the work passed verification.",
        "next_step": "Make your agent commerce-ready at https://swarmsync.ai",
        "verification_method": "SwarmSync delivery proof check",
        "release_decision": "Payment released to seller agent.",
        "refund_if_failed": "Funds returned to buyer agent if verification fails.",
        "swarmscore_updated": True,
        "demo_note": "This is a simulated verification. No real funds are moved.",
    }


# ----------------------------------------------------------------------------
# Phase 10 - Capability cards + marketplace listing
# ----------------------------------------------------------------------------

@app.get("/.well-known/agents.json")
async def well_known_agents() -> dict[str, Any]:
    """Public discovery endpoint - JSON-LD listing of all Genesis agents."""
    return {
        "version": 1,
        "platform": "swarmsync",
        "agents": all_cards(),
    }


@app.get("/agents/{slug}/card")
async def agent_capability_card(slug: str) -> dict[str, Any]:
    """Single agent's capability card."""
    _require_canonical_agent(slug)
    card = card_for(slug)
    if card is None:
        raise HTTPException(status_code=404, detail=f"agent not found: {slug}")
    return card


@app.get("/marketplace/search")
async def marketplace_search(
    q: str = "",
    capability: str = "",
    max_price_cents: int = 0,
    job_mode: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """Simple search over capability cards. Postgres FTS upgrade in Phase 11.

    q: keyword query (matches name + description)
    capability: filter to agents whose tools_advertised contains this name
    max_price_cents: cap on total price
    job_mode: 'sync' | 'async' (omit for both)
    """
    cards = all_cards()
    if q:
        ql = q.lower()
        cards = [c for c in cards if ql in c["name"].lower() or ql in c["description"].lower()]
    if capability:
        cards = [c for c in cards if any(cap["tool"] == capability for cap in c["capabilities"])]
    if max_price_cents > 0:
        cards = [c for c in cards if c["pricing"]["total_cents"] <= max_price_cents]
    if job_mode in ("sync", "async"):
        cards = [c for c in cards if c["job_mode"] == job_mode]

    # Rank by reputation, then price asc
    cards.sort(key=lambda c: (-(c["reputation"]["rating"] or 0), c["pricing"]["total_cents"]))
    return {"total": len(cards), "results": cards[:limit]}


# ----------------------------------------------------------------------------
# Phase 9 - Output storage + Conduit session delivery
# ----------------------------------------------------------------------------

@app.get("/jobs/{job_id}/artifacts", include_in_schema=False)
async def job_artifacts_legacy_redirect(
    job_id: str, principal=Depends(verify_continuation_principal)
) -> RedirectResponse:
    """Retired artifact listing; preserve method and credentials on migration."""
    return RedirectResponse(url=f"/agents/jobs/{job_id}/artifacts", status_code=308)


@app.post("/jobs/{job_id}/session", dependencies=[Depends(verify_gateway_key)])
async def store_buyer_session(job_id: str, body: dict) -> dict[str, Any]:
    """Buyer uploads a Conduit session export for the agent to operate under.

    Body: {"session_data": {...conduit session export...}}
    """
    session_data = body.get("session_data")
    if not session_data:
        raise HTTPException(status_code=400, detail="session_data required")
    result = store_session(job_id=job_id, session_data=session_data)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "unknown"))
    return result


@app.get("/artifacts/{job_id}/{name:path}", dependencies=[Depends(verify_gateway_key)])
async def serve_artifact(job_id: str, name: str):
    """Serve a local artifact when S3 is unavailable. Read-only."""
    local_dir = Path(os.getenv("GENESIS_LOCAL_ARTIFACT_DIR", "/var/data/genesis-artifacts"))
    path = local_dir / job_id / name
    # Path traversal guard
    try:
        path.resolve().relative_to(local_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(str(path))


@app.get("/proofs/{proof_id}/verify", dependencies=[Depends(verify_gateway_key)])
async def verify_proof(proof_id: str):
    """Verify a GenesisProof's VCAP wrapper JWT.

    Returns the proof metadata plus an Ed25519 verification verdict over the
    stored wrapper token. Does NOT re-verify the Conduit bundle bytes — that
    requires downloading the .tar.gz and running its bundled verify.py.
    """
    if not _PROOF_BRIDGE_OK or verify_vcap_wrapper_jwt is None:
        raise HTTPException(status_code=503, detail="proof_bridge unavailable")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except Exception:
        raise HTTPException(status_code=503, detail="psycopg unavailable")

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise HTTPException(status_code=503, detail="DATABASE_URL not configured")

    try:
        with psycopg.connect(db_url, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM genesis_proofs WHERE id = %s', (proof_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="proof not found")

            verification = verify_vcap_wrapper_jwt(row["vcapWrapperJwt"])
            created_at = row.get("createdAt")
            return {
                "proof_id": proof_id,
                "job_id": row.get("jobId"),
                "agent_slug": row["agentSlug"],
                "created_at": created_at.isoformat() if created_at else None,
                "verification": verification,
                "proof_bundle_uri": row["proofBundleUri"],
                "proof_bundle_signed_url": row.get("proofBundleSignedUrl"),
                "input_hash": row["inputHash"],
                "output_hash": row["outputHash"],
                "conduit_session_id": row["conduitSessionId"],
            }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("proof verification failed for proof_id=%s: %s", proof_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"database error: {exc}") from exc
