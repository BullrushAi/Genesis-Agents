# Per-agent model override — design (code only, NOT deployed)

## Problem
GENESIS_LLM_MODEL is global. Live it is anthropic/claude-sonnet-4-5, and in
AgentRuntime it overrides every bundle model_hint, and in the legacy persona router it
is the primary model. A per-agent switch for genesis-knowledge-evidence to M3.1-Flash
is therefore blocked: changing a bundle hint does nothing while the global is set, and
changing the global would switch all 19 agents.

## Design
One env var per agent: `GENESIS_MODEL_OVERRIDE_<SLUG>`, slug upper-cased with
hyphens replaced by underscores. For knowledge-evidence:
`GENESIS_MODEL_OVERRIDE_GENESIS_KNOWLEDGE_EVIDENCE=<router model id for M3.1-Flash>`.
The value is passed through verbatim to the router. No provider prefix is hardcoded:
Platform must confirm the exact router model id for M3.1-Flash before deploy (router
resolveModel uses registry, MODEL_MAP, prefix inference, then passthrough — a bare
"M3.1-Flash" with no provider prefix may not resolve, and that must be proven at
deploy time, not assumed here).

Precedence, router paths only:
1. per-agent override (non-empty, not "auto")
2. global GENESIS_LLM_MODEL (non-empty, not "auto")
3. bundle model_hint (non-empty, not "auto")
4. default (AgentRuntime: minimax/minimax-m2.5; persona router: existing fallback list)

Empty or "auto" override falls through silently to global. All other agents fall back
to global exactly as before. Fallback lists are unchanged, so a router error on the
override still falls through to the existing fallbacks — which is why observability
(see below) is mandatory after deploy: a fallback must never be mistaken for the
override having run.

## Code changes (this branch only)
- `runtime/per_agent_model.py` (new): slug normalisation, env name, override read,
  `resolve_agent_model` precedence function. No provider calls, no network.
- `agent_runtime.py`: `_call_llm` / `_call_llm_inner` take optional `slug`
  (default None preserves old behaviour); `execute_agent` threads its slug through;
  the non-Anthropic (router) branch resolves the primary via `resolve_agent_model`;
  the Anthropic branch is untouched. Success observability now also records
  `primary_model`, `model_resolved_from`, and `model_used`.
- `main.py` persona router: `call_llm_router` / `_call_llm_router_inner` take optional
  `slug`; when an override is set for that slug it replaces only the first (primary)
  model, fallbacks unchanged. Threaded at the library and canonical call sites in
  `_execute_bounded_job` and `run_agent`. Negotiation/evaluation callers pass no slug
  and keep the global model — they are not agent execution, and are out of scope.

## Explicitly NOT changed
- Global env GENESIS_LLM_MODEL: untouched, still Sonnet 4.5 live. No Render env change.
- Bundle hints: untouched.
- Canonical V5 candidate/judge: still Haiku-pinned (CANDIDATE_MODEL / JUDGE_MODEL,
  ROUTER_HAIKU_MODEL, startup `require_exact_model_role_binding`,
  `_normalize_haiku_model`). This override does NOT switch the SkillProof live_test /
  testContext route for knowledge-evidence, which stays Haiku. A canonical-path switch
  is a separate design (Haiku gate change, judge recalibration, full offline gate
  re-run) and needs its own Atlas approval.
- Direct Anthropic path: override is applied only in the router branch. If the endpoint
  is an Anthropic URL, the override is skipped and global/bundle selection applies,
  because a router model id must never be posted to api.anthropic.com.
- Validators, token caps, V5 contract, fallback lists: unchanged.

## Tests (offline, synthetic only)
`tests/test_per_agent_model.py`, stdlib unittest: slug-to-env-name, override beats
global and bundle, other agents fall back to global, no-slug falls back to global,
global "auto" falls back to bundle, override "auto"/empty falls through to global,
no global/bundle falls to default, override value passed verbatim. No provider call,
no key, no spend.

## Deploy checklist (for Atlas approval, NOT done here)
1. Confirm exact router model id for M3.1-Flash with Platform; set ONLY
   `GENESIS_MODEL_OVERRIDE_GENESIS_KNOWLEDGE_EVIDENCE` to that id on Render.
   Do NOT change GENESIS_LLM_MODEL.
2. Deploy this branch via the normal manual Render deploy (autoDeploy is off).
3. Run one knowledge-evidence paid/async or persona job and read
   `_genesis_observability`: `model_used` must equal the override id and
   `model_resolved_from` must be `per_agent_override`. If `fallback_used` is true,
   the override did NOT run — stop and report, do not assume it switched.
4. Run one other agent job and confirm `model_resolved_from` is `global` and the
   model is still Sonnet 4.5.
5. Canonical live_test / SkillProof for knowledge-evidence is expected to stay Haiku
   — do not treat that as a failure of this change.
