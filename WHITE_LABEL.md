# Genesis Agents — white-label Genesis-19

The active roster has 10 business and 9 finance specialists. The machine-readable authority is canonical_profiles.CANONICAL_SLUGS; role and scope authority is GENESIS_19_ROLE_CONTRACT.md.

GET /agents exposes exactly these 19 identities. Retired public run routes return 410. Historical bundles remain available internally for migration evidence; their presence does not make them public agents.

Five specialist roles are absorbed as documented in AGENT_CONSOLIDATION_MAP.md. Receivers retain the original no-write and no-self-verification boundaries; sources are supplied through scoped bootstrap, not hardwired customer accounts.

Genesis completes bounded analysis and proposals. It does not execute payments, ledger posting, filing, approval, or deployment. Gateway metadata is not proof of tool execution, marketplace reputation, or settlement.

Customer-specific credentials and data are configured separately. See CANONICAL_GENESIS_19.md for the roster and tests/test_genesis19_consolidation.py for role-transfer regressions.
