"""Load and validate canonical Genesis profile metadata and behavioral eval suites.

This module deliberately does not dispatch reviewers. Canonical profiles report review
requirements; Cato remains the only enforcement owner and dispatcher.
"""
from __future__ import annotations

import json
import hashlib
import re
import sys
import copy
from pathlib import Path
from typing import Any, Iterator

from bundle_loader import load_bundle
from canonical_result_contract import (
    CANONICAL_AGENT_IDS,
    CANONICAL_RESULT_CONTRACT_VERSION,
    CANONICAL_STATUSES,
    CLARIFICATION_FIELDS,
    COMMON_RESULT_FIELDS,
    FINANCE_PROPOSAL_FIELDS,
    NARRATIVE_INTENT_PREFIXES,
    validate_canonical_result,
)
from runtime.tool_policy import HUMAN_ONLY_TOOLS, PROHIBITION_GROUPS, SLUG_ALLOWED_RISKS


PROJECT_ROOT = Path(__file__).resolve().parent
CANONICAL_EVAL_DIR = PROJECT_ROOT / "eval" / "canonical_profiles"

MASTER_OS_SLUGS = frozenset(
    {
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
)

FINANCEOS_SLUGS = frozenset(
    {
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
)

CANONICAL_SLUGS = MASTER_OS_SLUGS | FINANCEOS_SLUGS
CANONICAL_SOURCE_FILE = "GENESIS_19_ROLE_CONTRACT.md"
if CANONICAL_SLUGS != CANONICAL_AGENT_IDS:
    raise RuntimeError("canonical roster drift between profile and result-contract authorities")


def canonical_agent_inventory() -> tuple[dict[str, Any], ...]:
    """Return the generated public Genesis inventory in stable slug order.

    ``CANONICAL_SLUGS`` is the sole roster declaration.  Every public surface
    consumes this projection rather than independently scanning personas or
    bundle files.
    """
    inventory: list[dict[str, Any]] = []
    for slug in sorted(CANONICAL_SLUGS):
        bundle = load_bundle(slug)
        if bundle is None:
            raise CanonicalProfileError(f"canonical bundle not found: {slug}")
        if bundle.get("slug") != slug:
            raise CanonicalProfileError(f"canonical bundle identity mismatch: {slug}")
        inventory.append({"slug": slug, "name": bundle.get("name", slug)})
    return tuple(inventory)


def check_canonical_inventory() -> dict[str, Any]:
    """Machine-readable, offline source/bundle identity self-check."""
    inventory = canonical_agent_inventory()
    slugs = [entry["slug"] for entry in inventory]
    errors = {
        slug: profile_errors
        for slug in slugs
        if (profile_errors := validate_canonical_profile(slug))
    }
    return {
        "ok": not errors and len(slugs) == 19 and len(slugs) == len(set(slugs)),
        "count": len(slugs),
        "slugs": slugs,
        "profile_errors": errors,
        "authority": "canonical_profiles.CANONICAL_SLUGS",
    }

MODEL_POLICY_FIELDS = frozenset(
    {
        "primary_model",
        "fallback_models",
        "evaluation_suite",
        "min_score",
        "max_cost_per_task",
        "max_latency",
        "tool_use_required",
        "long_context_required",
        "code_execution_required",
    }
)

REVIEW_TRIGGER_IDS = frozenset(
    {
        "material_accounting_policy_ambiguity",
        "legal_or_compliance_changes_action",
        "security_permissions_broadened",
        "material_production_incident",
        "consequential_write_automation",
        "material_evidence_conflict",
        "material_cross_entity_finance",
        "deployment_completion_without_independent_proof",
    }
)

COMMON_EVAL_DIMENSIONS = frozenset(
    {
        "task_correctness",
        "source_correctness",
        "scope_discipline",
        "permission_compliance",
        "abstention_quality",
        "conflict_detection",
        "hallucination_rate",
        "tool_selection_accuracy",
        "structured_output_validity",
        "latency",
        "cost",
        "escalation_correctness",
        "analysis_complete_semantics",
        "clarification_precision",
        "candidate_resolution_quality",
        "materiality_correctness",
        "urgency_correctness",
        "human_resolver_correctness",
        "source_exhaustion_before_clarification",
        "no_unnecessary_human_question",
        "no_direct_employee_interruption",
        "privacy_capture_scope",
        "no_private_durable_clarification_memory",
        "no_self_verification",
    }
)

FINANCE_EVAL_DIMENSIONS = frozenset(
    {
        "entity_binding",
        "period_binding",
        "coa_mapping",
        "tracking_mapping",
        "tax_mapping",
        "policy_version_use",
        "duplicate_sensitivity",
        "no_write_enforcement",
        "hold_correctness",
        "evidence_completeness",
    }
)


class CanonicalProfileError(ValueError):
    """Raised when canonical metadata or its eval suite is incomplete or unsafe."""


INSTRUCTION_INVARIANT_CATEGORIES = frozenset(
    {"role", "primary_reasoning", "source_tool_discipline", "authority_risk_boundary"}
)


def _canonical_spec_role_sections() -> dict[str, tuple[str, str]]:
    """Return exact roster section bodies keyed by canonical slug."""
    path = PROJECT_ROOT / CANONICAL_SOURCE_FILE
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CanonicalProfileError(f"canonical source unreadable: {CANONICAL_SOURCE_FILE}") from exc
    heading = re.compile(
        r"^##\s+(?P<section>3\.\d+|F\d+)\.?\s+`(?P<slug>genesis-[^`]+)`\s*$",
        re.MULTILINE,
    )
    matches = list(heading.finditer(source))
    sections: dict[str, tuple[str, str]] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        sections[match.group("slug")] = (match.group("section"), source[match.end():end])
    if set(sections) != set(CANONICAL_SLUGS):
        raise CanonicalProfileError("canonical source roster sections do not match exact 19")
    return sections


def _source_block(section: str, start: str, end: str | None) -> str:
    pattern = re.escape(start) + r"\s*(.*?)"
    pattern += re.escape(end) if end else r"(?:\n---|\Z)"
    match = re.search(pattern, section, re.DOTALL)
    if not match:
        raise CanonicalProfileError(f"canonical source block missing: {start}")
    return match.group(1).strip()


def _source_bullets(section: str, start: str, end: str | None) -> list[str]:
    block = _source_block(section, start, end)
    return [item.strip() for item in re.findall(r"^-\s+(.+?)\s*$", block, re.MULTILINE)]


def canonical_source_instruction_invariants(slug: str) -> list[dict[str, Any]]:
    """Derive four compact invariants directly from the slug's authoritative V2 section."""
    if slug not in CANONICAL_SLUGS:
        raise CanonicalProfileError(f"not a canonical slug: {slug}")
    anchor, section = _canonical_spec_role_sections()[slug]
    role_quote = " ".join(_source_block(section, "**Role**", "**Primary instructions**").split())
    primary = _source_bullets(section, "**Primary instructions**", "**Tools**")
    tools = _source_bullets(section, "**Tools**", "**Permissions**")
    risks = _source_bullets(section, "**May not do**", None)
    if not role_quote or not primary or not tools or not risks:
        raise CanonicalProfileError(f"canonical source instruction blocks incomplete: {slug}")

    risk_quote = risks[0].rstrip(".")
    invariants = [
        {
            "id": f"{slug}:role",
            "category": "role",
            "spec_anchor": f"{anchor} Role",
            "source_quote": role_quote,
            "instruction": f"Operate only within this canonical role: {role_quote}",
            "distinctive_primary": False,
        },
        {
            "id": f"{slug}:primary-reasoning",
            "category": "primary_reasoning",
            "spec_anchor": f"{anchor} Primary instructions",
            "source_quote": primary[0],
            "instruction": primary[0],
            "distinctive_primary": True,
        },
        {
            "id": f"{slug}:source-tool-discipline",
            "category": "source_tool_discipline",
            "spec_anchor": f"{anchor} Tools",
            "source_quote": tools[0],
            "instruction": f"Use only task-scoped access to this canonical source or capability: {tools[0]}.",
            "distinctive_primary": False,
        },
        {
            "id": f"{slug}:authority-risk-boundary",
            "category": "authority_risk_boundary",
            "spec_anchor": f"{anchor} May not do",
            "source_quote": risks[0],
            "instruction": f"Within this Genesis role, do not {risk_quote}.",
            "distinctive_primary": False,
        },
    ]
    return invariants


PROFILE_CHAIN_FIELDS = (
    "role",
    "instructions",
    "scoped_bootstrap",
    "allowed_tools",
    "denied_tools",
    "permissions",
    "output_schema",
    "model_policy",
    "risk_boundary",
    "clarification_behavior",
    "eval_suite",
)


def canonical_profile_chain(slug: str) -> dict[str, Any]:
    """Project the complete, source-bound runtime/eval chain for one canonical role."""
    if slug not in CANONICAL_SLUGS:
        raise CanonicalProfileError(f"not a canonical slug: {slug}")
    bundle = load_bundle(slug)
    if not bundle:
        raise CanonicalProfileError(f"canonical bundle not found: {slug}")
    prompt = str(bundle.get("system_prompt") or "")
    tools = list(bundle.get("tools_advertised") or [])
    clarification = dict(bundle.get("clarification_policy") or {})
    return {
        "role": bundle.get("logical_role"),
        "instructions": {
            "source_field": "system_prompt",
            "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "instruction_invariants": list(bundle.get("instruction_invariants") or []),
        },
        "scoped_bootstrap": {
            "source": "task.resolved_context",
            "required_type": "list",
            "max_items": 8,
            "tenant_binding_required": True,
            "evidence_fields": ["citation", "content_hash", "content"],
            "source_exhaustion_before_clarification": True,
        },
        "allowed_tools": tools,
        "denied_tools": sorted(PROHIBITION_GROUPS),
        "permissions": {
            "allowed_risk_classes": sorted(SLUG_ALLOWED_RISKS[slug]),
            "provider_write_allowed": False,
            "finance_write_allowed": False,
            "direct_human_contact_allowed": False,
            "durable_private_memory_allowed": False,
            "workspace_artifact_write_allowed": "file_write" in tools,
            "human_only_tools": sorted(HUMAN_ONLY_TOOLS),
        },
        "output_schema": {
            "contract_version": CANONICAL_RESULT_CONTRACT_VERSION,
            "supported_fields": list(bundle.get("output_shape_hint") or []),
            "allowed_statuses": list(CANONICAL_STATUSES),
            "finance_proposal_required": slug in FINANCEOS_SLUGS,
            "conditional_clarification": True,
        },
        "model_policy": dict(bundle.get("model_policy") or {}),
        "risk_boundary": list(bundle.get("role_restrictions") or []),
        "clarification_behavior": clarification,
        "eval_suite": (bundle.get("model_policy") or {}).get("evaluation_suite"),
    }


def _safe_suite_path(relative_path: str) -> Path:
    candidate = (PROJECT_ROOT / relative_path).resolve()
    eval_root = CANONICAL_EVAL_DIR.resolve()
    if candidate.parent != eval_root or candidate.suffix.lower() != ".json":
        raise CanonicalProfileError("evaluation_suite must be a direct canonical eval file")
    return candidate


def load_canonical_eval_suite(slug: str) -> dict[str, Any]:
    """Load the eval suite declared by a canonical bundle and validate its identity."""
    if slug not in CANONICAL_SLUGS:
        raise CanonicalProfileError(f"not a canonical slug: {slug}")
    bundle = load_bundle(slug)
    if not bundle:
        raise CanonicalProfileError(f"canonical bundle not found: {slug}")
    policy = bundle.get("model_policy")
    if not isinstance(policy, dict):
        raise CanonicalProfileError(f"model_policy missing: {slug}")
    relative_path = policy.get("evaluation_suite")
    if not isinstance(relative_path, str) or not relative_path:
        raise CanonicalProfileError(f"evaluation_suite missing: {slug}")
    path = _safe_suite_path(relative_path)
    if not path.is_file():
        raise CanonicalProfileError(f"evaluation suite not found: {relative_path}")
    try:
        suite = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CanonicalProfileError(f"evaluation suite unreadable: {relative_path}") from exc
    if suite.get("agent_slug") != slug:
        raise CanonicalProfileError(f"evaluation suite identity mismatch: {slug}")
    return suite


def _prefix_contract_fixture_result(value: Any) -> Any:
    """Project legacy synthetic eval fixtures into the current prefix contract."""
    result = copy.deepcopy(value)
    if not isinstance(result, dict):
        return result

    def apply(container: dict[str, Any], field: str, path: str) -> None:
        item = container.get(field)
        prefix = NARRATIVE_INTENT_PREFIXES[path] + ": "
        if isinstance(item, str) and not item.startswith(prefix):
            container[field] = prefix + item
        elif isinstance(item, list):
            container[field] = [
                entry if not isinstance(entry, str) or entry.startswith(prefix) else prefix + entry
                for entry in item
            ]

    for field in (
        "summary", "findings", "proposals", "assumptions", "conflicts", "unknowns",
        "risk_flags", "recommended_next_actions", "requested_human_decisions",
        "candidate_resolution", "reason_human_needed", "recommended_human_resolver",
    ):
        apply(result, field, field)
    attempts = result.get("resolution_attempts")
    if isinstance(attempts, list):
        for attempt in attempts:
            if isinstance(attempt, dict):
                apply(attempt, "result", "resolution_attempts.result")
    finance = result.get("finance_proposal")
    if isinstance(finance, dict):
        for field in (
            "transaction_or_schedule_type", "proposed_treatment", "account_mapping",
            "tracking_mapping", "tax_treatment", "amount_logic", "assumptions",
            "exceptions", "recommended_financeos_action",
        ):
            apply(finance, field, f"finance_proposal.{field}")
    return result


def validate_canonical_profile(slug: str) -> list[str]:
    """Return all contract violations for one canonical profile; an empty list is valid."""
    errors: list[str] = []
    bundle = load_bundle(slug)
    if not bundle:
        return [f"bundle missing: {slug}"]

    if bundle.get("source_file") != CANONICAL_SOURCE_FILE:
        errors.append("source_file must point to the current V2 canon")
    if bundle.get("output_contract_version") != CANONICAL_RESULT_CONTRACT_VERSION:
        errors.append("output_contract_version must be genesis-v5")
    try:
        expected_invariants = canonical_source_instruction_invariants(slug)
    except CanonicalProfileError as exc:
        errors.append(str(exc))
        expected_invariants = []
    invariants = bundle.get("instruction_invariants")
    if invariants != expected_invariants:
        errors.append("instruction_invariants do not match the authoritative V2 role section")
    if not isinstance(invariants, list) or len(invariants) < 3:
        errors.append("instruction_invariants must contain at least three source-bound requirements")
    else:
        categories = {row.get("category") for row in invariants if isinstance(row, dict)}
        instructions = [row.get("instruction") for row in invariants if isinstance(row, dict)]
        distinctive = [row for row in invariants if isinstance(row, dict) and row.get("distinctive_primary") is True]
        if categories != INSTRUCTION_INVARIANT_CATEGORIES:
            errors.append("instruction_invariants categories are incomplete")
        if len(instructions) != len(set(instructions)) or any(not isinstance(item, str) or len(item) < 20 for item in instructions):
            errors.append("instruction_invariants must be meaningful and distinct")
        if len(distinctive) != 1 or distinctive[0].get("category") != "primary_reasoning":
            errors.append("instruction_invariants need exactly one distinctive Primary Instruction")
    output_fields = bundle.get("output_shape_hint")
    supported_output_fields = set(COMMON_RESULT_FIELDS) | set(CLARIFICATION_FIELDS)
    required_output_fields = set(COMMON_RESULT_FIELDS)
    if slug in FINANCEOS_SLUGS:
        supported_output_fields.add("finance_proposal")
        required_output_fields.add("finance_proposal")
    if not isinstance(output_fields, list) or set(output_fields) != supported_output_fields:
        errors.append("output_shape_hint does not match the profile-aware V5 contract")
    clarification_policy = bundle.get("clarification_policy")
    if not isinstance(clarification_policy, dict):
        errors.append("clarification_policy must be an object")
    else:
        if clarification_policy.get("source_exhaustion_required") is not True:
            errors.append("clarification policy must require scoped source exhaustion")
        if clarification_policy.get("minimum_independent_attempts") != 2:
            errors.append("clarification policy must require two independent resolution attempts")
        if clarification_policy.get("direct_human_contact_allowed") is not False:
            errors.append("clarification policy must prohibit direct human contact")
        if clarification_policy.get("durable_private_memory_allowed") is not False:
            errors.append("clarification policy must prohibit private durable memory")
        if clarification_policy.get("orchestration_owner") != "MasterOS/Cato Clarification Engine":
            errors.append("clarification orchestration must remain outside Genesis")
    prompt = str(bundle.get("system_prompt") or "")
    if isinstance(invariants, list):
        for invariant in invariants:
            instruction = invariant.get("instruction") if isinstance(invariant, dict) else None
            if not isinstance(instruction, str) or instruction not in prompt:
                errors.append("system_prompt missing an enforced instruction_invariant")
                break
        distinctive_texts = [
            row.get("instruction") for row in invariants
            if isinstance(row, dict) and row.get("distinctive_primary") is True
        ]
        for instruction in distinctive_texts:
            leaked = [
                other for other in CANONICAL_SLUGS
                if other != slug and instruction in str((load_bundle(other) or {}).get("system_prompt") or "")
            ]
            if leaked:
                errors.append("distinctive Primary Instruction leaked across canonical profiles")
    for clause in (
        "ANALYSIS_COMPLETE means only the bounded Genesis reasoning assignment completed",
        "Before clarification_needed=true, exhaust authorized scoped inputs, sources, and capabilities",
        "Never message an employee directly",
        "Never persist a clarification ledger or employee answer in private durable memory",
    ):
        if clause not in prompt:
            errors.append(f"system_prompt missing V5 boundary: {clause}")

    profile_chain = bundle.get("profile_chain")
    if not isinstance(profile_chain, dict) or tuple(profile_chain) != PROFILE_CHAIN_FIELDS:
        errors.append("profile_chain must encode the ordered canonical chain")
    elif profile_chain != canonical_profile_chain(slug):
        errors.append("profile_chain does not match authoritative runtime/profile fields")

    model_policy = bundle.get("model_policy")
    if not isinstance(model_policy, dict):
        errors.append("model_policy must be an object")
    else:
        missing = MODEL_POLICY_FIELDS - model_policy.keys()
        if missing:
            errors.append(f"model_policy fields missing: {sorted(missing)}")
        if not isinstance(model_policy.get("primary_model"), str) or not model_policy.get("primary_model"):
            errors.append("primary_model must be non-empty")
        fallbacks = model_policy.get("fallback_models")
        if not isinstance(fallbacks, list) or not fallbacks or not all(isinstance(v, str) and v for v in fallbacks):
            errors.append("fallback_models must contain non-empty model ids")
        score = model_policy.get("min_score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 < score <= 1:
            errors.append("min_score must be within (0, 1]")
        for field in ("max_cost_per_task", "max_latency"):
            value = model_policy.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                errors.append(f"{field} must be positive")
        for field in ("tool_use_required", "long_context_required", "code_execution_required"):
            if not isinstance(model_policy.get(field), bool):
                errors.append(f"{field} must be boolean")

    reviewer_policy = bundle.get("reviewer_policy")
    if not isinstance(reviewer_policy, dict):
        errors.append("reviewer_policy must be an object")
    else:
        if reviewer_policy.get("enforcement_owner") != "Cato":
            errors.append("reviewer enforcement_owner must be Cato")
        if reviewer_policy.get("genesis_may_dispatch") is not False:
            errors.append("Genesis reviewer dispatch must be disabled")
        if reviewer_policy.get("consensus_is_not_authority") is not True:
            errors.append("reviewer policy must reject consensus as authority")
        semantics = reviewer_policy.get("enforcement_semantics")
        if not isinstance(semantics, str) or "only Cato may dispatch" not in semantics:
            errors.append("reviewer enforcement semantics must reserve dispatch to Cato")
        triggers = reviewer_policy.get("triggers")
        if not isinstance(triggers, list):
            errors.append("reviewer triggers must be a list")
        else:
            ids = {item.get("id") for item in triggers if isinstance(item, dict)}
            if ids != REVIEW_TRIGGER_IDS:
                errors.append("reviewer triggers do not match the canonical mandatory set")
            for item in triggers:
                if not isinstance(item, dict) or not item.get("reviewer_slugs"):
                    errors.append("every reviewer trigger requires recommended reviewer slugs")
                    break

    try:
        suite = load_canonical_eval_suite(slug)
    except CanonicalProfileError as exc:
        errors.append(str(exc))
    else:
        if suite.get("schema_version") != 2:
            errors.append("evaluation suite schema_version must be 2")
        if suite.get("output_contract_version") != CANONICAL_RESULT_CONTRACT_VERSION:
            errors.append("evaluation suite output_contract_version mismatch")
        if suite.get("logical_role") != bundle.get("logical_role"):
            errors.append("evaluation suite logical_role mismatch")
        if suite.get("instruction_invariants") != expected_invariants:
            errors.append("evaluation suite instruction_invariants mismatch")
        expected_domain = "financeos" if slug in FINANCEOS_SLUGS else "master-os"
        if suite.get("domain") != expected_domain:
            errors.append("evaluation suite domain mismatch")
        scenario = suite.get("role_scenario")
        if not isinstance(scenario, str) or len(scenario.strip()) < 20:
            errors.append("evaluation suite needs a substantive role_scenario")
        dimensions = suite.get("dimensions")
        if not isinstance(dimensions, list):
            errors.append("evaluation suite dimensions must be a list")
        else:
            names = {item.get("name") for item in dimensions if isinstance(item, dict)}
            required = set(COMMON_EVAL_DIMENSIONS)
            if slug in FINANCEOS_SLUGS:
                required.update(FINANCE_EVAL_DIMENSIONS)
            if names != required:
                errors.append("evaluation dimensions do not match the canonical set")
            for item in dimensions:
                conditions = item.get("pass_conditions") if isinstance(item, dict) else None
                if not isinstance(conditions, list) or not conditions:
                    errors.append("every evaluation dimension needs pass_conditions")
                    break
        expected_contract = suite.get("expected_contract")
        if not isinstance(expected_contract, dict):
            errors.append("evaluation suite expected_contract must be an object")
        else:
            if expected_contract.get("allowed_statuses") != list(CANONICAL_STATUSES):
                errors.append("evaluation allowed_statuses do not match V5")
            if expected_contract.get("output_contract_version") != CANONICAL_RESULT_CONTRACT_VERSION:
                errors.append("evaluation expected contract version mismatch")
            if set(expected_contract.get("required_output_fields") or []) != required_output_fields:
                errors.append("evaluation required output fields mismatch")
            if expected_contract.get("conditional_clarification") is not True:
                errors.append("evaluation must declare conditional clarification")
            if set(expected_contract.get("clarification_fields") or []) != set(CLARIFICATION_FIELDS):
                errors.append("evaluation clarification fields mismatch")
            expected_finance = list(FINANCE_PROPOSAL_FIELDS) if slug in FINANCEOS_SLUGS else []
            if expected_contract.get("finance_proposal_fields") != expected_finance:
                errors.append("evaluation finance proposal fields mismatch")
        fixtures = suite.get("contract_fixtures")
        fixture_kinds = {
            fixture.get("kind")
            for fixture in fixtures
            if isinstance(fixture, dict)
        } if isinstance(fixtures, list) else set()
        if fixture_kinds != {"normal", "material_clarification", "unsupported_clarification"}:
            errors.append("evaluation suite must contain the three V5 contract fixtures")
        elif isinstance(fixtures, list):
            for fixture in fixtures:
                if fixture.get("agent_slug") != slug:
                    errors.append("contract fixture identity mismatch")
                    continue
                verdict = validate_canonical_result(
                    slug, _prefix_contract_fixture_result(fixture.get("result")),
                )
                expected_valid = fixture.get("expected_valid")
                if not isinstance(expected_valid, bool) or verdict.ok is not expected_valid:
                    errors.append(f"contract fixture verdict mismatch: {fixture.get('kind')}")
                if expected_valid is False and fixture.get("expected_reason") != verdict.reason:
                    errors.append(f"contract fixture reason mismatch: {fixture.get('kind')}")
    return errors


def iter_canonical_eval_cases(slug: str) -> Iterator[dict[str, Any]]:
    """Expand a role fixture into one executable case per required dimension."""
    errors = validate_canonical_profile(slug)
    if errors:
        raise CanonicalProfileError(f"invalid canonical profile {slug}: {'; '.join(errors)}")
    suite = load_canonical_eval_suite(slug)
    for dimension in suite["dimensions"]:
        yield {
            "case_id": f"{slug}::{dimension['name']}",
            "agent_slug": slug,
            "dimension": dimension["name"],
            "input": suite["role_scenario"],
            "pass_conditions": list(dimension["pass_conditions"]),
            "expected_contract": dict(suite["expected_contract"]),
        }


if __name__ == "__main__":
    result = check_canonical_inventory()
    print(json.dumps(result, indent=2, sort_keys=True))
    sys.exit(0 if result["ok"] else 1)
