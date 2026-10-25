"""Contract tests for the 19 canonical Master OS and FinanceOS profiles."""
from __future__ import annotations

import asyncio

import main
from bundle_loader import BUNDLES_DIR, load_bundle, resolve_bundle_slug
from canonical_profiles import (
    COMMON_EVAL_DIMENSIONS,
    FINANCE_EVAL_DIMENSIONS,
    MODEL_POLICY_FIELDS,
    REVIEW_TRIGGER_IDS,
    iter_canonical_eval_cases,
    load_canonical_eval_suite,
    validate_canonical_profile,
)


MASTER_OS_SLUGS = {
    "genesis-knowledge-evidence",
    "genesis-execution-control",
    "genesis-automation-architect",
    "genesis-integration-architect",
    "genesis-platform-engineer",
    "genesis-data-intelligence",
    "genesis-security-identity",
    "genesis-legal-risk",
    "genesis-commercial-growth",
    "genesis-people-operations",
}

FINANCEOS_SLUGS = {
    "genesis-fin-revenue",
    "genesis-fin-ap",
    "genesis-fin-ar",
    "genesis-fin-reconciliation",
    "genesis-fin-treasury",
    "genesis-fin-close",
    "genesis-fin-fpa",
    "genesis-fin-payroll-tax",
    "genesis-fin-controller",
}

CANONICAL_SLUGS = MASTER_OS_SLUGS | FINANCEOS_SLUGS

MINIMUM_OUTPUT_FIELDS = {
    "agent_id",
    "task_id",
    "scope",
    "status",
    "summary",
    "findings",
    "proposals",
    "evidence_refs",
    "assumptions",
    "conflicts",
    "unknowns",
    "confidence",
    "risk_flags",
    "recommended_next_actions",
    "requested_human_decisions",
    "tool_calls_summary",
}

FINANCE_PROPOSAL_FIELDS = {
    "entity_id",
    "period",
    "transaction_or_schedule_type",
    "proposed_treatment",
    "account_mapping",
    "tracking_mapping",
    "tax_treatment",
    "amount_logic",
    "source_refs",
    "policy_refs",
    "assumptions",
    "confidence",
    "exceptions",
    "requires_human_decision",
    "recommended_financeos_action",
}


def test_exact_canonical_roster_has_19_unique_slugs() -> None:
    assert len(MASTER_OS_SLUGS) == 10
    assert len(FINANCEOS_SLUGS) == 9
    assert len(CANONICAL_SLUGS) == 19


def test_every_canonical_slug_is_catalogued_and_bundle_backed() -> None:
    for slug in sorted(CANONICAL_SLUGS):
        assert slug in main.AGENT_PERSONAS
        assert resolve_bundle_slug(slug) == slug
        assert len(list(BUNDLES_DIR.rglob(f"{slug}.json"))) == 1
        bundle = load_bundle(slug)
        assert bundle is not None
        assert bundle["slug"] == slug
        assert bundle["canonical_profile"] is True
        assert bundle["is_orchestrator"] is False


def test_get_agents_advertises_exact_canonical_roster() -> None:
    payload = asyncio.run(main.list_agents())
    advertised = {agent["slug"] for agent in payload["agents"]}
    assert advertised == CANONICAL_SLUGS


def test_every_canonical_bundle_encodes_shared_fail_closed_contract() -> None:
    for slug in sorted(CANONICAL_SLUGS):
        bundle = load_bundle(slug)
        assert bundle is not None
        prompt = bundle["system_prompt"].lower()
        assert "cato chooses and bounds the job" in prompt
        assert "no private durable memory" in prompt
        assert "fail closed" in prompt
        assert "never convert a timeout or stub response into success" in prompt
        assert "never guess a consequential identifier" in prompt
        assert "never silently broaden scope" in prompt
        assert "never self-verify material work" in prompt
        assert MINIMUM_OUTPUT_FIELDS <= set(bundle["output_shape_hint"])
        assert bundle["role_restrictions"]
        assert bundle["role_restrictions"][0] in bundle["system_prompt"]


def test_role_restrictions_are_profile_specific() -> None:
    restrictions = {
        load_bundle(slug)["role_restrictions"][0]  # type: ignore[index]
        for slug in CANONICAL_SLUGS
    }
    assert len(restrictions) == len(CANONICAL_SLUGS)


def test_finance_profiles_are_proposal_only_and_have_no_execution_tools() -> None:
    forbidden_tools = {
        "conduit",
        "github_tool",
        "workspace_shell",
        "finance_tool",
        "billing_tool",
        "commerce_tool",
        "pricing_tool",
        "send_email",
    }
    for slug in sorted(FINANCEOS_SLUGS):
        bundle = load_bundle(slug)
        assert bundle is not None
        assert bundle["canonical_domain"] == "financeos"
        assert set(bundle["tools_advertised"]) <= {"file_write"}
        assert not (set(bundle["tools_advertised"]) & forbidden_tools)
        assert bundle["conduit_budget_cents"] == 0
        prompt = bundle["system_prompt"].lower()
        assert "financeos owns all finance workflow and control state" in prompt
        assert "xero is the official ledger" in prompt
        assert "never post to xero" in prompt
        assert "never" in prompt and "move money" in prompt
        assert "deterministic financeos worker" in prompt
        assert "finance_proposal" in bundle["output_shape_hint"]
        assert all(field in prompt for field in FINANCE_PROPOSAL_FIELDS)
        assert "tool_calls_summary, finance_proposal." in prompt
        assert "finance_proposal is a required top-level object" in prompt
        assert "every declared subfield" in prompt
        assert "use null for scalar subfields and empty arrays for list subfields" in prompt
        assert "never invent facts, identifiers, amounts, treatments, mappings" in prompt


def test_master_profiles_do_not_advertise_finance_or_provider_write_tools() -> None:
    forbidden_tools = {
        "conduit",
        "github_tool",
        "workspace_shell",
        "finance_tool",
        "billing_tool",
        "commerce_tool",
        "pricing_tool",
        "send_email",
    }
    for slug in sorted(MASTER_OS_SLUGS):
        bundle = load_bundle(slug)
        assert bundle is not None
        assert bundle["canonical_domain"] == "master-os"
        assert not (set(bundle["tools_advertised"]) & forbidden_tools)
        assert bundle["conduit_budget_cents"] == 0


def test_every_canonical_profile_has_a_complete_model_policy() -> None:
    suite_paths: set[str] = set()
    for slug in sorted(CANONICAL_SLUGS):
        bundle = load_bundle(slug)
        assert bundle is not None
        policy = bundle["model_policy"]
        assert MODEL_POLICY_FIELDS <= policy.keys()
        assert policy["primary_model"]
        assert policy["fallback_models"]
        assert 0 < policy["min_score"] <= 1
        assert policy["max_cost_per_task"] > 0
        assert policy["max_latency"] > 0
        assert policy["max_cost_unit"] == "cents"
        assert policy["max_latency_unit"] == "seconds"
        for flag in (
            "tool_use_required",
            "long_context_required",
            "code_execution_required",
        ):
            assert isinstance(policy[flag], bool)
        suite_paths.add(policy["evaluation_suite"])
    assert len(suite_paths) == 19


def test_reviewer_policy_exposes_all_mandatory_triggers_but_reserves_dispatch_to_cato() -> None:
    for slug in sorted(CANONICAL_SLUGS):
        bundle = load_bundle(slug)
        assert bundle is not None
        policy = bundle["reviewer_policy"]
        assert policy["enforcement_owner"] == "Cato"
        assert policy["genesis_may_dispatch"] is False
        assert policy["consensus_is_not_authority"] is True
        assert "only Cato may dispatch" in policy["enforcement_semantics"]
        assert {trigger["id"] for trigger in policy["triggers"]} == REVIEW_TRIGGER_IDS
        assert all(trigger["reviewer_slugs"] for trigger in policy["triggers"])


def test_each_role_has_a_distinct_loadable_behavioral_eval_suite() -> None:
    scenarios: set[str] = set()
    case_ids: set[str] = set()
    for slug in sorted(CANONICAL_SLUGS):
        suite = load_canonical_eval_suite(slug)
        assert suite["agent_slug"] == slug
        assert suite["logical_role"] == load_bundle(slug)["logical_role"]  # type: ignore[index]
        scenarios.add(suite["role_scenario"])
        expected_dimensions = set(COMMON_EVAL_DIMENSIONS)
        if slug in FINANCEOS_SLUGS:
            expected_dimensions.update(FINANCE_EVAL_DIMENSIONS)
            assert suite["expected_contract"]["finance_proposal_only"] is True
        else:
            assert suite["expected_contract"]["finance_proposal_only"] is False
        cases = list(iter_canonical_eval_cases(slug))
        assert {case["dimension"] for case in cases} == expected_dimensions
        assert all(case["pass_conditions"] for case in cases)
        assert all(case["expected_contract"]["review_enforcement_owner"] == "Cato" for case in cases)
        assert all(case["expected_contract"]["genesis_may_dispatch_reviewer"] is False for case in cases)
        for case in cases:
            assert case["case_id"] not in case_ids
            case_ids.add(case["case_id"])
    assert len(scenarios) == 19


def test_runtime_validator_accepts_all_19_canonical_profiles() -> None:
    errors = {slug: validate_canonical_profile(slug) for slug in CANONICAL_SLUGS}
    assert errors == {slug: [] for slug in CANONICAL_SLUGS}


def test_eval_suite_paths_are_direct_json_files() -> None:
    for slug in CANONICAL_SLUGS:
        path = load_bundle(slug)["model_policy"]["evaluation_suite"]  # type: ignore[index]
        assert path.startswith("eval/canonical_profiles/")
        assert path.endswith(".json")
