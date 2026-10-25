# Genesis Agent Consolidation Map

## Overview

This document maps the consolidation from 24 canonical agents to 19 canonical agents, documenting which agents were retired and where their capabilities were absorbed.

**Previous roster**: 24 agents (12 MasterOS + 12 FinanceOS)
**New roster**: 19 agents (10 MasterOS + 9 FinanceOS)

## Consolidation Summary

### MasterOS: 12 → 10 agents

**Retired agents:**
- `genesis-canon-steward` → absorbed into `genesis-knowledge-evidence`
- `genesis-reliability` → absorbed into `genesis-platform-engineer`

### FinanceOS: 12 → 9 agents

**Retired agents:**
- `genesis-fin-cogs` → absorbed into `genesis-fin-controller`
- `genesis-fin-commissions` → absorbed into `genesis-fin-payroll-tax`
- `genesis-fin-intercompany` → absorbed into `genesis-fin-close`

## Detailed Consolidation Map

### 1. genesis-canon-steward → genesis-knowledge-evidence

**Retired**: `genesis-canon-steward` (Canon, Policy & Institutional Alignment Steward)

**Absorbed into**: `genesis-knowledge-evidence` (Institutional Knowledge & Evidence Analyst)

**Rationale**: Both agents handle company knowledge, policy, and canon alignment. Knowledge-evidence already manages company questions and evidence tracing, making it the natural home for canon/policy stewardship duties.

**Capabilities merged**:
- Canon and policy source monitoring for drift
- Company context repo read access
- Alignment without becoming the authority
- Policy coherence checking

**New combined scope**: genesis-knowledge-evidence now handles both:
- Finding/comparing/tracing evidence for company questions
- Monitoring canon/policy sources and keeping implementations aligned
- FULL duties of the former genesis-canon-steward

---

### 2. genesis-reliability → genesis-platform-engineer

**Retired**: `genesis-reliability` (Reliability, Incident & Root-Cause Analyst)

**Absorbed into**: `genesis-platform-engineer` (Software, Platform & DevOps Engineer)

**Rationale**: Platform engineering inherently includes system reliability, incident response, and production health. These concerns are tightly coupled with platform/DevOps work.

**Capabilities merged**:
- Cross-system failure analysis and root-cause determination
- Log/trace access and analysis
- Recovery path determination
- Incident definition and resolution
- Production health monitoring

**New combined scope**: genesis-platform-engineer now handles:
- Software/platform implementation, testing, and deployable changes
- FULL duties of the former genesis-reliability (incident analysis, root-cause, recovery)
- Unified platform health and engineering

---

### 3. genesis-fin-cogs → genesis-fin-controller

**Retired**: `genesis-fin-cogs` (Inventory, Landed Cost, COGS & Contribution Margin)

**Absorbed into**: `genesis-fin-controller` (Controller, Accounting Policy & Finance Arbitration)

**Rationale**: The controller role already arbitrates specialist proposals and ensures whole-book coherence. COGS oversight is a natural controller concern, especially for policy application and margin analysis.

**Capabilities merged**:
- Inventory valuation analysis
- Landed cost allocation
- Reserves, returns, write-offs analysis
- COGS and contribution-margin economics
- Inventory/fulfillment reads

**New combined scope**: genesis-fin-controller now includes COGS oversight as part of controller duties, maintaining senior finance reasoning plus specific COGS/inventory competency.

---

### 4. genesis-fin-commissions → genesis-fin-payroll-tax

**Retired**: `genesis-fin-commissions` (Commissions, Affiliate & Partner Accounting)

**Absorbed into**: `genesis-fin-payroll-tax` (Payroll Accounting, Tax & Compliance Coordination)

**Rationale**: Both agents handle compensation-related accounting. Commissions are a form of compensation that shares payroll's least-necessary-access privacy requirements and tax coordination concerns.

**Capabilities merged**:
- Commission plan interpretation and calculation
- Accruals, clawbacks, partner liabilities
- Exception case handling for commissions
- Commission plan registry access

**New combined scope**: genesis-fin-payroll-tax now handles:
- Payroll accounting and tax coordination
- Commission/affiliate/partner accounting (folded from genesis-fin-commissions)
- Unified compensation accounting (payroll + commissions)

---

### 5. genesis-fin-intercompany → genesis-fin-close

**Retired**: `genesis-fin-intercompany` (Intercompany, Entity & Group Accounting)

**Absorbed into**: `genesis-fin-close` (Close, Journals & Financial Statement Integrity)

**Rationale**: Intercompany transactions, eliminations, and group reporting are integral to the close process and consolidation work. The close agent already handles roll-forward integrity and financial statement consistency.

**Capabilities merged**:
- Intercompany transactions analysis
- Due-to/due-from relationships
- Eliminations and FX handling
- Group reporting and entity attribution
- Multi-entity Xero reads

**New combined scope**: genesis-fin-close now includes intercompany/consolidation guidance as part of close/consolidation duties, handling both single-entity close work and multi-entity/group concerns.

---

## Canonical Genesis-19 Roster

### Business / MasterOS (10 agents)

1. **genesis-automation-architect** — workflow automation & meta orchestration design
2. **genesis-commercial-growth** — marketing, content, seo, commerce, pricing strategy
3. **genesis-data-intelligence** — analyst, data-pipeline, research, AI vision
4. **genesis-execution-control** — support-ops, QA gates, maintenance runbooks
5. **genesis-integration-architect** — deploy connectors, domain/DNS integration
6. **genesis-knowledge-evidence** — research-docs + FULL canon-steward duties
7. **genesis-legal-risk** — legal compliance and risk analysis
8. **genesis-people-operations** — HR, onboarding, people management
9. **genesis-platform-engineer** — deploy, maintenance, domain, builder + FULL reliability duties
10. **genesis-security-identity** — security analysis and identity management

### Accounting / FinanceOS (9 agents)

1. **genesis-fin-ap** — accounts payable analysis (note: live 502 contract fix needed)
2. **genesis-fin-ar** — accounts receivable analysis
3. **genesis-fin-revenue** — revenue recognition and analysis
4. **genesis-fin-close** — close, journals + intercompany/consolidation guidance
5. **genesis-fin-reconciliation** — reconciliation analysis and review
6. **genesis-fin-controller** — controller, policy arbitration + COGS oversight
7. **genesis-fin-fpa** — financial planning & analysis + pricing-economics inputs
8. **genesis-fin-treasury** — treasury and cash management
9. **genesis-fin-payroll-tax** — payroll, tax + commissions

---

## Implementation Notes

### Code Changes Required

1. **canonical_profiles.py**:
   - Updated `MASTER_OS_SLUGS` to 10 agents (removed `genesis-canon-steward`, `genesis-reliability`)
   - Updated `FINANCEOS_SLUGS` to 9 agents (removed `genesis-fin-cogs`, `genesis-fin-commissions`, `genesis-fin-intercompany`)
   - Updated agent count check from 24 to 19

2. **Bundle files**:
   - Target agents should have their `absorbed_from` field populated with retired agent slugs (if this field pattern continues)
   - Retired bundles remain in history but are not loaded in the 19-agent runtime

3. **Documentation**:
   - `WHITE_LABEL.md` updated to reflect 19-agent roster
   - `CANONICAL_GENESIS_19.md` created as canonical reference
   - This consolidation map documents the transition

### Deployment Impact

- A Render redeploy will serve exactly 19 agents based on the updated `CANONICAL_SLUGS`
- Optional: Alias redirects can map retired slugs to their absorption targets
- E4L-specific legacy bundles remain quarantined in `skill_bundles/e4l-legacy/`

### White-Label Compatibility

All canonical bundles are white-label compatible:
- No E4L branding in canonical packs
- Generic company/entity references only
- Scoped, bounded intelligence without client-specific assumptions

---

## Version History

- **2026-09-12**: Initial consolidation from 24 to 19 canonical agents
- **Previous**: 24-agent roster (12 MasterOS + 12 FinanceOS)

---

**Authority**: This consolidation map is the sole source of truth for the Genesis-19 transition.
**Maintained by**: Genesis platform maintainers
**Status**: Active
