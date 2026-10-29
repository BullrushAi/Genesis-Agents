"""Genesis agent runtime - multi-turn LLM loop with tool dispatch, per-slug parameterized."""
from __future__ import annotations
import json
import hmac
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from bundle_loader import load_bundle
from canonical_result_contract import (
    CANONICAL_RESULT_CONTRACT_VERSION,
    CANONICAL_RESULT_TOOL_NAME,
    canonical_result_tool_schema,
    consume_canonical_result_submission,
    parse_and_validate_canonical_result,
    validate_canonical_result_semantics,
)
from tools import get_tool, register_default_tools, tool_schemas_for, validate_tool_arguments
from runtime.semantic_safety_judge import (
    CANDIDATE_READ_TIMEOUT_S,
    CANDIDATE_MODEL,
    ENDPOINT_READ_BUDGET_S,
    ENDPOINT_RESERVED_OVERHEAD_S,
    JUDGE_READ_TIMEOUT_S,
    JUDGE_MODEL,
    POLICY_VERSION as SEMANTIC_JUDGE_POLICY_VERSION,
    REASON_CANDIDATE_TRANSPORT_FAILED,
    call_exact_anthropic_judge,
    call_exact_anthropic_candidate,
    canonical_sha256,
    canonical_candidate_output_cap,
    judge_candidate_semantics,
    require_canonical_native_anthropic_config,
)
from runtime.router_canonical import (
    call_router_canonical_candidate,
    call_router_judge,
    router_fallback_allowed,
)
from runtime.anthropic_wire import exact_forced_tool_envelope_reason

# Phase 2-8 hardening modules — imported tolerantly so the runtime boots in
# stripped environments (tests without full deps, etc.)
try:
    from runtime.workspace_manager import create_workspace, set_workspace_status, get_workspace
    _WS_MANAGER_OK = True
except Exception:
    _WS_MANAGER_OK = False

try:
    from runtime.observability import (
        emit_event,
        EVT_JOB_CREATED, EVT_AGENT_STARTED, EVT_LLM_REQUESTED, EVT_LLM_RESPONDED,
        EVT_TOOL_CALLED, EVT_TOOL_BLOCKED, EVT_TOOL_RESULT,
        EVT_SUBAGENT_DISPATCHED, EVT_SUBAGENT_RETURNED,
        EVT_JOB_COMPLETED, EVT_JOB_FAILED, EVT_SANDBOX_STATUS,
    )
    _OBS_OK = True
except Exception:
    _OBS_OK = False
    def emit_event(job_id: str, event_type: str, data: Any = None) -> None:  # type: ignore[misc]
        pass

try:
    from runtime.tool_policy import check_tool_policy
    _POLICY_OK = True
except Exception:
    _POLICY_OK = False
    def check_tool_policy(agent_slug: str, tool_name: str) -> dict:  # type: ignore[misc]
        return {"ok": True, "tool_name": tool_name, "agent_slug": agent_slug}

# PERMANENTLY_PROHIBITED enforcement (FINANCE-TOOL-CONTRACTS.md Section 6.2
# Layer 4). Deliberately NOT a tolerant import: the tolerant import above falls
# back to allowing everything, and a prohibition that can be disabled by an
# ImportError is not a prohibition. runtime.tool_policy is pure standard
# library, so this import cannot fail anywhere agent_runtime itself imports.
from runtime.per_agent_model import resolve_agent_model
from runtime.tool_policy import (  # noqa: E402
    RISK_DEPLOYMENT,
    RISK_READ_ONLY,
    assert_prohibitions_intact,
    is_prohibited,
    prohibition_group,
)
from tools._envelope import prohibited_refusal  # noqa: E402
from runtime.genesis_audit import append_tool_event, append_tool_intent

# Phase 3/6: durable session + relationship store (Postgres-backed, best-effort).
try:
    import durable_store
    _DURABLE_OK = True
except Exception:
    _DURABLE_OK = False
    durable_store = None  # type: ignore

log = logging.getLogger(__name__)

# Resource limits — Phase 11 sandbox enforcement.
MAX_TURNS = 10
MAX_LLM_CALLS = 10  # Same as MAX_TURNS but tracked explicitly for clarity.
MAX_TOKENS_PER_JOB = 50_000  # Aggregate over all turns (response.usage.total_tokens).
MAX_FILES_WRITTEN = 20  # Hard cap on file_write tool successes per job.
DEFAULT_TIMEOUT_S = 300  # 5 minutes wall-time.
DEFAULT_MAX_OUTPUT_BYTES = 4 * 1024 * 1024  # 4 MB per tool result.
DEFAULT_SWARMSYNC_MODEL = "auto"
DISALLOWED_LLM_HOST_MARKERS = ("openrouter.ai",)


def _llm_client_session(timeout: "Any") -> "Any":
    """Build the ClientSession every LLM call uses.

    Uses ThreadedResolver (the OS resolver via a thread pool) rather than
    aiohttp's default async resolver: on Windows the default resolver returns
    "Could not contact DNS servers" for hosts the OS resolves fine, which
    surfaced as errorCode=llm_call_failed on every agent run. Cato hit and
    fixed the identical defect — see cato/tools/genesis.py::_ensure_session.
    """
    import aiohttp

    connector = aiohttp.TCPConnector(
        resolver=aiohttp.ThreadedResolver(),
        family=0,  # IPv4 + IPv6 — let the OS pick
        ssl=True,
    )
    return aiohttp.ClientSession(timeout=timeout, connector=connector)


def _check_success_criteria(
    criteria: list[dict] | None,
    result: dict,
    *,
    require_tool_evidence: bool = False,
) -> dict[str, Any]:
    """Validate result against bundle's success_criteria. Returns {ok, failed: [...]}.

    Supported criteria types:
      - non_empty           : response must be non-empty.
      - contains_keys       : response (JSON-parseable) must contain `keys`.
      - max_latency_s       : result.elapsed_s must be <= configured seconds.
      - min_successful_tool_calls: trace must contain at least N successful calls.
    Unknown types are ignored (forward-compatible).
    """
    criteria = list(criteria or [{"type": "non_empty"}])
    if require_tool_evidence and not any(
        c.get("type") == "min_successful_tool_calls" for c in criteria
    ):
        # A bundle may add prose/shape/latency criteria, but it cannot opt out
        # of execution evidence while advertising tools.
        criteria.append({"type": "min_successful_tool_calls", "config": {"count": 1}})
    failed: list[dict[str, Any]] = []
    for c in criteria:
        ct = c.get("type")
        if ct == "non_empty":
            if not result.get("response"):
                failed.append({"type": ct, "reason": "response is empty"})
        elif ct == "contains_keys":
            keys = (c.get("config") or {}).get("keys", [])
            try:
                response_obj = json.loads(result.get("response") or "{}")
                if not isinstance(response_obj, dict):
                    failed.append({"type": ct, "reason": "response not a JSON object"})
                else:
                    for k in keys:
                        if k not in response_obj:
                            failed.append({"type": ct, "reason": f"missing key: {k}"})
            except Exception:
                failed.append({"type": ct, "reason": "response not parseable as JSON"})
        elif ct == "max_latency_s":
            max_s = (c.get("config") or {}).get("seconds", 300)
            elapsed = result.get("elapsed_s", 0) or 0
            if elapsed > max_s:
                failed.append({
                    "type": ct,
                    "reason": f"elapsed {elapsed}s > {max_s}s",
                })
        elif ct == "min_successful_tool_calls":
            minimum = int((c.get("config") or {}).get("count", 1))
            calls = ((result.get("trace") or {}).get("tool_calls") or [])
            successful = sum(1 for call in calls if call.get("ok") is True)
            if successful < minimum:
                failed.append({
                    "type": ct,
                    "reason": f"successful tool calls {successful} < {minimum}",
                })
    return {"ok": len(failed) == 0, "failed": failed}


def _task_requires_tool_evidence(
    params: dict[str, Any] | None,
    tool_call_records: list[dict[str, Any]] | None = None,
) -> bool:
    """Require execution evidence only when this task actually opted into it.

    ``semantically_required_tools`` describes a profile's tool-capable work; it
    is not permission to manufacture a write or shell action for an otherwise
    read-only question. The request must use an explicit boolean opt-in. If a
    tool was called anyway, its result still becomes completion evidence and a
    failed call cannot be hidden behind a prose answer.

    Strict ``is True`` checks are intentional: strings such as ``"true"``,
    numbers, and non-empty collections are ambiguous network input and must not
    silently authorize side effects.
    """
    task_params = params if isinstance(params, dict) else {}
    explicit = any(
        task_params.get(name) is True
        for name in (
            "require_tool_evidence",
            "require_artifact",
            "tool_execution_required",
        )
    )
    return explicit or bool(tool_call_records)


def _canonical_output_contract(bundle: dict[str, Any], content: Any) -> dict[str, Any]:
    """Validate and normalize the final answer for canonical bundles only.

    A single complete JSON code fence is a presentation wrapper, so it may be
    removed deterministically. Nothing else is repaired or synthesized: prose,
    partial JSON, arrays, nested/multiple fences, and missing required fields
    fail closed with content-free metadata.
    """
    required_keys = tuple(str(key) for key in (bundle.get("output_shape_hint") or []))
    required = bool(bundle.get("canonical_profile") and required_keys)
    if not required:
        return {
            "required": False,
            "ok": True,
            "normalized_code_fence": False,
            "missing_keys": [],
            "reason": None,
            "response": content,
        }

    # Current canonical profiles use the single shared typed contract.  The
    # small legacy branch below remains for non-production test fixtures and
    # old non-versioned bundles; it performs only the historical shape check.
    if bundle.get("output_contract_version") == CANONICAL_RESULT_CONTRACT_VERSION:
        slug = str(bundle.get("slug") or "")
        parsed = parse_and_validate_canonical_result(slug, content)
        response = (
            json.dumps(parsed.value, ensure_ascii=False, separators=(",", ":"))
            if parsed.value is not None
            else (content if isinstance(content, str) else "")
        )
        return {
            "required": True,
            "ok": parsed.ok,
            "normalized_code_fence": parsed.normalized_code_fence,
            "missing_keys": list(parsed.missing_fields),
            "reason": parsed.reason,
            "response": response,
        }

    candidate = content if isinstance(content, str) else ""
    stripped = candidate.strip()
    normalized_fence = False
    lines = stripped.splitlines()
    if len(lines) >= 3 and lines[0].strip().lower() in {"```json", "```"} and lines[-1].strip() == "```":
        body = "\n".join(lines[1:-1]).strip()
        if "```" not in body:
            stripped = body
            normalized_fence = True

    try:
        parsed = json.loads(stripped)
    except Exception:
        parsed = None
        reason = "response_not_json_object"
    else:
        reason = None if isinstance(parsed, dict) else "response_not_json_object"

    if not isinstance(parsed, dict):
        return {
            "required": True,
            "ok": False,
            "normalized_code_fence": normalized_fence,
            "missing_keys": list(required_keys),
            "reason": reason,
            "response": stripped,
        }

    missing = [key for key in required_keys if key not in parsed]
    return {
        "required": True,
        "ok": not missing,
        "normalized_code_fence": normalized_fence,
        "missing_keys": missing,
        "reason": "missing_required_keys" if missing else None,
        "response": json.dumps(parsed, ensure_ascii=False, separators=(",", ":")),
    }


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


def _contract_correction(reason: Any, failing_fields: Any) -> str:
    """Content-free corrective instruction for a contract-stage rejection.

    Mirrors the canonical live-test path in main.py: the validator's own
    reason and failing field names (never candidate content) are fed back so
    a single stochastic phrasing failure gets exactly one repair attempt.
    """
    reason_text = str(reason or "unknown")
    fields = [str(field) for field in (failing_fields or [])]
    field_text = ", ".join(fields) if fields else "a narrative field"
    hints = {
        "finance_execution_claim": (
            "The validator rejects narrative that asserts or directs finance "
            "execution (a finance action verb -- pay, post, issue, execute, "
            "run, file, submit, transfer, move, refund, approve -- joined to "
            "a finance object -- payment, invoice, journal, bill, funds, "
            "refund, payroll, tax filing, Xero) when it is not framed as a "
            "proposal, recommendation, or plan step, and it rejects every "
            "immediacy directive ('now', 'immediately', 'do not wait'). Frame "
            "each action step explicitly as a recommendation or as a step of "
            "a stated plan with no immediacy wording, or use nouns only with "
            "no action verb when an item must mention a finance object (safe "
            "pattern: 'UNKNOWN: Invoice and payment terms absent from the "
            "request.'). Never state that a finance action occurred."
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
        reason_text,
        "Resubmit a fully compliant Genesis V5 result: prefix every "
        "narrative string and array item with its exact intent prefix, "
        "keep every required field, and keep all narrative analysis-only.",
    )
    return (
        "CORRECTION: your previous result submission was rejected by the "
        f"deterministic Genesis V5 contract validator (reason: {reason_text}; "
        f"field: {field_text}). {hint} Submit exactly one complete, "
        f"compliant V5 result tool call. {_CLARIFICATION_REPRESENTATION_FOOTER}"
    )


def _clarification_representation_metadata(submission: Any) -> dict[str, Any]:
    """Return only contract-owned, allowlisted provider-shape diagnostics."""
    return {
        "clarification_representation_subtype": getattr(
            submission, "representation_subtype", None,
        ),
        "clarification_representation_missing_fields": list(getattr(
            submission, "representation_missing_fields", (),
        )),
        "clarification_representation_missing_field_count": int(getattr(
            submission, "representation_missing_field_count", 0,
        )),
        "clarification_representation_extra_fields": list(getattr(
            submission, "representation_extra_fields", (),
        )),
        "clarification_representation_extra_field_count": int(getattr(
            submission, "representation_extra_field_count", 0,
        )),
    }


def _deployment_side_effect_authorized(params: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    """Strip the signed action grant and owner context before model exposure."""
    clean = dict(params or {})
    context = {
        "grant": str(clean.pop("_action_grant", "") or ""),
        "principal_id": str(clean.pop("_request_principal_id", "") or ""),
        "tenant_id": str(clean.pop("_request_tenant_id", "") or ""),
    }
    # Reject and strip the obsolete static credential rather than supporting a
    # compatibility path that recreates bearer-token deployment authority.
    clean.pop("_deployment_approval_token", None)
    # W3C propagation belongs to the runtime, never to model-visible params.
    # Discard retired carrier metadata from jobs queued before decommissioning.
    clean.pop("_phoenix_trace_context", None)
    return context, clean

# Lazy module init
_DEFAULTS_REGISTERED = False


def _ensure_tools_registered() -> None:
    global _DEFAULTS_REGISTERED
    if not _DEFAULTS_REGISTERED:
        register_default_tools()
        # Layer 2 — fail to boot, not fail to deny. If a prohibited tool has
        # been re-registered, or the frozen manifest no longer matches the
        # prohibition list, this raises and the runtime does not come up.
        assert_prohibitions_intact()
        _DEFAULTS_REGISTERED = True


class AgentRuntime:
    """Runs a single agent invocation."""

    def __init__(self, llm_url: str, llm_key: str):
        self.llm_url = llm_url
        self.llm_key = llm_key
        # Cumulative resources already consumed by DELEGATED children, keyed by the
        # delegating parent's job_id. Without this, a parent that calls genesis_call N times
        # hands each child the same "remaining" budget: the parent's own total_tokens only
        # counts its OWN turns, and conduit_budget_cents was passed as the parent's full
        # configured ceiling rather than what is left. Nine siblings therefore inherited
        # 9x the ceiling and the delegation budget was advisory, not enforced.
        self._delegated_tokens_spent: dict[str, int] = {}
        self._delegated_cents_spent: dict[str, int] = {}
        _ensure_tools_registered()

    def remaining_delegation_budget(
        self, job_id: str, *, token_budget: int, total_tokens: int, cost_budget_cents: int
    ) -> tuple[int, int]:
        """What is genuinely LEFT for the next delegated child of this job.

        Extracted from the tool-dispatch context so the per-tree (not per-child) ceiling is
        directly testable — the bug it fixes is invisible in any single call.
        """
        spent_tokens = self._delegated_tokens_spent.get(job_id, 0)
        spent_cents = self._delegated_cents_spent.get(job_id, 0)
        return (
            max(0, int(token_budget) - int(total_tokens) - spent_tokens),
            max(0, int(cost_budget_cents) - spent_cents),
        )

    def record_delegated_spend(
        self, parent_job_id: str | None, *, tokens: int = 0, cents: int = 0
    ) -> None:
        """Charge a child's resource use back to the delegating parent's remaining budget."""
        if not parent_job_id:
            return
        if tokens:
            self._delegated_tokens_spent[parent_job_id] = (
                self._delegated_tokens_spent.get(parent_job_id, 0) + max(0, int(tokens))
            )
        if cents:
            self._delegated_cents_spent[parent_job_id] = (
                self._delegated_cents_spent.get(parent_job_id, 0) + max(0, int(cents))
            )

    async def execute_agent(
        self,
        slug: str,
        task: str,
        params: dict[str, Any],
        *,
        job_id: Optional[str] = None,
        session_id: Optional[str] = None,
        parent_job_id: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        delegation_chain: tuple[str, ...] = (),
        delegation_depth: int = 0,
        delegated_allowed_risks: frozenset[str] | None = None,
        inherited_token_budget: int | None = None,
        inherited_cost_budget_cents: int | None = None,
    ) -> dict[str, Any]:
        """Execute an agent with explicit delegation and budget boundaries."""
        kwargs: dict[str, Any] = {
            "job_id": job_id,
            "session_id": session_id,
            "parent_job_id": parent_job_id,
            "parent_session_id": parent_session_id,
            "delegation_chain": delegation_chain,
            "delegation_depth": delegation_depth,
            "delegated_allowed_risks": delegated_allowed_risks,
            "inherited_token_budget": inherited_token_budget,
            "inherited_cost_budget_cents": inherited_cost_budget_cents,
        }
        result = await self._execute_agent_inner(slug, task, params, **kwargs)
        return result

    async def _execute_agent_inner(
        self,
        slug: str,
        task: str,
        params: dict[str, Any],
        *,
        job_id: Optional[str] = None,
        session_id: Optional[str] = None,
        parent_job_id: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        delegation_chain: tuple[str, ...] = (),
        delegation_depth: int = 0,
        delegated_allowed_risks: frozenset[str] | None = None,
        inherited_token_budget: int | None = None,
        inherited_cost_budget_cents: int | None = None,
    ) -> dict[str, Any]:
        """Execute one agent invocation. Returns structured result."""
        bundle = load_bundle(slug)
        if bundle is None:
            return {"ok": False, "error": "unknown_slug", "slug": slug}

        job_id = job_id or f"job-{uuid.uuid4().hex[:12]}"
        session_id = session_id or str(uuid.uuid4())

        bundle = dict(bundle)
        from accounting.specialists import is_e4l_specialist

        if is_e4l_specialist(str(bundle.get("slug") or slug)):
            try:
                from accounting.runtime_context import enrich_bundle

                bundle = enrich_bundle(bundle, params or {}, task or "")
            except Exception:
                log.exception("accounting entity pack load failed for %s", slug)
                return {
                    "ok": False,
                    "error": "accounting_entity_load_failed",
                    "slug": slug,
                    "job_id": job_id,
                }
        if inherited_token_budget is not None:
            bundle["token_budget"] = min(
                int(bundle.get("token_budget", inherited_token_budget)),
                max(0, int(inherited_token_budget)),
            )
        if inherited_cost_budget_cents is not None:
            bundle["conduit_budget_cents"] = min(
                int(bundle.get("conduit_budget_cents", inherited_cost_budget_cents)),
                max(0, int(inherited_cost_budget_cents)),
            )

        # Phase 2/6: Register workspace and set initial sandbox status
        if _WS_MANAGER_OK:
            ws = create_workspace(job_id, session_id)
            job_dir = ws.path
            set_workspace_status(job_id, "ACTIVE")
        else:
            job_dir = Path(f"/tmp/jobs/{job_id}")
            job_dir.mkdir(parents=True, exist_ok=True)

        # Phase 3: durable session record (survives restart; retrievable via
        # GET /agents/sessions/{id}). Best-effort — never blocks the job.
        if _DURABLE_OK:
            try:
                durable_store.session_create(
                    session_id=session_id,
                    job_id=job_id,
                    agent_slug=slug,
                    parent_job_id=parent_job_id,
                    parent_session_id=parent_session_id,
                    workspace_root=str(job_dir),
                )
            except Exception:
                log.debug("durable session_create failed for %s", job_id, exc_info=True)

        # Phase 4: emit job.created
        emit_event(job_id, EVT_JOB_CREATED if _OBS_OK else "job.created", {
            "session_id": session_id,
            "agent_slug": slug,
        })

        # Lazy-init Conduit bridge per job (only if conduit is in tools_advertised)
        bridge = None
        buyer_session: Any = None
        if "conduit" in bundle.get("tools_advertised", []):
            try:
                from conduit_browser import ConduitBridge
                bridge = ConduitBridge(
                    session_id=job_id,
                    budget_cents=bundle.get("conduit_budget_cents", 200),
                    data_dir=job_dir / "conduit",
                )
                # Phase 9b - if a buyer uploaded a Conduit session for this
                # job ("Concierge Mode"), pull it from the encrypted vault now
                # and inject it into the bridge AFTER start() (Playwright
                # context must exist before add_cookies will accept anything).
                try:
                    from conduit_sessions import load_session
                    sess_result = load_session(job_id=job_id)
                    if sess_result.get("ok") and sess_result.get("session_data"):
                        # The local Patchright bridge has no reviewed cookie
                        # import API. Refuse the entire job instead of silently
                        # dropping a buyer credential and claiming success.
                        from conduit_sessions import delete_session
                        delete_session(job_id=job_id)
                        return {
                            "ok": False,
                            "error": "buyer_session_injection_unsupported",
                            "slug": slug,
                            "job_id": job_id,
                        }
                        log.info(
                            "loading buyer session for job %s (concierge mode)",
                            job_id,
                        )
                except Exception:
                    log.exception("session load failed; continuing without buyer session")

                # Lazy browser: only launch Chromium up-front when we must
                # inject a buyer session (concierge mode). Otherwise the browser
                # starts on first conduit tool call (ensure_started), so jobs
                # that never browse use ZERO browser memory. This + the stop()
                # fix below prevents the per-job Chromium leak that OOM'd the
                # instance.
                if buyer_session is not None:
                    await bridge.start()
                    try:
                        # Conduit's session-import API is the BrowserTool's
                        # cookie-jar label system: write the cookie array as
                        # a label file under the bridge's _session_dir, then
                        # call ConduitBridge.load_cookies(label=...) which
                        # internally invokes Playwright's
                        # `BrowserContext.add_cookies(cookies)` via
                        # BrowserTool._load_cookies. Audit event is recorded
                        # by the bridge as part of the call.
                        #
                        # Accepted buyer formats:
                        #   - Playwright storage_state dict:
                        #       {"cookies": [...], "origins": [...]}
                        #     (origins/localStorage not yet wired; cookies only.)
                        #   - Raw cookie array: [{name, value, domain, ...}, ...]
                        if isinstance(buyer_session, dict):
                            cookies_list = buyer_session.get("cookies", [])
                        elif isinstance(buyer_session, list):
                            cookies_list = buyer_session
                        else:
                            cookies_list = []

                        if cookies_list:
                            browser_tool = getattr(bridge, "_browser_tool", None)
                            if browser_tool is None or getattr(browser_tool, "_session_dir", None) is None:
                                raise RuntimeError("bridge._browser_tool not initialised after start()")
                            label = "buyer"
                            session_file = browser_tool._session_dir / f"{label}.json"
                            session_file.parent.mkdir(parents=True, exist_ok=True)
                            session_file.write_text(
                                json.dumps(cookies_list), encoding="utf-8"
                            )
                            inject_result = await bridge.load_cookies(label=label)
                            if not (inject_result or {}).get("success"):
                                raise RuntimeError(
                                    f"load_cookies returned non-success: {inject_result}"
                                )
                            log.info(
                                "buyer session injected into bridge for job %s (cookies=%d)",
                                job_id,
                                inject_result.get("count", len(cookies_list)),
                            )
                        else:
                            log.warning(
                                "buyer session for job %s had no cookies; nothing injected",
                                job_id,
                            )
                    except Exception:
                        log.exception(
                            "buyer session injection failed for job %s; continuing without",
                            job_id,
                        )
            except Exception:
                log.exception("ConduitBridge failed to start for %s", slug)
                bridge = None

        _result: dict[str, Any] | None = None
        try:
            _result = await self._run_loop(
                bundle, task, params, job_id, job_dir, bridge, session_id,
                delegation_chain=delegation_chain,
                delegation_depth=delegation_depth,
                delegated_allowed_risks=delegated_allowed_risks,
            )
            return _result
        finally:
            if bridge is not None:
                try:
                    await bridge.stop()
                except Exception:
                    log.warning("bridge.stop() failed for %s", job_id)
            # Phase 3: finalize the durable session for every return path
            # (success, failure, timeout, exception). Best-effort.
            if _DURABLE_OK:
                try:
                    _ok = bool(_result and _result.get("ok"))
                    durable_store.session_finish(
                        session_id,
                        status="COMPLETED" if _ok else "FAILED",
                        trace=(_result or {}).get("trace"),
                        error=None if _ok else (_result or {}).get("error"),
                    )
                except Exception:
                    log.debug("durable session_finish failed for %s", job_id, exc_info=True)
            # Concierge cleanup: delete buyer session from vault after job
            # completion so credentials don't accumulate on disk. Best-effort.
            try:
                from conduit_sessions import delete_session
                delete_session(job_id=job_id)
            except Exception:
                log.warning("session cleanup failed for job %s", job_id)

    async def _run_loop(
        self,
        bundle: dict[str, Any],
        task: str,
        params: dict[str, Any],
        job_id: str,
        job_dir: Path,
        bridge: Any,
        session_id: str = "",
        *,
        delegation_chain: tuple[str, ...] = (),
        delegation_depth: int = 0,
        delegated_allowed_risks: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Trace the complete multi-turn chain while preserving the loop contract."""
        result = await self._run_loop_inner(
            bundle,
            task,
            params,
            job_id,
            job_dir,
            bridge,
            session_id,
            delegation_chain=delegation_chain,
            delegation_depth=delegation_depth,
            delegated_allowed_risks=delegated_allowed_risks,
        )
        return result

    async def _run_loop_inner(
        self,
        bundle: dict[str, Any],
        task: str,
        params: dict[str, Any],
        job_id: str,
        job_dir: Path,
        bridge: Any,
        session_id: str = "",
        *,
        delegation_chain: tuple[str, ...] = (),
        delegation_depth: int = 0,
        delegated_allowed_risks: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        slug = bundle["slug"]
        last_swarmsync: dict[str, Any] | None = None
        tools_advertised = list(bundle.get("tools_advertised", []))
        try:
            from accounting.xero_scope import augment_tools_advertised

            tools_advertised = augment_tools_advertised(slug, tools_advertised)
        except Exception:
            pass
        token_budget = bundle.get("token_budget", 4000)
        model = bundle.get("model_hint", "anthropic/claude-sonnet-4-5")
        timeout_s = bundle.get("timeout_s", DEFAULT_TIMEOUT_S)

        # Phase 4: agent.started
        emit_event(job_id, "agent.started", {"session_id": session_id, "agent_slug": slug})

        system_prompt = bundle["system_prompt"]
        deployment_context, safe_params = _deployment_side_effect_authorized(params)
        user_prompt = task + (f"\n\nAdditional params: {json.dumps(safe_params)}" if safe_params else "")

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        # Denied/prohibited schemas never enter the model prompt. This prevents
        # wasted turns and closes the gap where the dispatcher denied a tool
        # only after the LLM had already been invited to call it.
        model_tools = [name for name in tools_advertised if check_tool_policy(slug, name)["ok"]]
        tools = tool_schemas_for(model_tools)
        forced_result_submission = bool(
            bundle.get("canonical_profile")
            and bundle.get("output_contract_version") == CANONICAL_RESULT_CONTRACT_VERSION
        )
        if forced_result_submission:
            native_ready = True
            try:
                require_canonical_native_anthropic_config(
                    api_url=self.llm_url,
                    api_key=self.llm_key,
                    declared_provider=os.getenv("GENESIS_LLM_PROVIDER"),
                )
            except RuntimeError:
                native_ready = False
            router_ready = router_fallback_allowed(
                api_url=self.llm_url,
                api_key=self.llm_key,
                declared_provider=os.getenv("GENESIS_LLM_PROVIDER"),
            )
            if not native_ready and not router_ready:
                return {
                    "ok": False,
                    "error": "output_contract_failed",
                    "message": REASON_CANDIDATE_TRANSPORT_FAILED,
                    "slug": slug,
                    "response": "",
                    "output_contract_eval": {
                        "ok": False,
                        "reason": REASON_CANDIDATE_TRANSPORT_FAILED,
                    },
                }
        provider_tools = [canonical_result_tool_schema(slug)] if forced_result_submission else tools

        started = time.time()
        turn = 0
        llm_calls = 0
        total_tokens = 0
        files_written = 0
        audit_integrity_failed = False
        tool_integrity_failed = False
        orchestration_integrity_failed = False
        success_criteria = bundle.get("success_criteria")

        tool_call_records: list[dict[str, Any]] = []
        tool_call_id_signatures: dict[str, str] = {}
        tool_call_results: dict[str, tuple[str, dict[str, Any]]] = {}
        # Phase 5: separate subagent trace list
        subagent_records: list[dict[str, Any]] = []
        # Canonical contract rejections get exactly one corrective retry on
        # this path too (mirror of the live-test/persona path in main.py).
        contract_retry_used = False

        while turn < MAX_TURNS:
            turn += 1
            if time.time() - started > timeout_s:
                return {
                    "ok": False,
                    "error": "timeout",
                    "slug": slug,
                    "turns_completed": turn - 1,
                }

            # Phase 11 — enforce LLM call cap (parallel to MAX_TURNS, makes
            # the limit explicit and easier to audit).
            if llm_calls >= MAX_LLM_CALLS:
                return {
                    "ok": False,
                    "error": "llm_call_limit_exceeded",
                    "slug": slug,
                    "llm_calls": llm_calls,
                    "limit": MAX_LLM_CALLS,
                }

            # Call LLM
            try:
                response = await self._call_llm(
                    model, messages, provider_tools, token_budget, slug=slug
                )
                llm_calls += 1
                if isinstance(response.get("swarmsync"), dict):
                    last_swarmsync = response["swarmsync"]
            except Exception as e:
                log.exception("LLM call failed turn=%d slug=%s", turn, slug)
                return {
                    "ok": False,
                    "error": "llm_call_failed",
                    "type": type(e).__name__,
                    "message": str(e),
                }

            # Phase 11 — aggregate token-budget enforcement.
            try:
                usage = response.get("usage") or {}
                total_tokens += int(usage.get("total_tokens", 0) or 0)
            except Exception:
                pass
            if total_tokens > MAX_TOKENS_PER_JOB:
                return {
                    "ok": False,
                    "error": "token_budget_exceeded",
                    "slug": slug,
                    "total_tokens": total_tokens,
                    "limit": MAX_TOKENS_PER_JOB,
                }

            # Parse response - OpenAI-format expected
            choices = response.get("choices", [])
            if not choices:
                return {"ok": False, "error": "no_choices_in_llm_response"}

            msg = choices[0].get("message", {})
            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")

            forced_contract_eval: dict[str, Any] | None = None
            submission_call_id: str | None = None
            if forced_result_submission:
                submission_call_id = next(
                    (
                        tc.get("id")
                        for tc in tool_calls
                        if (tc.get("function") or {}).get("name")
                        == CANONICAL_RESULT_TOOL_NAME
                    ),
                    None,
                )
                submission = consume_canonical_result_submission(slug, response)
                representation_metadata = _clarification_representation_metadata(submission)
                if submission.ok and submission.value is not None:
                    candidate_sha_before = canonical_sha256(submission.value)
                    judgment_started = time.monotonic()
                    if response.get("model") != CANDIDATE_MODEL:
                        judgment = None
                        judgment_reason = "semantic_judge_model_mismatch"
                    else:
                        async def _judge_transport(request: dict[str, Any]) -> dict[str, Any]:
                            declared_provider = os.getenv("GENESIS_LLM_PROVIDER")
                            try:
                                require_canonical_native_anthropic_config(
                                    api_url=self.llm_url,
                                    api_key=self.llm_key,
                                    declared_provider=declared_provider,
                                )
                                native_judge = True
                            except RuntimeError:
                                native_judge = False
                            if native_judge:
                                return await call_exact_anthropic_judge(
                                    request, api_url=self.llm_url, api_key=self.llm_key
                                )
                            if router_fallback_allowed(
                                api_url=self.llm_url,
                                api_key=self.llm_key,
                                declared_provider=declared_provider,
                            ):
                                return await call_router_judge(
                                    request, api_url=self.llm_url, api_key=self.llm_key
                                )
                            raise RuntimeError("semantic_judge_transport_failed")

                        judgment = await judge_candidate_semantics(
                            submission.value, transport=_judge_transport
                        )
                        judgment_reason = judgment.reason
                    judgment_elapsed_ms = round(
                        (time.monotonic() - judgment_started) * 1000, 3,
                    )
                    deterministic = None
                    deterministic_reason = None
                    deterministic_failing_fields: tuple[str, ...] = ()
                    if judgment_reason is None:
                        deterministic = validate_canonical_result_semantics(
                            slug, submission.value,
                        )
                        deterministic_reason = deterministic.reason
                        deterministic_failing_fields = deterministic.failing_fields
                    candidate_sha_after = canonical_sha256(submission.value)
                    if candidate_sha_after != candidate_sha_before:
                        deterministic_reason = "canonical_candidate_mutated"
                    disagreement = bool(
                        judgment_reason is None and deterministic_reason is not None
                    )
                    terminal_reason = judgment_reason or deterministic_reason
                    if terminal_reason is None:
                        content = json.dumps(
                            submission.value, ensure_ascii=False, separators=(",", ":")
                        )
                    else:
                        content = ""
                    forced_contract_eval = {
                        "required": True,
                        "ok": terminal_reason is None,
                        "normalized_code_fence": False,
                        "missing_keys": [],
                        "reason": terminal_reason,
                        "structure_ok": True,
                        "structure_reason": None,
                        "judge_decision": judgment.decision if judgment else None,
                        "judge_reason": judgment_reason,
                        "deterministic_ok": deterministic.ok if deterministic else None,
                        "deterministic_reason": deterministic_reason,
                        "deterministic_failing_fields": list(deterministic_failing_fields),
                        "deterministic_failing_field_count": len(deterministic_failing_fields),
                        "semantic_disagreement": disagreement,
                        "candidate_sha256": candidate_sha_before,
                        "projection_sha256": judgment.projection_sha256 if judgment else None,
                        **representation_metadata,
                    }
                else:
                    content = ""
                    forced_contract_eval = {
                        "required": True,
                        "ok": False,
                        "normalized_code_fence": False,
                        "missing_keys": [],
                        "reason": submission.reason,
                        "structure_ok": False,
                        "structure_reason": submission.reason,
                        "judge_decision": None,
                        "judge_reason": None,
                        "deterministic_ok": None,
                        "deterministic_reason": None,
                        "semantic_disagreement": False,
                        **representation_metadata,
                    }
                # The reserved provider tool is a response envelope, never a
                # business action, dispatch candidate, or evidence-bearing call.
                tool_calls = []

            if not tool_calls:
                # Final answer
                _finished_at = time.time()
                if forced_contract_eval is None:
                    output_contract = _canonical_output_contract(bundle, content)
                    content = output_contract.pop("response")
                else:
                    output_contract = forced_contract_eval
                if not output_contract["ok"] and output_contract.get("required"):
                    # Mirror of the canonical live-test path (main.py): a
                    # single stochastic candidate can trip a fail-closed
                    # contract stage on phrasing alone. Retry exactly once
                    # with the validator's own feedback; the retry must pass
                    # every stage unchanged, and a second rejection fails
                    # the job with the identical contract metadata.
                    # Transport failures never reach the contract stages,
                    # and a judge model mismatch cannot be repaired by
                    # rephrasing, so neither is retried.
                    rejection_reason = output_contract.get("reason")
                    if forced_contract_eval is not None and (
                        forced_contract_eval.get("judge_reason")
                        == "semantic_judge_model_mismatch"
                    ):
                        rejection_reason = None
                    if rejection_reason is not None:
                        if (
                            not contract_retry_used
                            and turn < MAX_TURNS
                            and llm_calls < MAX_LLM_CALLS
                        ):
                            contract_retry_used = True
                            stage = (
                                "submission"
                                if not output_contract.get("structure_ok", True)
                                else "judge"
                                if output_contract.get("judge_reason")
                                else "deterministic"
                            )
                            log.warning(
                                "canonical contract rejection slug=%s stage=%s "
                                "reason=%s attempt=1; retrying once with "
                                "validator feedback",
                                slug, stage, rejection_reason,
                            )
                            correction = _contract_correction(
                                rejection_reason,
                                output_contract.get(
                                    "deterministic_failing_fields"
                                ) or [],
                            )
                            messages.append(msg)
                            if submission_call_id:
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": submission_call_id,
                                    "content": json.dumps({
                                        "ok": False,
                                        "error": "output_contract_failed",
                                        "reason": rejection_reason,
                                        "correction": correction,
                                    }),
                                })
                            else:
                                messages.append({
                                    "role": "user",
                                    "content": correction,
                                })
                            continue
                        log.warning(
                            "canonical contract rejection slug=%s reason=%s "
                            "attempt=%d; failing job",
                            slug, rejection_reason,
                            2 if contract_retry_used else 1,
                        )
                result: dict[str, Any] = {
                    "ok": True,
                    "slug": slug,
                    "response": content,
                    "transport": response.get("transport") if isinstance(response, dict) else None,
                    "turns": turn,
                    "elapsed_s": round(_finished_at - started, 2),
                    "job_id": job_id,
                    "resource_usage": {
                        "llm_calls": llm_calls,
                        "total_tokens": total_tokens,
                        "files_written": files_written,
                    },
                    "trace": {
                        "job_id": job_id,
                        "session_id": session_id,
                        "agent_slug": slug,
                        "workspace_path": str(job_dir),
                        "artifact_count": files_written,
                        "tool_calls": tool_call_records,
                        "subagents": subagent_records,
                        "started_at": started,
                        "finished_at": _finished_at,
                        "status": "ok",
                    },
                }
                result["output_contract_eval"] = output_contract
                if last_swarmsync:
                    result["swarmsync"] = last_swarmsync
                    routed = last_swarmsync.get("routed_model") or ""
                    result["routing"] = {
                        "model": routed,
                        "provider": routed.split("/")[0] if "/" in routed else routed,
                        "tier": last_swarmsync.get("tier"),
                        "routing_reason": last_swarmsync.get("routing_reason"),
                        "estimated_cost": last_swarmsync.get("estimated_cost"),
                        "latency_ms": last_swarmsync.get("latency_ms"),
                    }

                # Phase 11 — validate success_criteria against the structured
                # result. If any fail, mark the job FAILED so the worker can
                # refund escrow (Phase 6) and reputation tracking updates.
                requires_tool_evidence = _task_requires_tool_evidence(
                    params, tool_call_records
                )
                # Policy visibility and semantic necessity are distinct.
                # Optional artifact tools must not force meaningless writes;
                # task/profile contracts opt in explicitly.
                criteria_eval = _check_success_criteria(
                    success_criteria,
                    result,
                    require_tool_evidence=requires_tool_evidence,
                )
                result["success_criteria_eval"] = criteria_eval
                if not criteria_eval["ok"]:
                    result["ok"] = False
                    result["error"] = "success_criteria_failed"
                if not output_contract["ok"]:
                    result["ok"] = False
                    result["error"] = "output_contract_failed"
                if tool_integrity_failed and any(
                    call.get("ok") is True for call in tool_call_records
                ):
                    result["ok"] = False
                    result["error"] = "tool_call_failed"
                if audit_integrity_failed:
                    result["ok"] = False
                    result["error"] = "audit_integrity_failed"
                if orchestration_integrity_failed:
                    result["ok"] = False
                    result["error"] = "subagent_failed"
                result["trace"]["status"] = "ok" if result["ok"] else "failed"

                # Phase 4/6: emit completion event + transition sandbox
                _final_evt = "job.completed" if result["ok"] else "job.failed"
                emit_event(job_id, _final_evt, {
                    "session_id": session_id,
                    "status": result["trace"]["status"],
                    "turns": turn,
                    "elapsed_s": result.get("elapsed_s"),
                })
                if _WS_MANAGER_OK:
                    set_workspace_status(job_id, "FINALIZING")

                # Phase 7 — generate VCAP proof bundle only if the browser was
                # actually used (started). Skipping when unused avoids launching
                # Chromium just to produce a proof for a non-browser job.
                if bridge is not None and getattr(bridge, "is_started", lambda: True)():
                    try:
                        from proof_bridge import generate_proof_for_job
                        proof = await generate_proof_for_job(
                            job_id=job_id,
                            agent_slug=slug,
                            bridge=bridge,
                            job_dir=job_dir,
                            input_data={"task": task, "params": params},
                            output_data={"response": content, "turns": turn},
                            started_at=started,
                            completed_at=time.time(),
                        )
                        if proof.get("ok"):
                            result["proof"] = {
                                "proof_id": proof.get("proof_id"),
                                "vcap_wrapper_jwt": proof.get("vcap_wrapper_jwt"),
                                "proof_bundle_signed_url": proof.get("signed_url"),
                                "input_hash": proof.get("input_hash"),
                                "output_hash": proof.get("output_hash"),
                            }
                        else:
                            result["proof"] = {
                                "ok": False,
                                "error": proof.get("error"),
                            }
                    except Exception:
                        log.exception("proof generation raised; continuing without proof")
                        result["proof"] = {
                            "ok": False,
                            "error": "proof_pipeline_exception",
                        }

                return result

            # Append the assistant message to history
            messages.append(msg)

            # Execute each tool call
            for tc in tool_calls:
                tc_id = tc.get("id", "unknown")
                fn_name = tc.get("function", {}).get("name", "")
                raw_args = tc.get("function", {}).get("arguments", "{}")
                argument_error: dict[str, Any] | None = None
                try:
                    args = json.loads(raw_args)
                except Exception:
                    args = None
                    argument_error = {
                        "ok": False,
                        "error": "invalid_tool_arguments",
                        "tool": fn_name,
                        "reason": "malformed_json",
                    }
                if (
                    argument_error is None
                    and fn_name in tools_advertised
                    and get_tool(fn_name) is not None
                    and not is_prohibited(fn_name, slug)
                ):
                    argument_error = validate_tool_arguments(fn_name, args)

                signature = json.dumps(
                    {"tool": fn_name, "arguments": args},
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                duplicate_of: str | None = None
                if tc_id and tc_id != "unknown":
                    previous_signature = tool_call_id_signatures.get(tc_id)
                    if previous_signature is not None and previous_signature != signature:
                        argument_error = {
                            "ok": False,
                            "error": "tool_call_id_conflict",
                            "tool": fn_name,
                            "tool_call_id": tc_id,
                        }
                    else:
                        tool_call_id_signatures[tc_id] = signature
                cached = tool_call_results.get(signature)
                if cached is not None and argument_error is None:
                    duplicate_of, cached_result = cached

                tc_started = time.time()
                tool_result: dict[str, Any]
                tool_executed = False
                _policy: dict[str, Any] = {}
                # Layer 4 — prohibition pre-check. Runs BEFORE check_tool_policy
                # and before any tool is invoked, and is independent of TOOL_RISK
                # and slug risk grants, so a mistake in the risk table cannot
                # reach a prohibited tool. This is also the only place slug-scoped
                # prohibition is evaluated.
                tool = None if is_prohibited(fn_name, slug) else get_tool(fn_name)
                if is_prohibited(fn_name, slug):
                    # Reaching here means Layers 1-3 have failed. Severity is
                    # critical on purpose: this must page.
                    log.critical(
                        "PROHIBITED TOOL REACHED DISPATCHER: tool=%s slug=%s job=%s",
                        fn_name, slug, job_id,
                    )
                    emit_event(job_id, "tool.prohibited", {
                        "tool_name": fn_name,
                        "agent_slug": slug,
                        "severity": "critical",
                        "prohibition_group": prohibition_group(fn_name),
                        "session_id": session_id,
                    })
                    tool_result = prohibited_refusal(
                        fn_name,
                        group=prohibition_group(fn_name),
                        agent_slug=slug,
                    )
                elif tool is None or fn_name not in tools_advertised:
                    tool_result = {
                        "ok": False,
                        "error": "tool_not_allowed",
                        "tool": fn_name,
                    }
                elif duplicate_of is not None:
                    tool_result = dict(cached_result)
                    tool_result["deduplicated"] = True
                    tool_result["duplicate_of_tool_call_id"] = duplicate_of
                elif argument_error is not None:
                    tool_result = argument_error
                else:
                    # Phase 8: policy check before execution
                    _policy = check_tool_policy(slug, fn_name)
                    dispatch_allowed = True
                    deployment_authorized = False
                    if not _policy["ok"]:
                        emit_event(job_id, "tool.blocked", {
                            "tool_name": fn_name,
                            "agent_slug": slug,
                            "risk_class": _policy.get("risk_class"),
                            "session_id": session_id,
                        })
                        tool_result = {
                            "ok": False,
                            "error": "tool_policy_denied",
                            "tool": fn_name,
                            "risk_class": _policy.get("risk_class"),
                        }
                        dispatch_allowed = False
                    elif (
                        delegated_allowed_risks is not None
                        and _policy.get("risk_class") not in delegated_allowed_risks
                    ):
                        tool_result = {
                            "ok": False,
                            "error": "delegation_privilege_escalation",
                            "tool": fn_name,
                            "risk_class": _policy.get("risk_class"),
                        }
                        dispatch_allowed = False
                    elif _policy.get("risk_class") == RISK_DEPLOYMENT:
                        try:
                            from runtime.action_grants import consume_action_grant
                            consume_action_grant(
                                deployment_context["grant"],
                                principal_id=deployment_context["principal_id"],
                                tenant_id=deployment_context["tenant_id"],
                                tool=fn_name,
                                args=args,
                            )
                            deployment_authorized = True
                        except Exception:
                            deployment_authorized = False
                    # This must be the same fail-closed chain as the policy
                    # verdicts above. A second independent ``if`` previously
                    # let denied or over-privileged model-emitted calls fall
                    # through to the execution ``else``.
                    if not dispatch_allowed:
                        pass
                    elif _policy.get("risk_class") == RISK_DEPLOYMENT and not deployment_authorized:
                        emit_event(job_id, "tool.blocked", {
                            "tool_name": fn_name,
                            "agent_slug": slug,
                            "risk_class": RISK_DEPLOYMENT,
                            "reason": "deployment_approval_required",
                            "session_id": session_id,
                        })
                        tool_result = {
                            "ok": False,
                            "error": "deployment_approval_required",
                            "tool": fn_name,
                            "risk_class": RISK_DEPLOYMENT,
                        }
                    # Phase 11 — file_write quota enforced BEFORE the call.
                    elif fn_name == "file_write" and files_written >= MAX_FILES_WRITTEN:
                        tool_result = {
                            "ok": False,
                            "error": "file_write_limit_exceeded",
                            "files_written": files_written,
                            "limit": MAX_FILES_WRITTEN,
                        }
                    else:
                        if _policy.get("risk_class") != RISK_READ_ONLY:
                            try:
                                append_tool_intent(
                                    session_id=session_id,
                                    tool_name=fn_name,
                                    inputs=args,
                                )
                            except Exception:
                                audit_integrity_failed = True
                                log.exception(
                                    "Genesis audit preflight failed job=%s tool=%s",
                                    job_id,
                                    fn_name,
                                )
                                tool_result = {
                                    "ok": False,
                                    "error": "audit_preflight_failed",
                                    "tool": fn_name,
                                }
                                tool = None
                        if tool is None:
                            pass
                        else:
                            _remaining_budget = self.remaining_delegation_budget(
                                job_id,
                                token_budget=token_budget,
                                total_tokens=total_tokens,
                                cost_budget_cents=int(bundle.get("conduit_budget_cents", 0)),
                            )
                            # Inject context: bridge, job_dir, runtime, parent_job_id, session_id
                            ctx = {
                                "_bridge": bridge,
                                "_job_dir": job_dir,
                                "_runtime": self,
                                "_parent_job_id": job_id,
                                "_session_id": session_id,
                                "_parent_agent_slug": slug,
                                "_agent_slug": slug,
                                "_allowed_xero_operations": params.get("allowed_xero_operations"),
                                "_scope_map_version": params.get("scope_map_version"),
                                "_execution_realm": params.get("execution_realm"),
                                "_delegation_chain": (*delegation_chain, slug),
                                "_delegation_depth": delegation_depth,
                                # Subtract what earlier siblings already spent, or the
                                # ceiling applies per-child instead of per-tree.
                                "_remaining_token_budget": _remaining_budget[0],
                                "_remaining_cost_budget_cents": _remaining_budget[1],
                            }
                            emit_event(job_id, "tool.called", {
                                "tool_name": fn_name,
                                "session_id": session_id,
                                "turn": turn,
                            })
                            try:
                                tool_executed = True
                                tool_result = await tool(**args, **ctx)
                                if (
                                    fn_name == "genesis_call"
                                    and isinstance(tool_result, dict)
                                    and tool_result.get("child_ok") is False
                                ):
                                    # A child dispatch returning is not proof
                                    # that the child completed successfully.
                                    tool_result = dict(tool_result)
                                    tool_result["ok"] = False
                                    tool_result.setdefault("error", "child_agent_failed")
                                    orchestration_integrity_failed = True
                                # Phase 11 — count successful file writes.
                                if (
                                    fn_name == "file_write"
                                    and isinstance(tool_result, dict)
                                    and tool_result.get("ok")
                                ):
                                    files_written += 1
                            except Exception as e:
                                log.exception("tool %s raised", fn_name)
                                tool_result = {
                                    "ok": False,
                                    "error": "tool_exception",
                                    "type": type(e).__name__,
                                    "message": str(e),
                                }

                tc_finished = time.time()

                if not (isinstance(tool_result, dict) and tool_result.get("ok") is True):
                    tool_integrity_failed = True

                if duplicate_of is None and argument_error is None and isinstance(tool_result, dict):
                    tool_call_results[signature] = (tc_id, dict(tool_result))

                if tool_executed:
                    try:
                        append_tool_event(
                            session_id=session_id,
                            tool_name=fn_name,
                            inputs=args,
                            outputs=tool_result if isinstance(tool_result, dict) else {"ok": False},
                        )
                    except Exception:
                        log.exception("Genesis audit append failed job=%s tool=%s", job_id, fn_name)
                        tool_result = {
                            "ok": False,
                            "error": "audit_append_failed",
                            "tool": fn_name,
                        }

                # Build structured trace record
                record: dict[str, Any] = {
                    "turn": turn,
                    "tool_name": fn_name,
                    "tool_call_id": tc_id,
                    "arguments": (
                        {k: v for k, v in args.items() if not str(k).startswith("_")}
                        if isinstance(args, dict) else {}
                    ),
                    "ok": bool(tool_result.get("ok")) if isinstance(tool_result, dict) else False,
                    "result_summary": json.dumps(tool_result)[:300] if isinstance(tool_result, dict) else str(tool_result)[:300],
                    "started_at": tc_started,
                    "finished_at": tc_finished,
                    "elapsed_s": round(tc_finished - tc_started, 3),
                    "parent_job_id": job_id,
                    "parent_agent_slug": slug,
                }
                if duplicate_of is not None:
                    record["deduplicated"] = True
                    record["duplicate_of_tool_call_id"] = duplicate_of
                # genesis_call gets extra linkage fields + Phase 5 subagent trace entry
                if fn_name == "genesis_call" and isinstance(tool_result, dict):
                    record["target_agent_slug"] = (
                        tool_result.get("target_agent_slug")
                        or tool_result.get("agent", "")
                    )
                    record["child_job_id"] = tool_result.get("child_job_id")
                    record["child_session_id"] = tool_result.get("child_session_id")
                    record["child_ok"] = tool_result.get(
                        "child_ok", bool(tool_result.get("ok"))
                    )
                    child_summary = tool_result.get("child_response_summary") or ""
                    if not child_summary and isinstance(tool_result.get("result"), dict):
                        child_summary = str(tool_result["result"].get("response", ""))[:300]
                    record["child_response_summary"] = child_summary

                    # Phase 5: dedicated subagent trace entry
                    subagent_records.append({
                        "parent_job_id": job_id,
                        "parent_session_id": session_id,
                        "parent_agent_slug": slug,
                        "child_job_id": tool_result.get("child_job_id"),
                        "child_session_id": tool_result.get("child_session_id"),
                        "child_agent_slug": record["target_agent_slug"],
                        "task": args.get("task", ""),
                        "status": "ok" if record["child_ok"] else "failed",
                        "child_ok": record["child_ok"],
                        "child_trace_uri": None,
                        "artifact_uris": [],
                    })

                    emit_event(job_id, "subagent.returned", {
                        "child_job_id": tool_result.get("child_job_id"),
                        "child_session_id": tool_result.get("child_session_id"),
                        "child_agent_slug": record["target_agent_slug"],
                        "child_ok": record["child_ok"],
                        "session_id": session_id,
                    })

                tool_call_records.append(record)

                # Append tool result message
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps(tool_result)[:DEFAULT_MAX_OUTPUT_BYTES],
                })

        return {
            "ok": False,
            "error": "max_turns_reached",
            "slug": slug,
            "turns": turn,
            "resource_usage": {
                "llm_calls": llm_calls,
                "total_tokens": total_tokens,
                "files_written": files_written,
            },
        }

    def _is_anthropic(self) -> bool:
        """Provider selection: explicit env signal first, URL detection as the fallback.

        GENESIS_LLM_PROVIDER is authoritative when set, so an operator can force either path
        without editing a URL. Otherwise an api.anthropic.com URL selects the Anthropic wire
        format — pointing at Anthropic and getting OpenAI-shaped requests is never intended.
        """
        from runtime.anthropic_wire import is_anthropic_url

        declared = (os.getenv("GENESIS_LLM_PROVIDER") or "").strip().lower()
        if declared:
            return declared == "anthropic"
        return is_anthropic_url(self.llm_url)

    async def _call_anthropic(
        self, model: str, messages: list, tools: list, max_tokens: int
    ) -> dict[str, Any]:
        """POST /v1/messages, returning the OpenAI-shaped dict the agent loop expects."""
        import aiohttp

        from runtime.anthropic_wire import (
            ANTHROPIC_FALLBACK_MODELS,
            ANTHROPIC_VERSION,
            build_anthropic_request,
            estimate_anthropic_cost,
            from_anthropic_response,
            translate_model_id,
        )

        if any(
            isinstance(tool, dict) and tool.get("name") == CANONICAL_RESULT_TOOL_NAME
            for tool in (tools or [])
        ):
            result_tool = next(
                (
                    tool for tool in tools
                    if isinstance(tool, dict) and tool.get("name") == CANONICAL_RESULT_TOOL_NAME
                ),
                {},
            )
            result_slug = str(result_tool.get("_genesis_agent_id") or "")
            effective_max_tokens = canonical_candidate_output_cap(result_slug, max_tokens)
            request_started = time.monotonic()
            declared_provider = os.getenv("GENESIS_LLM_PROVIDER")
            try:
                require_canonical_native_anthropic_config(
                    api_url=self.llm_url,
                    api_key=self.llm_key,
                    declared_provider=declared_provider,
                )
                native_ready = True
            except RuntimeError:
                native_ready = False
            if native_ready:
                result = await call_exact_anthropic_candidate(
                    api_url=self.llm_url,
                    api_key=self.llm_key,
                    slug=result_slug,
                    messages=messages,
                    max_tokens=max_tokens,
                    declared_provider=declared_provider,
                )
            elif router_fallback_allowed(
                api_url=self.llm_url,
                api_key=self.llm_key,
                declared_provider=declared_provider,
            ):
                result = await call_router_canonical_candidate(
                    api_url=self.llm_url,
                    api_key=self.llm_key,
                    slug=result_slug,
                    messages=messages,
                    max_tokens=max_tokens,
                )
            else:
                raise RuntimeError(REASON_CANDIDATE_TRANSPORT_FAILED)
            if isinstance(result, dict) and not result.get("transport"):
                result["transport"] = "anthropic-native"
            usage = result.get("usage") or {}
            estimate = estimate_anthropic_cost(
                str(result.get("model") or ""),
                {
                    "input_tokens": usage.get("prompt_tokens"),
                    "output_tokens": usage.get("completion_tokens"),
                },
            )
            result["_genesis_observability"] = {
                "attempt_count": 1,
                "fallback_used": False,
                "candidate_count": 1,
                "latency_ms": round((time.monotonic() - request_started) * 1000, 3),
                "estimated_cost_usd": estimate["estimated_cost_usd"],
                "cost_basis": estimate["cost_basis"],
                "pricing_version": estimate["pricing_version"],
                "cost_unavailable_reason": estimate["unavailable_reason"],
                "requested_max_tokens": max_tokens,
                "effective_max_tokens": effective_max_tokens,
            }
            return result

        env_model = (os.getenv("GENESIS_LLM_MODEL") or "").strip()
        chosen = env_model if (env_model and env_model != "auto") else model
        candidates: list[str] = []
        for candidate in (translate_model_id(chosen), *ANTHROPIC_FALLBACK_MODELS):
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        headers = {
            "x-api-key": self.llm_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        url = self.llm_url if self.llm_url.rstrip("/").endswith("/v1/messages") else (
            self.llm_url.rstrip("/") + "/v1/messages"
        )

        timeout = aiohttp.ClientTimeout(total=120)
        last_error = "unknown"
        attempt_count = 0
        request_started = time.monotonic()
        async with _llm_client_session(timeout) as session:
            for candidate_index, routed_model in enumerate(candidates):
                attempt_count += 1
                body = build_anthropic_request(
                    model=routed_model, messages=messages, tools=tools, max_tokens=max_tokens
                )
                async with session.post(url, headers=headers, json=body) as resp:
                    if resp.status in (200, 201):
                        raw_payload = await resp.json()
                        result = from_anthropic_response(raw_payload)
                        actual_model = str(raw_payload.get("model") or routed_model)
                        estimate = estimate_anthropic_cost(actual_model, raw_payload.get("usage"))
                        result["_genesis_observability"] = {
                            "attempt_count": attempt_count,
                            "fallback_used": candidate_index > 0,
                            "candidate_count": len(candidates),
                            "latency_ms": round((time.monotonic() - request_started) * 1000, 3),
                            "estimated_cost_usd": estimate["estimated_cost_usd"],
                            "cost_basis": estimate["cost_basis"],
                            "pricing_version": estimate["pricing_version"],
                            "cost_unavailable_reason": estimate["unavailable_reason"],
                        }
                        return result
                    text = await resp.text()
                    last_error = f"LLM HTTP {resp.status}: {text[:500]}"
                    # Only a model-availability or capacity problem is worth retrying on another
                    # model. A 401/403 is the same for every model, and retrying it three times
                    # just buries the real cause.
                    if resp.status in (404, 429, 529):
                        log.warning(
                            "anthropic call failed status=%s model=%s; trying next model",
                            resp.status, routed_model,
                        )
                        continue
                    raise RuntimeError(last_error)
        raise RuntimeError(last_error)

    async def _call_llm(
        self,
        model: str,
        messages: list,
        tools: list,
        max_tokens: int,
        slug: Optional[str] = None,
    ) -> dict[str, Any]:
        """Dispatch a model request through the configured provider."""
        return await self._call_llm_inner(model, messages, tools, max_tokens, slug=slug)

    async def _call_llm_inner(
        self,
        model: str,
        messages: list,
        tools: list,
        max_tokens: int,
        slug: Optional[str] = None,
    ) -> dict[str, Any]:
        """Call the configured LLM endpoint.

        Two providers, selected explicitly. The agent loop always speaks OpenAI shape; the
        Anthropic branch translates at the wire and translates the response back, so nothing
        downstream (trace, budget accounting, tool dispatch) has to know which provider ran.
        """
        import aiohttp

        reserved_result_call = any(
            isinstance(tool, dict) and tool.get("name") == CANONICAL_RESULT_TOOL_NAME
            for tool in (tools or [])
        )
        if reserved_result_call or self._is_anthropic():
            return await self._call_anthropic(model, messages, tools, max_tokens)

        if any(marker in self.llm_url for marker in DISALLOWED_LLM_HOST_MARKERS):
            raise RuntimeError(
                "OpenRouter is disabled for Genesis agents; set LLM_API_URL to "
                "https://api.swarmsync.ai/v1/chat/completions"
            )

        # Router branch only: the Anthropic branch above is untouched, and the
        # canonical V5 path is untouched. Per-agent override beats the global
        # GENESIS_LLM_MODEL, which beats the bundle model_hint, which beats the
        # default — see runtime/per_agent_model.py. All other agents fall back
        # to the global env exactly as before (slug=None preserves old behaviour).
        env_model = (os.getenv("GENESIS_LLM_MODEL") or "").strip()
        primary, resolved_from = resolve_agent_model(
            slug, model, env_model, DEFAULT_SWARMSYNC_MODEL
        )
        model_candidates = [primary, "minimax/minimax-m2.5"]
        deduped: list[str] = []
        for m in model_candidates:
            if m and m not in deduped:
                deduped.append(m)

        headers = {
            "Authorization": f"Bearer {self.llm_key}",
            "Content-Type": "application/json",
        }

        timeout = aiohttp.ClientTimeout(total=120)
        last_error = "unknown"
        attempt_count = 0
        async with _llm_client_session(timeout) as session:
            for candidate_index, routed_model in enumerate(deduped):
                attempt_count += 1
                body: dict[str, Any] = {
                    "model": routed_model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "tools": tools if tools else None,
                    "tool_choice": "auto" if tools else None,
                }
                body = {k: v for k, v in body.items() if v is not None}
                async with session.post(self.llm_url, headers=headers, json=body) as resp:
                    if resp.status in (200, 201):
                        result = await resp.json()
                        if isinstance(result, dict):
                            result["_genesis_observability"] = {
                                "attempt_count": attempt_count,
                                "fallback_used": candidate_index > 0,
                                "candidate_count": len(deduped),
                                "primary_model": primary,
                                "model_resolved_from": resolved_from,
                                "model_used": routed_model,
                            }
                        return result
                    text = await resp.text()
                    last_error = f"LLM HTTP {resp.status}: {text[:500]}"
                    combined = text.lower()
                    if resp.status in (400, 402, 429) or any(
                        x in combined for x in ("402", "credit", "balance", "quota", "payment")
                    ):
                        log.warning(
                            "LLM call failed status=%s model=%s; trying next model",
                            resp.status,
                            routed_model,
                        )
                        continue
                    raise RuntimeError(last_error)
        raise RuntimeError(last_error)
