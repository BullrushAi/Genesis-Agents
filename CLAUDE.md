---
entity: E4L
project: Genesis-Agents
type: claude-project-rules
status: active
version: "2.0"
canon_version: "5"
canonical: true
control_plane: "C:\\Users\\Work\\Desktop\\E4L-Project-Control-Plane\\CURRENT"
updated: 2026-08-27
---
## 🚨 E4L PROJECT STARTUP — MANDATORY, READ FIRST EVERY SESSION

Before doing ANY project work, answering project-status questions, editing code, assigning agents, deploying, or claiming completion:

1. Read:
   `C:\Users\Work\Desktop\E4L-Project-Control-Plane\CURRENT\README.md`
2. Follow its mandatory startup sequence in order:
   - `PROJECT-REGISTRY.yaml`
   - `SPEC-REGISTRY.yaml`
   - `BUILD-STATUS.yaml`
   - `DEPLOYMENT-REGISTRY.yaml`
   - `CONTRACT-MATRIX.yaml`
   - `BLOCKERS.md`
   - `VERIFICATION-REGISTER.md`
   - `DECISION-REGISTER.md`
3. Read this repo's own canonical spec/current-status files named in `PROJECT-REGISTRY.yaml`.
4. Mechanically verify this repo's:
   - Git root
   - remote
   - branch
   - HEAD SHA
   - worktree status
   - canonical spec path/hash
   - deployed SHA/runtime when applicable
5. Do NOT rely on old conversation memory, stale handoffs, similarly named folders, old specs, or historical test counts when live evidence can be checked.
6. After every material commit, release, deployment, blocker, contract change, or verified milestone, update the appropriate files under:
   `C:\Users\Work\Desktop\E4L-Project-Control-Plane\CURRENT`
7. Never create a competing cross-project status/registry file when `CURRENT` already owns that purpose.

The shared Control Plane coordinates projects. It does NOT replace this repo's canonical architecture or implementation authority.


---

# Genesis Agents — Mandatory Development Guide

## Canonical role

Genesis is the **bounded specialist intelligence layer**.

Genesis does NOT own:
- Cato orchestration
- Company Control Plane state
- Ask E4L/company truth
- FinanceOS finance truth
- finance mappings/semantic definitions
- approvals
- Xero writes
- business-action verification

## Canonical roster

The old 74-slug / 38-bundle / 36-unguarded catalogue model is historical and must not be used as current architecture.

The canonical Genesis runtime exposes **exactly 19 logical agents**:

### 10 MasterOS specialists
- genesis-knowledge-evidence
- genesis-execution-control
- genesis-automation-architect
- genesis-integration-architect
- genesis-platform-engineer
- genesis-data-intelligence
- genesis-security-identity
- genesis-legal-risk
- genesis-commercial-growth
- genesis-people-operations

### 9 FinanceOS specialists
- genesis-fin-revenue
- genesis-fin-ap
- genesis-fin-ar
- genesis-fin-reconciliation
- genesis-fin-treasury
- genesis-fin-close
- genesis-fin-fpa
- genesis-fin-payroll-tax
- genesis-fin-controller

No additional ambient-capture agent.

Active role authority: GENESIS_19_ROLE_CONTRACT.md. Retired roles are folded per AGENT_CONSOLIDATION_MAP.md; historical V2 is not the active roster.
No Shopify/Stripe/Xero/source-app agents.

Logical agent does not require one physical endpoint/process per profile.

## Current deployment

Production service:
`https://swarmsync-agents.onrender.com`

Entry surface includes:
- `GET /agents`
- `POST /agents/{slug}/run`
- worker/health/version surfaces defined by current runtime

Do not trust an old deployed SHA. Verify `/health/worker` and Render metadata before any production claim.

## Canonical result contract

Successful bounded reasoning uses:

`ANALYSIS_COMPLETE`

Not:
- COMPLETE
- DONE
- business completed
- payment completed
- journal posted

The result contract is fail-closed and structured.

Conditional clarification fields are evidence-first and must be populated only when clarification is genuinely needed.

Do not synthesize filler facts just to satisfy schema shape.

## Business-completion firewall

Genesis may complete analysis.

Genesis may NOT claim that external business work actually occurred unless that state is independently established through an authorized trusted runtime observation under the canonical contract.

Never turn:
- model prose
- citations
- tool summaries
- reserved result submission
into proof of real-world execution.

The deterministic safety layer remains non-overridable.

## FinanceOS V5 input rule

Finance specialists should default to governed FinanceOS context:

```text
FinanceOS certified data product
+ semantic definition/version
+ mapping/rule version
+ certification/reconciliation state
+ lineage
→ Genesis bounded analysis
```

Raw Xero/Shopify/Stripe/Excel are evidence/investigation sources, not a second semantic layer.

Finance agents may:
- analyze
- explain
- investigate
- propose mapping changes
- propose accounting treatment
- identify discrepancies
- recommend next steps

Finance agents may NOT:
- activate mappings
- adopt semantic definitions
- certify data
- modify FinanceOS authoritative state
- write Xero
- move money
- approve finance work
- treat Excel as actuals authority

When supplied, preserve:
- entity
- period/as-of
- data-product ID/version
- semantic-definition ID/version
- mapping/rule version
- certification/reconciliation state
- freshness
- lineage/source refs
- policy versions
- exception context

Do not invent absent metadata.

## Xero write rule

Genesis has **zero direct Xero write authority against live/production Xero tenants**.

**CORRECTION 2026-08-27 (E4L-Integration-Lead code audit):** this file previously stated an unqualified "zero direct Xero write authority" with no exception. That is not accurate against the actual code. `tools/xero_scoped_tool.py::xero_scoped_invoke` is a live, wired POST path (`XERO_MCP_BRIDGE_URL`/`XERO_MCP_BRIDGE_TOKEN`, both present as configured env vars on the production Render deployment per `proof-artifacts/source-truth/render_evidence_redacted.txt`) that lets the **legacy `genesis-e4l-*` agents** (not the canonical 19-roster `genesis-fin-*` agents, which have no grant in `accounting/XERO_SCOPE_TO_AGENT_MAP.yaml`) submit **DRAFT-tier only** operations (`create_draft_bill`, `create_draft_invoice`, `create_draft_manual_journal`, `create_bank_transaction`), gated by `accounting/xero_scope.py::operation_allowed`, a `verify_api_run_id`, and per-run idempotency. The default bridge URL in `.env`/`.env.example` points at the tenant-locked DEMO Xero MCP server (`xero-mcp-demo`, tenant `c00a07d6-...`, "Demo Company (US)") — the same demo-only endpoint Cato's `xero_mcp.py` uses — but the actual production Render env var value is redacted and was NOT independently confirmed this session to still point at demo rather than a live tenant. Treat that as an open verification item, not a closed one. See `CURRENT/BLOCKERS.md`.

This includes, for any tenant other than the confirmed-demo path above:
- production
- remediation
- AP
- AR
- journals
- commissions
- treasury
- controller

The deterministic FinanceOS Worker is the sole authoritative financial writer for live/production Xero. The legacy `genesis-e4l-*` draft-write bridge above is demo-scoped-by-default and human-approved before anything posts to a real ledger; it must be re-audited (confirm the live bridge URL, and either retire the legacy catalogue's write grants or bring them under the same FinanceOS-governed contract as the canonical roster) before this file's write-authority claim can be stated as unqualified again.

## Provider/runtime rule

The current runtime has undergone substantial provider-contract hardening.

Before modifying provider execution:
- read the current canonical V2/V5 spec
- inspect current provider/runtime code
- inspect current exact-SHA production evidence
- do not restore old SwarmSync-router assumptions merely because historical CLAUDE.md said so

Current provider/schema/safety behavior must be derived from HEAD and the canonical spec, not this file's historical predecessor.

No automatic retry is allowed where request outcome is ambiguous unless the current canonical contract explicitly permits it.

## Verification evidence

Use revision-bound local test, provider-response, deployment and business-operation receipts. No external tracing service is configured or required. Evidence must distinguish analysis completion from real business execution.

## Production certification

Do not declare Genesis complete until one immutable live SHA proves:
- exactly 19 canonical agents
- auth/refusal behavior
- canonical structured result contract
- all-19 profile parity
- FinanceOS V5 finance-context behavior
- no-write finance boundary
- revision-bound provider and deployment receipts
- exact-19 production cohort
- independent truth/audit launch gate

Historical passing agents from another SHA do not count toward a new SHA's exact-19 certification unless the launch protocol explicitly permits inherited evidence.

## Local dev setup (verified 2026-08-27, unchanged from prior CLAUDE.md)

```bash
pip install -r requirements.txt
python -m patchright install chromium
uvicorn main:app --reload --port 8000
```

Critical env vars (see `.env.example`): `LLM_API_KEY`, `LLM_API_URL`, `GENESIS_LLM_MODEL`, `AGENT_GATEWAY_SECRET`. `LLM_API_URL` defaults to the SwarmSync router (`https://api.swarmsync.ai/v1/chat/completions`); pointing it at `https://api.anthropic.com` with an `ANTHROPIC_API_KEY` in `LLM_API_KEY` is the direct-Anthropic alternative documented inline in `.env.example`. This is about model-call transport for local dev, not a contradiction of Cato's separate "Cato calls Anthropic directly" rule in its own `CLAUDE.md` — the two repos route model calls independently.

## Cross-project boundaries

If a defect belongs to:
- Cato → hand it to Cato builder
- FinanceOS → hand it to FinanceOS builder

Do not fix another product by duplicating its logic inside Genesis.

## Local work

Always derive exact commands from current repo files.

At minimum:
- run focused tests for changed contracts
- run full regression required by current repo
- verify exact 19 profile inventory
- verify generated datasets/evals remain current
- run secret/scope checks
- use immutable release + exact-SHA CI/deploy custody

## Session close

Before ending:
1. verify local HEAD
2. capture tests
3. update live SHA if deployed
4. update `E4L-Project-Control-Plane\CURRENT`
5. update `CURRENT\HANDOFFS\genesis\LATEST.md`
6. do not leave production-cohort state only in chat logs
