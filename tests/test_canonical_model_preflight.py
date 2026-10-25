from __future__ import annotations

import pytest

from eval.canonical.model_catalog import (
    ANTHROPIC_LOGICAL_ROUTES,
    CANDIDATE_LOGICAL_MODEL,
    CANDIDATE_NATIVE_MODEL,
    ExperimentPreflightError,
    JUDGE_LOGICAL_MODEL,
    JUDGE_NATIVE_MODEL,
    anthropic_route_fingerprint,
    parse_model_catalogue,
    preflight_anthropic_models,
    preflight_models,
)


CANDIDATE = CANDIDATE_LOGICAL_MODEL
JUDGE = "openai/gpt-test-judge"


def catalogue(*, candidate_tools=True, judge_tools=True):
    return parse_model_catalogue(
        {
            "data": [
                {"id": CANDIDATE, "supports_tools": candidate_tools, "secret_provider_field": "discarded"},
                {"id": JUDGE, "capabilities": {"supports_tools": judge_tools}},
            ]
        },
        fetched_at="2026-08-26T00:00:00Z",
    )


def test_exact_distinct_tool_capable_models_pass_with_sanitized_fingerprint() -> None:
    result = preflight_models(catalogue(), CANDIDATE, JUDGE)
    assert result["catalogue_sha256"]
    assert result["model_count"] == 2
    assert all(row["present"] and row["supports_tools"] for row in result["requested"])
    assert "secret_provider_field" not in str(result)


@pytest.mark.parametrize(
    ("candidate", "judge", "code"),
    [
        ("anthropic/claude-haiku-4-5", JUDGE, "candidate_model_absent"),
        (CANDIDATE.upper(), JUDGE, "candidate_model_absent"),
        ("haiku", JUDGE, "candidate_model_absent"),
        (CANDIDATE, "anthropic/claude-sonnet-4-5", "judge_model_absent"),
        ("auto", JUDGE, "exact_models_required"),
        (CANDIDATE, CANDIDATE, "must_differ"),
    ],
)
def test_aliases_absent_case_changes_auto_and_same_models_fail_closed(candidate, judge, code) -> None:
    with pytest.raises(ExperimentPreflightError, match=code):
        preflight_models(catalogue(), candidate, judge)


@pytest.mark.parametrize(
    ("candidate_tools", "judge_tools", "code"),
    [
        (False, True, "candidate_model_tools_unsupported"),
        (True, False, "judge_model_tools_unsupported"),
    ],
)
def test_models_without_explicit_tool_support_fail_closed(candidate_tools, judge_tools, code) -> None:
    with pytest.raises(ExperimentPreflightError, match=code):
        preflight_models(catalogue(candidate_tools=candidate_tools, judge_tools=judge_tools), CANDIDATE, JUDGE)


def test_catalogue_fingerprint_is_stable_across_provider_order_and_ignores_extra_fields() -> None:
    first = catalogue()
    second = parse_model_catalogue(
        {"data": [{"id": JUDGE, "supports_tools": True}, {"id": CANDIDATE, "supports_tools": True, "pricing": "discard"}]},
        fetched_at="later",
    )
    assert first.sha256 == second.sha256


def test_duplicate_and_malformed_catalogue_rows_are_rejected() -> None:
    for payload in (
        {"data": [{"id": CANDIDATE}, {"id": CANDIDATE}]},
        {"data": [{"id": ""}]},
        {"models": []},
    ):
        with pytest.raises(ExperimentPreflightError):
            parse_model_catalogue(payload)


def anthropic_catalogue(*, candidate_tools=True, judge_tools=True):
    assert CANDIDATE_NATIVE_MODEL == JUDGE_NATIVE_MODEL
    return parse_model_catalogue(
        {
            "data": [
                {
                    "id": ANTHROPIC_LOGICAL_ROUTES[CANDIDATE],
                    "supports_tools": candidate_tools and judge_tools,
                },
            ]
        },
        fetched_at="2026-08-26T00:00:00Z",
    )


def test_anthropic_logical_routes_freeze_exact_dual_haiku_with_distinct_role_bindings() -> None:
    result = preflight_anthropic_models(
        anthropic_catalogue(), CANDIDATE, JUDGE_LOGICAL_MODEL
    )
    assert result["route_fingerprint"] == anthropic_route_fingerprint()
    assert [row["native_model_id"] for row in result["requested"]] == [
        CANDIDATE_NATIVE_MODEL,
        JUDGE_NATIVE_MODEL,
    ]
    assert all(row["supports_tools"] for row in result["requested"])
    assert result["requested"][0]["native_model_id"] == result["requested"][1]["native_model_id"]
    assert result["requested"][0]["role_binding_sha256"] != result["requested"][1]["role_binding_sha256"]
    assert result["requested"][0]["tool_name"] != result["requested"][1]["tool_name"]
    assert result["requested"][0]["schema_sha256"] != result["requested"][1]["schema_sha256"]
    assert result["requested"][0]["policy_version"] != result["requested"][1]["policy_version"]
    from canonical_profiles import CANONICAL_SLUGS
    from runtime.semantic_safety_judge import role_contract_metadata

    assert set(result["canonical_role_bindings"]) == set(CANONICAL_SLUGS)
    for slug, rows in result["canonical_role_bindings"].items():
        runtime = role_contract_metadata(slug)
        assert rows["candidate"]["tool_name"] == runtime["candidate_tool"]
        assert rows["candidate"]["schema_sha256"] == runtime["candidate_schema_sha256"]
        assert rows["candidate"]["policy_sha256"] == runtime["candidate_policy_sha256"]
        assert rows["candidate"]["role_binding_sha256"] == runtime["candidate_binding_sha256"]
        assert rows["candidate"]["finance_instruction_sha256"] == runtime.get("candidate_finance_instruction_sha256")
        assert rows["candidate"]["master_instruction_sha256"] == runtime.get("candidate_master_instruction_sha256")
        assert rows["judge"]["tool_name"] == runtime["judge_tool"]
        assert rows["judge"]["schema_sha256"] == runtime["judge_schema_sha256"]
        assert rows["judge"]["policy_sha256"] == runtime["judge_policy_sha256"]
        assert rows["judge"]["role_binding_sha256"] == runtime["judge_binding_sha256"]


def test_anthropic_dual_haiku_fails_if_role_tool_schema_or_policy_binding_is_shared(monkeypatch) -> None:
    import runtime.semantic_safety_judge as safety

    monkeypatch.setattr(
        safety,
        "_EXPECTED_JUDGE_BINDING_SHA256",
        next(iter(safety._EXPECTED_CANDIDATE_BINDING_SHA256.values())),
    )
    with pytest.raises(Exception, match="model_role_binding_invalid"):
        safety.require_exact_model_role_binding()


@pytest.mark.parametrize(
    ("candidate", "judge", "code"),
    [
        ("anthropic/unknown", JUDGE_LOGICAL_MODEL, "candidate_model_role_binding_mismatch"),
        (CANDIDATE, "anthropic/unknown", "judge_model_role_binding_mismatch"),
        ("anthropic/claude-sonnet-4.5", CANDIDATE, "candidate_model_role_binding_mismatch"),
        (CANDIDATE_NATIVE_MODEL, JUDGE_LOGICAL_MODEL, "candidate_model_role_binding_mismatch"),
        (CANDIDATE, JUDGE_NATIVE_MODEL, "judge_model_role_binding_mismatch"),
    ],
)
def test_anthropic_missing_routes_and_same_model_fail_closed(candidate, judge, code) -> None:
    with pytest.raises(ExperimentPreflightError, match=code):
        preflight_anthropic_models(anthropic_catalogue(), candidate, judge)


@pytest.mark.parametrize(
    ("candidate_tools", "judge_tools", "code"),
    [
        (False, True, "candidate_model_tools_unsupported"),
        (True, False, "candidate_model_tools_unsupported"),
    ],
)
def test_anthropic_capability_policy_failure_aborts_preflight(candidate_tools, judge_tools, code) -> None:
    with pytest.raises(ExperimentPreflightError, match=code):
        preflight_anthropic_models(
            anthropic_catalogue(candidate_tools=candidate_tools, judge_tools=judge_tools),
            CANDIDATE,
            JUDGE_LOGICAL_MODEL,
        )
