from __future__ import annotations

import json
from pathlib import Path

import pytest

from canonical_profiles import CANONICAL_SLUGS
from canonical_routing import (
    CAPABILITY_TO_SPECIALIST,
    RoutingPolicyError,
    build_route_plan,
    verify_required_specialists,
)


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads(
    (ROOT / "eval/canonical_scenarios/deterministic_routing_cases.json").read_text(encoding="utf-8")
)


def _wave_slugs(plan) -> list[list[str]]:
    return [[dispatch.specialist for dispatch in wave] for wave in plan.waves]


def test_all_24_capabilities_route_to_exact_canonical_19() -> None:
    assert len(CAPABILITY_TO_SPECIALIST) == 24
    assert len(set(CAPABILITY_TO_SPECIALIST.values())) == 19
    assert set(CAPABILITY_TO_SPECIALIST.values()) == set(CANONICAL_SLUGS)


@pytest.mark.parametrize("capability,expected", sorted(CAPABILITY_TO_SPECIALIST.items()))
def test_every_obvious_single_agent_route(capability: str, expected: str) -> None:
    plan = build_route_plan([{"work_id": "work", "capability": capability}])
    assert plan.specialists == (expected,)
    verify_required_specialists(plan, [expected])


@pytest.mark.parametrize("case", CASES["valid_cases"], ids=lambda row: row["id"])
def test_valid_adversarial_and_workflow_routes(case: dict) -> None:
    first = build_route_plan(case["work"])
    assert _wave_slugs(first) == case["expected_waves"]
    verify_required_specialists(first, case["required"])
    assert first.as_dict() == build_route_plan(case["work"]).as_dict()
    assert first.as_dict() == build_route_plan(list(reversed(case["work"]))).as_dict()


@pytest.mark.parametrize("case", CASES["rejected_cases"], ids=lambda row: row["id"])
def test_wrong_duplicate_missed_and_circular_routes_fail_closed(case: dict) -> None:
    with pytest.raises(RoutingPolicyError) as rejected:
        build_route_plan(case["work"])
    assert rejected.value.code == case["error"]


def test_repeatability_is_byte_stable_across_input_runs() -> None:
    for case in CASES["valid_cases"]:
        serialized = {
            json.dumps(build_route_plan(case["work"]).as_dict(), sort_keys=True, separators=(",", ":"))
            for _ in range(100)
        }
        assert len(serialized) == 1


def test_required_specialist_verifier_detects_missed_and_unnecessary_routes() -> None:
    plan = build_route_plan([{"work_id": "legal", "capability": "legal_risk"}])
    with pytest.raises(RoutingPolicyError, match="missed_required_specialist"):
        verify_required_specialists(plan, ["genesis-legal-risk", "genesis-security-identity"])
    with pytest.raises(RoutingPolicyError, match="unnecessary_specialist"):
        verify_required_specialists(plan, [])


@pytest.mark.parametrize("case", CASES["verification_cases"], ids=lambda row: row["id"])
def test_route_verification_rejects_missed_or_unnecessary_specialists(case: dict) -> None:
    plan = build_route_plan(case["work"])
    with pytest.raises(RoutingPolicyError) as rejected:
        verify_required_specialists(plan, case["required"])
    assert rejected.value.code == case["error"]


def test_routing_authority_cannot_be_mutated_at_runtime() -> None:
    with pytest.raises(TypeError):
        CAPABILITY_TO_SPECIALIST["legal_risk"] = "genesis-fin-controller"  # type: ignore[index]


def test_routing_planner_never_executes_or_selects_tools() -> None:
    source = (ROOT / "canonical_routing.py").read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "httpx" not in source
    assert "requests" not in source
    assert "tool_calls" not in source
