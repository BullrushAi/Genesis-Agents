# Finance Specialist Genesis Agents

This directory contains **generic finance specialist agents** that provide deep expertise in specific financial domains. These agents follow the canonical FinanceOS V5 contract and are designed to be white-label compatible.

## Purpose

These 12 finance specialists provide granular financial intelligence for organizations that need:

- Specialized financial expertise beyond the general-purpose `genesis-finance` agent
- Domain-specific analysis and proposals (AP, AR, reconciliation, treasury, etc.)
- Structured output following the Genesis V5 result contract
- Integration with FinanceOS governance and control frameworks

## Contents (12 specialists)

All `genesis-fin-*` agents:

1. **genesis-fin-ap** - Accounts Payable & Vendor Accounting
2. **genesis-fin-ar** - Accounts Receivable & Customer Billing
3. **genesis-fin-close** - Period Close & Financial Reporting
4. **genesis-fin-cogs** - Cost of Goods Sold Analysis
5. **genesis-fin-commissions** - Commission Calculation & Tracking
6. **genesis-fin-controller** - Controller Review & Policy Guidance
7. **genesis-fin-fpa** - Financial Planning & Analysis
8. **genesis-fin-intercompany** - Intercompany Transaction Management
9. **genesis-fin-payroll-tax** - Payroll & Tax Compliance
10. **genesis-fin-reconciliation** - Account Reconciliation
11. **genesis-fin-revenue** - Revenue Recognition & Analysis
12. **genesis-fin-treasury** - Treasury & Cash Management

## Key Characteristics

- **White-label compatible** - No hard-coded client branding
- **Analysis-only** - Propose treatments, never write directly to financial systems
- **Evidence-based** - Return structured findings with confidence levels
- **Governed** - Respect FinanceOS control boundaries
- **Canonical V5 contract** - Consistent result structure across all specialists

## Usage Model

These specialists are designed to be:

1. **Task-scoped** - Called with specific bounded financial questions
2. **Evidence-driven** - Work from provided FinanceOS data products or authorized sources
3. **Proposal-focused** - Return structured proposals, not executed transactions
4. **Review-aware** - Recommend human review when material ambiguity exists

## Difference from E4L Legacy

Unlike the E4L legacy agents (`skill_bundles/e4l-legacy/`), these specialists:

- ✅ Are white-label compatible (no E4L branding)
- ✅ Follow canonical Genesis V5 output contract
- ✅ Work with generic FinanceOS data products
- ✅ Use conditional clarification protocol
- ✅ Respect modern permission boundaries

## Difference from Canonical 24

The canonical 24 white-label agents include **genesis-finance** at the root, which is a general-purpose finance agent. These finance specialists provide deeper domain expertise when needed, similar to how a company might have both a CFO (genesis-finance) and specialized department heads (genesis-fin-ap, genesis-fin-ar, etc.).

## Deployment Options

### Option 1: Include All Finance Specialists
Deploy all 12 specialists for comprehensive financial coverage.

### Option 2: Select Subset
Deploy only the specialists relevant to your organization's needs (e.g., just AP, AR, and reconciliation for a simpler operation).

### Option 3: Use Only genesis-finance
For basic financial needs, the canonical `genesis-finance` agent at the root may be sufficient without these specialists.

## Configuration Note

These agents respect the canonical Genesis agent roster defined in your deployment configuration. Ensure your `main.py` or agent loader includes these specialists if you want them available at runtime.
