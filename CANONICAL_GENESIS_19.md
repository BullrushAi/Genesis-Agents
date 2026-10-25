# Genesis Agents - Canonical 19 Roster

## Overview

Genesis Agents is a **white-label AI agent platform** providing specialized business intelligence across **19 canonical domains**. This is the authoritative roster definition for Genesis.

**Canonical count**: 19 agents (10 MasterOS + 9 FinanceOS)

**Authority**: `canonical_profiles.py::CANONICAL_SLUGS`

---

## Canonical 19 Agent Roster

### Business / MasterOS Specialists (10 agents)

Located in `skill_bundles/masteros-specialists/`

#### 1. genesis-automation-architect
**Purpose**: Workflow automation & meta orchestration design
**Capabilities**: Absorbs workflow-automator and orchestration design concerns
**Tools**: Workflow export, webhook triggers, automation pattern design

#### 2. genesis-commercial-growth
**Purpose**: Marketing, content, SEO, commerce, pricing strategy
**Capabilities**: Absorbs marketing, content, seo, commerce, and pricing-strategy agents into unified commercial growth intelligence
**Tools**: Web search, web fetch, content generation

#### 3. genesis-data-intelligence
**Purpose**: Business intelligence, data analysis, research, AI vision
**Capabilities**: Absorbs analyst, data-pipeline, research, and ai-vision into unified data intelligence
**Tools**: Data pipeline design, quality checks, vision analysis, research

#### 4. genesis-execution-control
**Purpose**: Support operations, QA gates, maintenance runbooks
**Capabilities**: Absorbs support-ops, qa, and maintenance concerns
**Tools**: Test execution, validation, maintenance scheduling

#### 5. genesis-integration-architect
**Purpose**: Deploy connectors, domain/DNS integration architecture
**Capabilities**: Absorbs deploy and domain integration concerns
**Tools**: Domain configuration, DNS management, integration design

#### 6. genesis-knowledge-evidence
**Purpose**: Institutional knowledge, evidence analysis, **+ canon/policy stewardship**
**Capabilities**: Research-docs + **FULL duties of genesis-canon-steward** (canon monitoring, policy alignment)
**Tools**: Knowledge retrieval, evidence tracing, canon monitoring
**Note**: **Absorbs genesis-canon-steward** — handles both evidence analysis and policy/canon alignment

#### 7. genesis-legal-risk
**Purpose**: Legal compliance and risk analysis
**Capabilities**: Legal review, compliance checking, risk flagging
**Tools**: Document review, legal research, compliance validation

#### 8. genesis-people-operations
**Purpose**: HR, onboarding, people management
**Capabilities**: Absorbs hr and onboarding
**Tools**: Onboarding workflows, HR policy guidance

#### 9. genesis-platform-engineer
**Purpose**: Software, platform & DevOps engineering **+ reliability/incident response**
**Capabilities**: Deploy, maintenance, domain, builder concerns + **FULL duties of genesis-reliability** (incident analysis, root-cause, recovery)
**Tools**: Code tools, GitHub, run_code, file_write, **+ log/trace access for reliability**
**Note**: **Absorbs genesis-reliability** — unified platform engineering and reliability

#### 10. genesis-security-identity
**Purpose**: Security analysis and identity management
**Capabilities**: Security hardening, identity access, permission review
**Tools**: Security scans, code review, access analysis

---

### Accounting / FinanceOS Specialists (9 agents)

Located in `skill_bundles/finance-specialists/`

All FinanceOS agents follow the V5 result contract with mandatory `finance_proposal` output and no-write boundaries.

#### 1. genesis-fin-ap
**Purpose**: Accounts Payable analysis
**Capabilities**: Vendor invoice analysis, payment scheduling, AP reconciliation
**Tools**: file_write (proposals only, no posting)
**Note**: Live 502 output_contract_failed/finance_execution_claim — prompt/contract hardening in progress to ensure analysis-only paths don't claim execution

#### 2. genesis-fin-ar
**Purpose**: Accounts Receivable analysis
**Capabilities**: Customer invoice analysis, receivables aging, collections review
**Tools**: file_write (proposals only)

#### 3. genesis-fin-revenue
**Purpose**: Revenue recognition and analysis
**Capabilities**: Revenue treatment proposals, recognition timing, contract analysis
**Tools**: file_write (proposals only)

#### 4. genesis-fin-close
**Purpose**: Close, journals, **+ intercompany/consolidation**
**Capabilities**: Reconciliation review, journal proposals, roll-forward integrity + **intercompany transactions, eliminations, group reporting** (absorbed from genesis-fin-intercompany)
**Tools**: file_write (proposals only), FinanceOS close state access
**Note**: **Absorbs genesis-fin-intercompany** — handles both single-entity close and multi-entity consolidation concerns

#### 5. genesis-fin-reconciliation
**Purpose**: Reconciliation analysis and review
**Capabilities**: Account reconciliation, variance analysis, balance verification
**Tools**: file_write (proposals only)

#### 6. genesis-fin-controller
**Purpose**: Controller, accounting policy arbitration **+ COGS oversight**
**Capabilities**: Finance reasoning, policy application, specialist proposal arbitration, whole-book coherence + **inventory valuation, landed cost, COGS, contribution margin** (absorbed from genesis-fin-cogs)
**Tools**: file_write (proposals only), FinanceOS state access
**Note**: **Absorbs genesis-fin-cogs** — controller now includes COGS oversight duties

#### 7. genesis-fin-fpa
**Purpose**: Financial planning & analysis + pricing-economics inputs
**Capabilities**: Absorbs financial analysis and pricing-economics concerns, forecasting, budget analysis
**Tools**: file_write (proposals only)

#### 8. genesis-fin-treasury
**Purpose**: Treasury and cash management
**Capabilities**: Cash flow analysis, treasury operations review, liquidity planning
**Tools**: file_write (proposals only)

#### 9. genesis-fin-payroll-tax
**Purpose**: Payroll, tax **+ commissions**
**Capabilities**: Payroll accounting, tax coordination, VAT/GST/sales tax **+ commissions, affiliate, partner accounting** (absorbed from genesis-fin-commissions)
**Tools**: file_write (proposals only), payroll-provider read (least-necessary access)
**Note**: **Absorbs genesis-fin-commissions** — unified compensation accounting (payroll + commissions)

---

## Core Architectural Principles

### 1. White-Label Compatible
- No client-specific branding or hard-coded entities
- Generic company/entity references only
- Portable across deployments

### 2. Bounded Intelligence
- Each agent has a specific, well-defined role
- Agents analyze and propose; they don't execute
- Source systems remain authoritative

### 3. Fail-Closed
- Agents surface uncertainty rather than guessing
- Missing/conflicting/stale evidence is explicitly reported
- Never convert timeouts or stubs into success

### 4. Evidence-Based
- All claims require supporting evidence
- Citations and source references mandatory
- Distinguish fact from inference

### 5. Structured Output
- Consistent V5 JSON response contracts
- `ANALYSIS_COMPLETE` status means only analysis finished (not business action complete)
- FinanceOS agents must include `finance_proposal` object

---

## What Genesis Agents Are NOT

- **Not autonomous executors** — Agents analyze and propose, they don't execute business operations
- **Not authoritative systems** — Source systems (ERP, CRM, ledger) remain authoritative
- **Not self-approving** — Material actions require human or system-of-record approval
- **Not ambient capture** — Agents work on bounded, scoped tasks
- **Not direct finance writers** — FinanceOS Worker is the sole authoritative writer for production ledgers

---

## Output Contract (V5)

All canonical agents follow the **Genesis V5 Result Contract**:

```json
{
  "agent_id": "genesis-<domain>",
  "task_id": "<task-identifier>",
  "status": "ANALYSIS_COMPLETE | PARTIAL | HOLD | REFUSED | FAILED",
  "summary": "2-sentence concise summary",
  "findings": ["key finding 1", "key finding 2"],
  "proposals": ["proposed action 1", "proposed action 2"],
  "evidence_refs": ["source 1", "source 2"],
  "assumptions": ["assumption 1 if any"],
  "conflicts": ["conflict 1 if any"],
  "unknowns": ["unknown 1 if any"],
  "confidence": 0.0-1.0,
  "risk_flags": ["risk 1 if any"],
  "recommended_next_actions": ["action 1", "action 2"],
  "requested_human_decisions": ["decision needed 1 if any"],
  "tool_calls_summary": ["tool call summary"],
  "clarification_needed": false | true,
  "candidate_resolution": "...",
  "finance_proposal": { /* required for FinanceOS agents */ }
}
```

### Status Semantics

- **ANALYSIS_COMPLETE**: The bounded reasoning assignment completed successfully (does NOT mean business action completed)
- **PARTIAL**: Partial analysis completed, some aspects remain
- **HOLD**: Analysis cannot proceed without additional input
- **REFUSED**: Request is outside agent's scope or permissions
- **FAILED**: Technical failure during processing

---

## Safety Boundaries

### What Agents Cannot Do

- **No direct writes**: Agents never write directly to production systems
- **No payments**: Cannot initiate payments, transfers, or financial transactions
- **No external communications**: Cannot send emails, messages, or notifications on their own
- **No credential management**: Cannot create, modify, or share credentials
- **No self-approval**: Cannot approve their own proposals

### Permission Model

- **Read**: Agents can read scoped, task-specific data
- **Analyze**: Agents can perform reasoning and analysis
- **Propose**: Agents can generate structured proposals
- **Escalate**: Agents can request human review

All consequential actions require external approval and execution.

---

## Deployment Configurations

### Standard Deployment (19 agents)
Load all canonical agents from `skill_bundles/masteros-specialists/` and `skill_bundles/finance-specialists/`

### White-Label Business Focus (10 agents)
Load only MasterOS specialists for non-finance deployments

### Finance-Only Deployment (9 agents)
Load only FinanceOS specialists for pure finance intelligence

### Excluding Legacy
**Always exclude** `skill_bundles/e4l-legacy/` from white-label deployments to avoid client-specific branding

---

## Integration Patterns

### Orchestration
Genesis agents are designed to be **orchestrated**, not autonomous:

1. **Orchestrator** (e.g., Cato) receives user request
2. **Route** to appropriate Genesis agent(s) with bounded scope
3. **Collect** structured analysis and proposals
4. **Present** findings to user or downstream system
5. **Execute** approved actions via authoritative systems

### Multi-Agent Workflows
Complex tasks can involve multiple agents in sequence or parallel, coordinated by the orchestrator.

---

## Production Requirements

### Observability
- **Phoenix tracing**: Request/response/latency/cost tracking
- **Structured logging**: Evidence trail for all agent decisions
- **Audit compliance**: Immutable record of agent reasoning

### Testing
- **Unit tests**: Agent-specific behavior verification
- **Contract tests**: Output structure validation
- **Integration tests**: Multi-agent workflow testing
- **Evaluation suites**: Quality score benchmarks

Each canonical agent includes an evaluation suite in `eval/canonical_profiles/`

---

## Retired Agents (Folded into Canonical 19)

### From MasterOS:
- **genesis-canon-steward** → folded into `genesis-knowledge-evidence`
- **genesis-reliability** → folded into `genesis-platform-engineer`

### From FinanceOS:
- **genesis-fin-cogs** → folded into `genesis-fin-controller`
- **genesis-fin-commissions** → folded into `genesis-fin-payroll-tax`
- **genesis-fin-intercompany** → folded into `genesis-fin-close`

See `AGENT_CONSOLIDATION_MAP.md` for detailed consolidation rationale and capability mapping.

---

## Version Information

- **Roster version**: Genesis-19 (Canonical)
- **Contract version**: Genesis V5
- **Profile architecture**: V2
- **Last updated**: 2026-09-12
- **Authority**: `canonical_profiles.py::CANONICAL_SLUGS`

---

## Model Support

Genesis agents support multiple LLM backends:
- **Primary**: Anthropic Claude (claude-sonnet-4-5)
- **Fallbacks**: Configurable per agent or per task
- **Provider flexibility**: Adaptable to deployment needs

---

## Related Documentation

- **Consolidation details**: `AGENT_CONSOLIDATION_MAP.md`
- **White-label guide**: `WHITE_LABEL.md`
- **E4L-specific reference**: `E4L_GENESIS_AGENT_CANON_MASTER_OS_AND_FINANCEOS_V2.md` (historical)
- **Implementation**: `canonical_profiles.py`, `bundle_loader.py`, `main.py`

---

**This document describes the canonical Genesis-19 roster, the sole source of truth for Genesis agent definitions going forward.**

For deployment assistance, custom agent development, or enterprise support, contact the platform maintainer.
