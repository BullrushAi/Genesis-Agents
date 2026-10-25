# Genesis production certification

Current certification remains RED while the September 2026 repair is verified. Local passing tests do not establish production readiness.

Current roster: 19 agents (10 MasterOS, 9 FinanceOS). Active role authority: GENESIS_19_ROLE_CONTRACT.md. Evaluation code and datasets: eval/canonical.

The external tracing integration has been removed at the user direction. Verify exact Git/deployment identity, all 19 provider responses, API authentication, signed job ownership, native Cato continuation and applicable business outcomes using independent receipts. No retired hosted tracing evidence is a current launch gate.

Historical documents under proof-artifacts remain immutable evidence of their original revisions.


## Release-gate repair (October 2026)

Production may be declared GREEN only from the repository's
`.github/workflows/production-certification.yml` workflow against the exact
40-character SHA reported by `GET /health/worker`. A passing local/CI suite is
necessary but not sufficient.

The live certification lane must prove, on the same deployed SHA:

- authenticated positive access plus missing/invalid credential rejection;
- exactly 19 canonical live agents;
- a real non-`testContext` provider run for all 19 agents, including
  `genesis-fin-close`, `genesis-fin-controller`,
  `genesis-fin-payroll-tax`, and `genesis-fin-revenue`;
- live rejection of a permanently prohibited finance/money capability;
- a durable async job reaching a delivered terminal state;
- worker ownership is explicit and healthy.

### Worker topology

The standalone Render service `swarmsync-agents-worker` is not required when
the web service owns queue processing through `GENESIS_WORKER_ENABLED=true`,
or when an explicitly configured Trigger.dev mode owns dispatch. The public
`/health/worker` response is the release authority: it reports
`worker_mode`, `in_process_worker_enabled`, `separate_worker_required`, and
`worker_intent`. A suspended standalone worker is acceptable only when
`separate_worker_required=false`; otherwise certification must fail.

The finance result contract permits representation-only normalization of
omitted nullable/list `finance_proposal` members to explicit `null`/`[]`.
That normalization is not permission to invent identifiers, amounts,
treatments, approvals, evidence, or other business facts.
