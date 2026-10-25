from __future__ import annotations

import json
from pathlib import Path

import main
from bundle_loader import load_bundle, resolve_bundle_slug


ROOT = Path(__file__).resolve().parents[1]
MATRIX = json.loads((ROOT / "eval/canonical_scenarios/canonical_route_tool_failure_matrix.json").read_text(encoding="utf-8"))
INVENTORY = json.loads((ROOT / "proof-artifacts/system-inventory/system_inventory.json").read_text(encoding="utf-8"))


def test_route_dataset_covers_exactly_every_canonical_agent_individually() -> None:
    scenarios = MATRIX["agents"]
    inventory_slugs = {a["slug"] for a in INVENTORY["agents"]}
    scenario_slugs = [a["slug"] for a in scenarios]
    assert len(scenario_slugs) == len(set(scenario_slugs)) == 19
    assert set(scenario_slugs) == inventory_slugs
    assert all(a["route_fixture"].strip() for a in scenarios)
    for slug in scenario_slugs:
        assert slug in main.AGENT_PERSONAS
        assert resolve_bundle_slug(slug) == slug
        assert load_bundle(slug)["slug"] == slug


def test_every_declared_reviewer_transition_is_covered_for_every_source_agent() -> None:
    agents = INVENTORY["agents"]
    edges = {
        (source["slug"], trigger["id"], target)
        for source in agents
        for trigger in source["handoffs"]["recommended_reviewer_triggers"]
        for target in trigger["reviewer_slugs"]
    }
    assert len(edges) == 19 * 9
    assert {target for _, _, target in edges} == {
        "genesis-fin-controller", "genesis-legal-risk", "genesis-security-identity",
        "genesis-platform-engineer", "genesis-knowledge-evidence",
    }
    for source in agents:
        assert source["handoffs"]["genesis_may_dispatch_reviewer"] is False
        assert source["handoffs"]["may_dispatch_subagent"] is False
        assert source["handoffs"]["reviewer_dispatch_owner"] == "Cato"


def test_ambiguous_and_no_dispatch_classes_are_explicit_and_bounded() -> None:
    canonical = {a["slug"] for a in INVENTORY["agents"]}
    ambiguous = MATRIX["ambiguous_routes"]
    assert len(ambiguous) + len(MATRIX["resolved_fold_routes"]) == 12
    for case in MATRIX["resolved_fold_routes"]:
        assert case["candidates"] == [case["primary"]]
        assert case["primary"] in canonical
    assert len({case["id"] for case in ambiguous}) == len(ambiguous)
    for case in ambiguous:
        assert case["primary"] in case["candidates"]
        assert 2 <= len(case["candidates"]) <= 3
        assert set(case["candidates"]) <= canonical
    assert len(MATRIX["no_dispatch"]) >= 3
    assert all(c["expected_agent"] is None and c["expected_tool"] is None for c in MATRIX["no_dispatch"])


def test_bundles_cannot_turn_reviewer_recommendations_into_self_dispatch() -> None:
    for item in MATRIX["agents"]:
        bundle = load_bundle(item["slug"])
        policy = bundle["reviewer_policy"]
        assert policy["enforcement_owner"] == "Cato"
        assert policy["genesis_may_dispatch"] is False
        assert policy["consensus_is_not_authority"] is True
