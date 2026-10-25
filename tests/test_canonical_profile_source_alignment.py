from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

import canonical_profiles
from bundle_loader import load_bundle
from canonical_profiles import (
    CANONICAL_SOURCE_FILE,
    CANONICAL_SLUGS,
    FINANCEOS_SLUGS,
    MASTER_OS_SLUGS,
    canonical_profile_chain,
    canonical_source_instruction_invariants,
    load_canonical_eval_suite,
    validate_canonical_profile,
)


def test_exact_19_profiles_are_mechanically_source_aligned() -> None:
    assert len(CANONICAL_SLUGS) == 19
    assert len(MASTER_OS_SLUGS) == 10 and len(FINANCEOS_SLUGS) == 9
    for slug in sorted(CANONICAL_SLUGS):
        bundle = load_bundle(slug)
        expected = canonical_source_instruction_invariants(slug)
        assert bundle["source_file"] == CANONICAL_SOURCE_FILE
        assert bundle["instruction_invariants"] == expected
        assert len(expected) == 4
        assert {row["category"] for row in expected} == {
            "role",
            "primary_reasoning",
            "source_tool_discipline",
            "authority_risk_boundary",
        }
        assert sum(row["distinctive_primary"] is True for row in expected) == 1
        assert all(row["instruction"] in bundle["system_prompt"] for row in expected)
        assert canonical_profile_chain(slug)["instructions"]["instruction_invariants"] == expected
        assert load_canonical_eval_suite(slug)["instruction_invariants"] == expected
        assert validate_canonical_profile(slug) == []


def test_distinctive_primary_instructions_are_unique_and_do_not_leak() -> None:
    primary_by_slug = {
        slug: next(
            row["instruction"]
            for row in canonical_source_instruction_invariants(slug)
            if row["distinctive_primary"] is True
        )
        for slug in CANONICAL_SLUGS
    }
    assert len(set(primary_by_slug.values())) == 19
    prompts = {slug: load_bundle(slug)["system_prompt"] for slug in CANONICAL_SLUGS}
    for owner, instruction in primary_by_slug.items():
        assert [slug for slug, prompt in prompts.items() if instruction in prompt] == [owner]


@pytest.mark.parametrize("mutation", ["delete", "change", "wrong_profile"])
def test_hostile_invariant_deletion_mutation_and_cross_profile_swap_fail(
    monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    slug = "genesis-fin-ap"
    bundle = deepcopy(load_bundle(slug))
    if mutation == "delete":
        bundle["instruction_invariants"].pop()
    elif mutation == "change":
        bundle["instruction_invariants"][1]["instruction"] = "Use a generic accounting approach."
    else:
        bundle["instruction_invariants"] = deepcopy(
            load_bundle("genesis-fin-close")["instruction_invariants"]
        )

    real_loader = canonical_profiles.load_bundle
    monkeypatch.setattr(
        canonical_profiles,
        "load_bundle",
        lambda requested: bundle if requested == slug else real_loader(requested),
    )
    errors = validate_canonical_profile(slug)
    assert any("instruction_invariants" in error for error in errors)


def test_commissions_completion_guidance_is_mirrored_without_invariant_drift() -> None:
    root = Path(__file__).resolve().parents[1]
    bundle = load_bundle("genesis-fin-payroll-tax")
    eval_profile = json.loads(
        (root / "eval/canonical_profiles/genesis-fin-payroll-tax.json").read_text(encoding="utf-8")
    )
    assert bundle["completion_guidance"] == eval_profile["completion_guidance"]
    assert bundle["instruction_invariants"] == eval_profile["instruction_invariants"]
    assert all(
        row["instruction"] in bundle["system_prompt"]
        for row in bundle["instruction_invariants"]
    )
    for slug in CANONICAL_SLUGS - {"genesis-fin-payroll-tax"}:
        assert "SAFE COMMISSIONS COMPLETION PHRASING" not in load_bundle(slug)["system_prompt"]
