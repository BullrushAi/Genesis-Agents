# MasterOS Specialist Genesis Agents

This directory contains **MasterOS domain specialist agents** from the canonical Genesis V2 architecture. These agents provide deep expertise in cross-functional operational domains.

## Purpose

These 12 MasterOS specialists provide organizational intelligence beyond the core 24 white-label agents, focusing on:

- Cross-functional operational concerns
- Platform and infrastructure management
- Evidence and knowledge management
- Risk, compliance, and security governance
- Integration and automation architecture

## Contents (12 specialists)

All MasterOS canonical domain agents:

1. **genesis-automation-architect** - Automation & Business Process Architecture
2. **genesis-canon-steward** - Canonical Specification & Documentation Stewardship
3. **genesis-commercial-growth** - Commercial Strategy & Growth Operations
4. **genesis-data-intelligence** - Data Strategy & Intelligence Operations
5. **genesis-execution-control** - Execution Coordination & Control
6. **genesis-integration-architect** - Integration Design & API Architecture
7. **genesis-knowledge-evidence** - Knowledge Management & Evidence Verification
8. **genesis-legal-risk** - Legal & Risk Assessment
9. **genesis-people-operations** - People Operations & HR Systems
10. **genesis-platform-engineer** - Platform Engineering & Infrastructure
11. **genesis-reliability** - Site Reliability & Operational Excellence
12. **genesis-security-identity** - Security & Identity Management

## Relationship to Canonical 24

The **canonical 24 white-label Genesis Agents** at `skill_bundles/` root are designed for general business operations. These MasterOS specialists provide **deeper operational and architectural expertise** for organizations that need:

- Platform and infrastructure intelligence
- Cross-domain integration architecture
- Operational risk and compliance governance
- Advanced automation design
- Evidence-based decision support

## Key Characteristics

- **White-label compatible** - Generic, not client-specific
- **Canonical V5 contract** - Follow Genesis structured output standards
- **Operational focus** - Address cross-functional operational concerns
- **Reviewer-ready** - Include reviewer policy for material decisions
- **Evidence-driven** - Return findings based on task-scoped evidence

## Difference from E4L Legacy

Unlike E4L legacy agents, these specialists:

- ✅ Are white-label compatible
- ✅ Follow canonical Genesis V2/V5 specifications
- ✅ Use conditional clarification protocol
- ✅ Respect modern operational boundaries
- ✅ Support multi-tenant deployments

## Difference from Finance Specialists

MasterOS specialists focus on **operational and platform concerns**, while finance specialists (`skill_bundles/finance-specialists/`) focus on **financial domain expertise**. Together with the canonical 24, they form the complete Genesis intelligence layer:

- **Canonical 24** (root) → General business operations
- **Finance Specialists** → Deep financial domain expertise
- **MasterOS Specialists** → Operational platform & cross-functional expertise

## Usage Model

These specialists are typically invoked:

1. **By orchestrators** - Cato or other orchestration layers call them for bounded analysis
2. **For reviews** - Other agents recommend these specialists for review (e.g., genesis-security-identity for security reviews)
3. **For architecture** - When designing integrations, automations, or platform changes
4. **For evidence** - When verifying claims or resolving conflicting information

## Deployment Options

### Option 1: Full MasterOS Suite
Deploy all 12 specialists for comprehensive operational coverage.

### Option 2: Platform-Only Subset
Deploy just the platform/infrastructure specialists (platform-engineer, reliability, integration-architect).

### Option 3: Governance-Only Subset
Deploy just the governance specialists (security-identity, legal-risk, knowledge-evidence).

### Option 4: Exclude MasterOS
For simpler deployments, use only the canonical 24 white-label agents without these specialists.

## Canonical Source

These agents are derived from:

```
source_file: "E4L_GENESIS_AGENT_CANON_MASTER_OS_AND_FINANCEOS_V2.md"
canonical_profile: true
canonical_domain: "master-os"
```

They represent the **operational intelligence layer** of the Genesis architecture, distinct from both white-label business agents and financial specialists.

## Configuration Note

Ensure your deployment configuration includes these agents in the agent roster if you want them available. They are designed to be optional additions to the canonical 24 white-label agents.
