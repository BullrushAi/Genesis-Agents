# E4L Legacy Genesis Agents

This directory contains **optional enterprise legacy agents** that were built specifically for the Empire4Less (E4L) implementation of Genesis Agents.

## ⚠️ Not Part of White-Label Genesis

These agents are **not part of the canonical 24 white-label Genesis Agents**. They contain:

- E4L-specific branding and references
- Hard-coded entity identifiers (nesllc, xpo, energy4life, nespty)
- E4L-specific accounting contracts and workflows
- Direct Xero integration paths for E4L entities

## Contents (14 agents)

All `genesis-e4l-*` agents:

- genesis-e4l-ap (Accounts Payable)
- genesis-e4l-ar (Accounts Receivable)
- genesis-e4l-cash (Cash Management)
- genesis-e4l-close (Month-End Close)
- genesis-e4l-cogs-cm (COGS & Cost Management)
- genesis-e4l-commissions (Commission Processing)
- genesis-e4l-controller (Controller Functions)
- genesis-e4l-fs-integrity (Financial Statement Integrity)
- genesis-e4l-intercompany (Intercompany Transactions)
- genesis-e4l-journals (Journal Entry Management)
- genesis-e4l-revenue (Revenue Recognition)
- genesis-e4l-shopify (Shopify Integration)
- genesis-e4l-stripe (Stripe Integration)
- genesis-e4l-treasury (Treasury Management)

## Usage

These agents are preserved for:

1. **Backward compatibility** with existing E4L deployments
2. **Reference implementations** for enterprise customizations
3. **Migration support** when transitioning to white-label alternatives

## White-Label Alternative

For generic, white-label functionality, use the **canonical 24 Genesis Agents** at the `skill_bundles/` root, particularly:

- `genesis-finance` - General finance operations
- `genesis-billing` - Billing and invoicing
- `genesis-commerce` - Commerce and payment processing

Or the generic **finance specialists** in `skill_bundles/finance-specialists/` for more specialized financial roles.

## Deployment Note

When deploying Genesis Agents in a white-label context, **exclude this directory** to avoid E4L-specific branding and dependencies.
