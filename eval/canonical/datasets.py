"""Dataset loading and strict schema validation for Genesis local evaluation experiments.

Each JSONL row follows the local evaluator portable dataset shape: ``input``,
``expected_output``, and ``metadata``.  Validation intentionally fails closed so a
malformed fixture cannot silently disappear from an experiment.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from canonical_result_contract import (
    CANONICAL_AGENT_IDS,
    CANONICAL_RESULT_CONTRACT_VERSION,
    CANONICAL_STATUSES,
    COMMON_RESULT_FIELDS,
    FINANCE_AGENT_IDS,
)


DATASET_PATH = Path(__file__).with_name("genesis_24.jsonl")
TOOL_DATASET_PATH = Path(__file__).with_name("genesis_tool_matrix.jsonl")
CATEGORIES = (
    "happy",
    "boundary",
    "ambiguous",
    "incomplete",
    "contradictory",
    "malformed",
    "unsupported",
    "adversarial",
    "distractor",
    "stale-data",
    "long-context",
    "tool-required",
    "tool-unnecessary",
    "correct-tool",
    "wrong-tool-trap",
    "malformed-tool-result",
    "empty-tool-result",
    "tool-timeout",
    "rate-limit-429",
    "provider-5xx",
    "dependency-failure",
    "authentication-failure",
    "multi-turn",
    "context-contamination",
    "repeated-request",
    "repeatability",
    "cross-agent",
    "recovery",
)
TOOL_MATRIX_CATEGORIES = frozenset({"happy", "ambiguous", "tool-failure"})
TOOL_SPECIFIC_CATEGORIES = frozenset(
    {
        "tool-required",
        "correct-tool",
        "malformed-tool-result",
        "empty-tool-result",
        "tool-timeout",
        "rate-limit-429",
        "provider-5xx",
    }
)
INVARIANTS = frozenset(
    {
        "routing",
        "schema",
        "prohibited_tools",
        "arguments",
        "order",
        "status",
        "retry",
        "step",
        "idempotency",
    }
)
REQUIRED_OUTPUT_FIELDS = frozenset(COMMON_RESULT_FIELDS)
ALLOWED_STATUSES = frozenset(CANONICAL_STATUSES)
V2_EVAL_DIMENSIONS = frozenset(
    {
        "analysis_complete_semantics", "structured_output_validity", "evidence_quality",
        "source_discipline", "abstention_quality", "clarification_precision",
        "candidate_resolution_quality", "materiality_correctness", "urgency_correctness",
        "human_resolver_correctness", "no_unnecessary_human_question",
        "no_direct_employee_interruption", "privacy_capture_scope",
        "no_private_durable_clarification_memory", "no_self_verification",
        "finance_no_write", "tool_selection_accuracy", "routing_accuracy",
        "provider_failure_honesty", "execution_receipt_correctness", "production_schema_compliance",
    }
)


class DatasetContractError(ValueError):
    """Raised when an evaluation row violates the locked dataset contract."""


def _strings(value: Any, field: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise DatasetContractError(f"{field} must be a list of non-empty strings")
    if not allow_empty and not value:
        raise DatasetContractError(f"{field} must not be empty")
    return value


def validate_case(case: dict[str, Any]) -> None:
    """Validate one local evaluation row without importing runtime code or contacting services."""
    if not isinstance(case, dict):
        raise DatasetContractError("case must be an object")
    if case.get("schema_version") != 1:
        raise DatasetContractError("schema_version must be 1")
    for field in ("example_id", "input", "expected_output", "metadata"):
        if field not in case:
            raise DatasetContractError(f"missing field: {field}")
    metadata = case["metadata"]
    expected = case["expected_output"]
    request = case["input"]
    if not all(isinstance(value, dict) for value in (metadata, expected, request)):
        raise DatasetContractError("input, expected_output, and metadata must be objects")
    slug = metadata.get("agent_slug")
    category = metadata.get("category")
    if slug not in CANONICAL_AGENT_IDS:
        raise DatasetContractError("metadata.agent_slug is invalid")
    case_kind = metadata.get("case_kind", "canonical_category")
    if case_kind == "canonical_category":
        if category not in CATEGORIES:
            raise DatasetContractError("metadata.category is invalid")
        if case["example_id"] != f"{slug}::{category}":
            raise DatasetContractError("example_id must bind agent and category")
    elif case_kind == "tool_matrix":
        if category not in TOOL_MATRIX_CATEGORIES:
            raise DatasetContractError("tool matrix category is invalid")
        if not case["example_id"].startswith(f"{slug}::tool::"):
            raise DatasetContractError("tool example_id must bind canonical agent")
        if metadata.get("tool_outcome") not in {"positive", "negative", "failure"}:
            raise DatasetContractError("tool matrix outcome is invalid")
        if not isinstance(metadata.get("tool_name"), str) or not metadata["tool_name"]:
            raise DatasetContractError("tool matrix name is invalid")
    else:
        raise DatasetContractError("metadata.case_kind is invalid")
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        raise DatasetContractError("input.messages must be non-empty")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "tool"}:
            raise DatasetContractError("message has invalid role")
        if not isinstance(message.get("content"), str) or not message["content"]:
            raise DatasetContractError("message content must be non-empty")
    if expected.get("expected_agent") != slug:
        raise DatasetContractError("expected_agent must match metadata.agent_slug")
    required = set(_strings(expected.get("required_fields"), "required_fields", allow_empty=False))
    if required != REQUIRED_OUTPUT_FIELDS:
        raise DatasetContractError("required_fields must equal the canonical output schema")
    if expected.get("output_contract_version") != CANONICAL_RESULT_CONTRACT_VERSION:
        raise DatasetContractError("output_contract_version must be genesis-v5")
    if expected.get("finance_proposal_required") is not (slug in FINANCE_AGENT_IDS):
        raise DatasetContractError("finance proposal requirement does not match profile domain")
    clarification = expected.get("clarification_expectation")
    if clarification not in {"ordinary", "required_valid", "unsupported_rejected"}:
        raise DatasetContractError("clarification expectation is invalid")
    statuses = set(_strings(expected.get("allowed_statuses"), "allowed_statuses", allow_empty=False))
    if not statuses <= ALLOWED_STATUSES:
        raise DatasetContractError("allowed_statuses contains an unknown status")
    dimensions = set(_strings(metadata.get("v2_eval_dimensions"), "v2_eval_dimensions", allow_empty=False))
    if dimensions != V2_EVAL_DIMENSIONS:
        raise DatasetContractError("V2 evaluation dimensions must be complete")
    _strings(expected.get("expected_tools"), "expected_tools")
    _strings(expected.get("prohibited_tools"), "prohibited_tools")
    if not isinstance(expected.get("expected_arguments"), dict):
        raise DatasetContractError("expected_arguments must be an object")
    order = _strings(expected.get("expected_event_order"), "expected_event_order", allow_empty=False)
    if order[0] != "agent_start" or order[-1] != "agent_finish":
        raise DatasetContractError("expected_event_order must delimit the agent lifecycle")
    for field in ("max_retries", "max_steps", "repeat_count"):
        value = expected.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise DatasetContractError(f"{field} must be a non-negative integer")
    if expected["max_steps"] < len(order):
        raise DatasetContractError("max_steps cannot be below required event count")
    if not isinstance(expected.get("idempotency_key"), str) or not expected["idempotency_key"]:
        raise DatasetContractError("idempotency_key must be non-empty")
    business = expected.get("business_rules")
    if not isinstance(business, dict):
        raise DatasetContractError("business_rules must be an object")
    for flag in (
        "cato_dispatch_only",
        "evidence_required_for_completion",
        "finance_proposal_only",
        "fail_closed_on_conflict",
    ):
        if not isinstance(business.get(flag), bool):
            raise DatasetContractError(f"business_rules.{flag} must be boolean")
    invariants = set(_strings(metadata.get("deterministic_invariants"), "deterministic_invariants"))
    if invariants != INVARIANTS:
        raise DatasetContractError("every case must exercise all deterministic invariant evaluators")
    if metadata.get("tool_expectation") not in {
        "required_positive",
        "required_failure",
        "not_required_for_scenario",
        "no_effective_tools",
    }:
        raise DatasetContractError("metadata.tool_expectation is invalid")


def load_cases(path: Path = DATASET_PATH) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetContractError(f"invalid JSON on line {line_number}") from exc
        validate_case(case)
        cases.append(case)
    return cases


def load_tool_cases(path: Path = TOOL_DATASET_PATH) -> list[dict[str, Any]]:
    return load_cases(path)


def validate_dataset(cases: Iterable[dict[str, Any]], expected_slugs: Iterable[str]) -> dict[str, int]:
    rows = list(cases)
    for case in rows:
        validate_case(case)
    expected = set(expected_slugs)
    counts = Counter((row["metadata"]["agent_slug"], row["metadata"]["category"]) for row in rows)
    if {slug for slug, _ in counts} != expected:
        raise DatasetContractError("dataset agent roster mismatch")
    missing = []
    for slug in expected:
        has_effective_tools = any(
            row["metadata"]["agent_slug"] == slug and row["metadata"].get("allowed_tools")
            for row in rows
        )
        applicable = set(CATEGORIES) - (set() if has_effective_tools else set(TOOL_SPECIFIC_CATEGORIES))
        missing.extend((slug, category) for category in applicable if counts[(slug, category)] != 1)
        missing.extend((slug, category) for category in TOOL_SPECIFIC_CATEGORIES if not has_effective_tools and counts[(slug, category)] != 0)
    if missing:
        raise DatasetContractError(f"dataset coverage mismatch: {missing[:3]}")
    return {"agents": len(expected), "categories": len(CATEGORIES), "cases": len(rows)}
