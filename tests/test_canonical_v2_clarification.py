from __future__ import annotations

from copy import deepcopy
import json

import pytest

from canonical_profiles import (
    CANONICAL_SLUGS,
    _prefix_contract_fixture_result,
    load_canonical_eval_suite,
)
from canonical_result_contract import (
    CLARIFICATION_FIELDS,
    FINANCE_AGENT_IDS,
    canonical_provider_result_json_schema,
    canonical_result_tool_schema,
)
from eval.canonical.contracts import (
    ContractViolation,
    extract_forced_tool_result,
    extract_forced_tool_result_with_metadata,
    genesis_output_contract,
)
from eval.canonical.datasets import V2_EVAL_DIMENSIONS, load_cases
from eval.canonical.evaluators import evaluate_genesis_business_rules
from test_canonical_result_contract_v2 import (
    CLAUSE_BYPASS_REASONS,
    SEMANTIC_HOSTILE_REASONS,
    clause_bypass_hostile,
    semantic_hostile,
)


def _fixture(slug: str, kind: str) -> dict:
    suite = load_canonical_eval_suite(slug)
    result = deepcopy(
        next(row["result"] for row in suite["contract_fixtures"] if row["kind"] == kind)
    )
    return _prefix_contract_fixture_result(result)


def _provider_fixture(slug: str, kind: str) -> dict:
    public = _fixture(slug, kind)
    needed = public.pop("clarification_needed")
    public["clarification"] = (
        {field: public.pop(field) for field in CLARIFICATION_FIELDS[1:]}
        if needed else None
    )
    return public


def _response(slug: str, payload: dict) -> dict:
    contract = genesis_output_contract(slug)
    return {
        "model": "exact/model",
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "content": None,
                "tool_calls": [{
                    "function": {
                        "name": contract.tool_name,
                        "arguments": json.dumps(payload),
                    },
                }],
            },
        }],
    }


def test_every_profile_has_three_identity_bound_v2_canonical_lanes() -> None:
    rows = load_cases()
    for slug in CANONICAL_SLUGS:
        by_category = {
            row["metadata"]["category"]: row
            for row in rows
            if row["metadata"]["agent_slug"] == slug
        }
        assert by_category["happy"]["metadata"]["v2_fixture_kind"] == "normal"
        assert by_category["ambiguous"]["metadata"]["v2_fixture_kind"] == "valid_clarification"
        assert by_category["adversarial"]["metadata"]["v2_fixture_kind"] == "unsupported_clarification"
        assert set(by_category["happy"]["metadata"]["v2_eval_dimensions"]) == V2_EVAL_DIMENSIONS


@pytest.mark.parametrize("slug", sorted(CANONICAL_SLUGS))
def test_slug_bound_contract_accepts_normal_and_material_clarification(slug: str) -> None:
    contract = genesis_output_contract(slug)
    assert contract.schema_version == 5
    assert contract.tool_name == "submit_genesis_result_v5"
    assert contract.json_schema == canonical_provider_result_json_schema(slug)
    assert contract.json_schema == canonical_result_tool_schema(slug)["input_schema"]
    assert contract.validate(_provider_fixture(slug, "normal"))["agent_id"] == slug
    with pytest.raises(ContractViolation, match="submit_arguments_invalid"):
        contract.validate(_provider_fixture(slug, "material_clarification"))


@pytest.mark.parametrize("slug", sorted(CANONICAL_SLUGS))
def test_slug_bound_contract_rejects_unsupported_human_question(slug: str) -> None:
    with pytest.raises(ContractViolation, match="submit_arguments_invalid"):
        genesis_output_contract(slug).validate(_provider_fixture(slug, "unsupported_clarification"))


@pytest.mark.parametrize("kind", ["normal", "material_clarification"])
def test_forced_tool_extraction_projects_provider_clarification_before_evaluation(kind: str) -> None:
    slug = "genesis-automation-architect"
    contract = genesis_output_contract(slug)
    provider = _provider_fixture(slug, kind)
    if kind == "material_clarification":
        with pytest.raises(ContractViolation, match="submit_arguments_invalid"):
            extract_forced_tool_result(_response(slug, provider), contract, requested_model="exact/model")
        return
    result = extract_forced_tool_result(
        _response(slug, provider), contract, requested_model="exact/model",
    )
    expected = _fixture(slug, kind)
    assert all(result[field] == value for field, value in expected.items())
    assert "clarification" not in result


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(clarification={}),
        lambda value: value["clarification"].update(hostile="smuggled"),
    ],
)
def test_canonical_provider_clarification_rejects_flat_and_hostile_shapes(mutation) -> None:
    slug = "genesis-automation-architect"
    contract = genesis_output_contract(slug)
    provider = _provider_fixture(slug, "material_clarification")
    mutation(provider)
    with pytest.raises(ContractViolation, match="submit_arguments_invalid"):
        extract_forced_tool_result(
            _response(slug, provider), contract, requested_model="exact/model",
        )


def test_complete_legacy_flat_provider_shape_is_projected_with_compatibility_subtype() -> None:
    slug = "genesis-automation-architect"
    from canonical_result_contract import project_provider_canonical_result
    legacy = project_provider_canonical_result(slug, _provider_fixture(slug, "normal"))
    result, metadata = extract_forced_tool_result_with_metadata(
        _response(slug, legacy), genesis_output_contract(slug),
        requested_model="exact/model",
    )
    assert result == legacy
    assert metadata == {
        "representation_subtype": "legacy_flat",
        "representation_missing_fields": (),
        "representation_extra_fields": (),
        "representation_missing_field_count": 0,
        "representation_extra_field_count": 0,
    }


@pytest.mark.parametrize(
    ("mutation", "missing", "extra", "extra_count"),
    [
        (lambda value: value["clarification"].pop("candidate_resolution"),
         ("candidate_resolution",), (), 0),
        (lambda value: value["clarification"].update(clarification_needed=True),
         (), ("clarification_needed",), 1),
        (lambda value: value["clarification"].update(private_payload="must-not-escape"),
         (), (), 1),
    ],
)
def test_invalid_canonical_representation_exposes_only_allowlisted_identifiers(
    mutation, missing, extra, extra_count,
) -> None:
    slug = "genesis-automation-architect"
    provider = _provider_fixture(slug, "material_clarification")
    mutation(provider)
    with pytest.raises(ContractViolation, match="submit_arguments_invalid") as caught:
        extract_forced_tool_result_with_metadata(
            _response(slug, provider), genesis_output_contract(slug),
            requested_model="exact/model",
        )
    assert caught.value.representation_subtype == "canonical_invalid"
    assert caught.value.representation_missing_fields == missing
    assert caught.value.representation_extra_fields == extra
    assert caught.value.representation_extra_field_count == extra_count
    assert "must-not-escape" not in repr(caught.value.__dict__)


def test_master_and_finance_contracts_are_structurally_isolated() -> None:
    master = "genesis-automation-architect"
    finance = "genesis-fin-ap"
    assert "finance_proposal" not in genesis_output_contract(master).json_schema["properties"]
    assert "finance_proposal" in genesis_output_contract(finance).json_schema["required"]
    assert finance in FINANCE_AGENT_IDS and master not in FINANCE_AGENT_IDS


def test_surrounding_prose_is_not_a_canonical_eval_success_path() -> None:
    from scripts.run_canonical_evals import EvalRunError, _json_object

    with pytest.raises(EvalRunError, match="model_output_not_json"):
        _json_object('Result follows: {"agent_id":"genesis-fin-ap"}')


@pytest.mark.parametrize("case_id", sorted(SEMANTIC_HOSTILE_REASONS))
def test_exact_kraken_hostiles_fail_canonical_with_shared_content_free_reason(case_id: str) -> None:
    slug, output = semantic_hostile(case_id)
    case = deepcopy(next(row for row in load_cases() if row["metadata"]["agent_slug"] == slug))
    observed = {"output": output, "tool_calls": []}
    result = next(
        row for row in evaluate_genesis_business_rules(case, observed)
        if row.name == "canonical_result_contract"
    )
    assert result.passed is False
    assert result.explanation == SEMANTIC_HOSTILE_REASONS[case_id]
    with pytest.raises(ContractViolation, match="submit_arguments_invalid"):
        genesis_output_contract(slug).validate(output)


@pytest.mark.parametrize("case_id", sorted(CLAUSE_BYPASS_REASONS))
def test_clause_bypasses_fail_canonical_with_shared_content_free_reason(case_id: str) -> None:
    slug, output = clause_bypass_hostile(case_id)
    case = deepcopy(next(row for row in load_cases() if row["metadata"]["agent_slug"] == slug))
    result = next(
        row for row in evaluate_genesis_business_rules(case, {"output": output, "tool_calls": []})
        if row.name == "canonical_result_contract"
    )
    assert result.passed is False
    assert result.explanation == CLAUSE_BYPASS_REASONS[case_id]
