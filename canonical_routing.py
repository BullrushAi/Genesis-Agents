"""Deterministic dispatch-plan validation for the canonical Genesis surface.

Cato remains the semantic routing owner.  It submits structured work types; this
    module maps those types to exactly one canonical specialist and produces a stable
    dependency-ordered plan. Within each dependency wave, dispatch order is lexical by
    ``work_id`` so equivalent inputs cannot change behavior when their array order changes.
    It deliberately does not infer intent from prose and it does not execute agents.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from canonical_profiles import CANONICAL_SLUGS


CAPABILITY_TO_SPECIALIST: Mapping[str, str] = MappingProxyType({
    "automation_architecture": "genesis-automation-architect",
    "canon_stewardship": "genesis-knowledge-evidence",
    "commercial_growth": "genesis-commercial-growth",
    "data_intelligence": "genesis-data-intelligence",
    "execution_control": "genesis-execution-control",
    "finance_accounts_payable": "genesis-fin-ap",
    "finance_accounts_receivable": "genesis-fin-ar",
    "finance_close": "genesis-fin-close",
    "finance_cogs": "genesis-fin-controller",
    "finance_commissions": "genesis-fin-payroll-tax",
    "finance_controllership": "genesis-fin-controller",
    "finance_planning_analysis": "genesis-fin-fpa",
    "finance_intercompany": "genesis-fin-close",
    "finance_payroll_tax": "genesis-fin-payroll-tax",
    "finance_reconciliation": "genesis-fin-reconciliation",
    "finance_revenue": "genesis-fin-revenue",
    "finance_treasury": "genesis-fin-treasury",
    "integration_architecture": "genesis-integration-architect",
    "knowledge_evidence": "genesis-knowledge-evidence",
    "legal_risk": "genesis-legal-risk",
    "people_operations": "genesis-people-operations",
    "platform_engineering": "genesis-platform-engineer",
    "reliability_incident": "genesis-platform-engineer",
    "security_identity": "genesis-security-identity",
})


class RoutingPolicyError(ValueError):
    """Fail-closed routing rejection with a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WorkItem:
    work_id: str
    capability: str
    depends_on: tuple[str, ...] = ()
    claimed_specialist: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WorkItem":
        if not isinstance(value, Mapping):
            raise RoutingPolicyError("invalid_work_item")
        if not set(value) <= {"work_id", "capability", "depends_on", "claimed_specialist"}:
            raise RoutingPolicyError("unexpected_work_item_field")
        work_id = value.get("work_id")
        capability = value.get("capability")
        dependencies = value.get("depends_on", [])
        claimed = value.get("claimed_specialist")
        if not isinstance(work_id, str) or not work_id.strip():
            raise RoutingPolicyError("invalid_work_id")
        if not isinstance(capability, str) or not capability.strip():
            raise RoutingPolicyError("invalid_capability")
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) and item for item in dependencies
        ):
            raise RoutingPolicyError("invalid_dependencies")
        if claimed is not None and (not isinstance(claimed, str) or not claimed):
            raise RoutingPolicyError("invalid_claimed_specialist")
        return cls(work_id.strip(), capability.strip(), tuple(dependencies), claimed)


@dataclass(frozen=True)
class Dispatch:
    specialist: str
    work_id: str
    capability: str


@dataclass(frozen=True)
class RoutePlan:
    waves: tuple[tuple[Dispatch, ...], ...]

    @property
    def dispatches(self) -> tuple[Dispatch, ...]:
        return tuple(dispatch for wave in self.waves for dispatch in wave)

    @property
    def specialists(self) -> tuple[str, ...]:
        return tuple(dispatch.specialist for dispatch in self.dispatches)

    def as_dict(self) -> dict[str, Any]:
        return {
            "waves": [
                [
                    {
                        "specialist": dispatch.specialist,
                        "work_id": dispatch.work_id,
                        "capability": dispatch.capability,
                    }
                    for dispatch in wave
                ]
                for wave in self.waves
            ],
            "dispatch_count": len(self.dispatches),
            "specialists": list(self.specialists),
        }


def _validate_authority_table() -> None:
    specialists = tuple(CAPABILITY_TO_SPECIALIST.values())
    if len(CAPABILITY_TO_SPECIALIST) != 24 or len(set(specialists)) != 19:
        raise RoutingPolicyError("routing_authority_coverage_invalid")
    if set(specialists) != set(CANONICAL_SLUGS):
        raise RoutingPolicyError("routing_authority_roster_mismatch")


def build_route_plan(raw_items: Iterable[Mapping[str, Any]]) -> RoutePlan:
    """Validate Cato's structured choices and return stable parallel waves.

    Empty input is an explicit no-agent plan. Ready work is ordered lexically by
    ``work_id`` inside each wave. Any ambiguity, unknown work type,
    wrong claimed specialist, repeated specialist, dangling dependency, or cycle
    fails closed before dispatch.
    """
    _validate_authority_table()
    try:
        items = tuple(WorkItem.from_mapping(item) for item in raw_items)
    except TypeError as exc:
        raise RoutingPolicyError("invalid_work_items") from exc
    if not items:
        return RoutePlan(())

    by_id = {item.work_id: item for item in items}
    if len(by_id) != len(items):
        raise RoutingPolicyError("duplicate_work_id")

    resolved: dict[str, str] = {}
    for item in items:
        specialist = CAPABILITY_TO_SPECIALIST.get(item.capability)
        if specialist is None:
            raise RoutingPolicyError("no_specialist_for_capability")
        if item.claimed_specialist is not None and item.claimed_specialist != specialist:
            raise RoutingPolicyError("wrong_specialist")
        if item.work_id in item.depends_on:
            raise RoutingPolicyError("circular_route")
        if len(set(item.depends_on)) != len(item.depends_on):
            raise RoutingPolicyError("duplicate_dependency")
        if any(dependency not in by_id for dependency in item.depends_on):
            raise RoutingPolicyError("missed_required_specialist")
        resolved[item.work_id] = specialist

    specialists = tuple(resolved.values())
    if len(set(specialists)) != len(specialists):
        # One bounded dispatch may contain multiple instructions. Repeating the
        # same specialist creates redundant work and enables A->B->A ping-pong.
        raise RoutingPolicyError("duplicate_specialist_dispatch")

    pending = set(by_id)
    completed: set[str] = set()
    waves: list[tuple[Dispatch, ...]] = []
    while pending:
        ready = sorted(
            work_id
            for work_id in pending
            if set(by_id[work_id].depends_on) <= completed
        )
        if not ready:
            raise RoutingPolicyError("circular_route")
        wave = tuple(
            Dispatch(resolved[work_id], work_id, by_id[work_id].capability)
            for work_id in ready
        )
        waves.append(wave)
        pending.difference_update(ready)
        completed.update(ready)

    return RoutePlan(tuple(waves))


def verify_required_specialists(plan: RoutePlan, required: Iterable[str]) -> None:
    """Reject missed, unnecessary, duplicated, or non-canonical dispatches."""
    expected = tuple(required)
    if len(set(expected)) != len(expected):
        raise RoutingPolicyError("duplicate_required_specialist")
    if not set(expected) <= set(CANONICAL_SLUGS):
        raise RoutingPolicyError("noncanonical_required_specialist")
    actual = plan.specialists
    if len(set(actual)) != len(actual):
        raise RoutingPolicyError("duplicate_specialist_dispatch")
    missing = set(expected) - set(actual)
    extra = set(actual) - set(expected)
    if missing:
        raise RoutingPolicyError("missed_required_specialist")
    if extra:
        raise RoutingPolicyError("unnecessary_specialist")
