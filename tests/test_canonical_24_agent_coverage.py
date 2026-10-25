from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from canonical_profiles import CANONICAL_SLUGS
from eval.canonical.datasets import CATEGORIES, TOOL_SPECIFIC_CATEGORIES, load_cases, validate_dataset


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = json.loads((ROOT / "proof-artifacts/system-inventory/system_inventory.json").read_text(encoding="utf-8"))


def _transcript(case: dict) -> str:
    return "\n".join(f"{m['role']}:{m['content']}" for m in case["input"]["messages"])


def test_all_24_agents_cover_every_applicable_finish_brief_behavior() -> None:
    cases = load_cases()
    summary = validate_dataset(cases, CANONICAL_SLUGS)
    assert summary == {"agents": 19, "categories": 28, "cases": 504}
    by_slug: dict[str, set[str]] = defaultdict(set)
    toolful = {
        agent["slug"]
        for agent in INVENTORY["agents"]
        if agent["tools"]["effective_allowed_from_advertised"]
    }
    for case in cases:
        by_slug[case["metadata"]["agent_slug"]].add(case["metadata"]["category"])
    for slug in CANONICAL_SLUGS:
        expected = set(CATEGORIES) if slug in toolful else set(CATEGORIES) - set(TOOL_SPECIFIC_CATEGORIES)
        assert by_slug[slug] == expected
    assert Counter(len(categories) for categories in by_slug.values()) == {28: 15, 21: 4}


def test_scenarios_are_identity_bound_unique_and_not_answer_leaking() -> None:
    cases = load_cases()
    ids = [case["example_id"] for case in cases]
    transcripts = [_transcript(case) for case in cases]
    assert len(ids) == len(set(ids))
    assert len(transcripts) == len(set(transcripts))
    leakage_markers = (
        "expected_output", "allowed_statuses", "required_fields", "pass_conditions",
        "gold answer", "expected answer", "score this", "evaluator must",
    )
    for case, transcript in zip(cases, transcripts):
        lowered = transcript.lower()
        assert not any(marker in lowered for marker in leakage_markers), case["example_id"]
        assert case["metadata"]["agent_purpose"] in transcript


def test_role_specific_prompts_bind_real_responsibilities_boundaries_and_tools() -> None:
    cases = load_cases()
    by_key = {(c["metadata"]["agent_slug"], c["metadata"]["category"]): c for c in cases}
    for agent in INVENTORY["agents"]:
        slug = agent["slug"]
        fixture = agent["responsibility_fixture"]
        boundary = agent["guardrails"]["role_specific"][0]
        assert fixture in _transcript(by_key[(slug, "happy")])
        assert boundary in _transcript(by_key[(slug, "boundary")])
        assert fixture in _transcript(by_key[(slug, "cross-agent")])
        tools = agent["tools"]["effective_allowed_from_advertised"]
        if tools:
            required = by_key[(slug, "tool-required")]
            correct = by_key[(slug, "correct-tool")]
            assert required["expected_output"]["expected_tools"]
            assert correct["expected_output"]["expected_tools"]
            assert set(required["expected_output"]["expected_tools"]) <= set(tools)
            assert set(correct["expected_output"]["expected_tools"]) <= set(tools)
        else:
            assert all((slug, category) not in by_key for category in TOOL_SPECIFIC_CATEGORIES)


def test_failure_contracts_are_fail_closed_zero_retry_and_non_successful() -> None:
    failure_categories = {
        "malformed-tool-result", "empty-tool-result", "tool-timeout", "rate-limit-429", "provider-5xx"
    }
    for case in load_cases():
        category = case["metadata"]["category"]
        expected = case["expected_output"]
        if category in failure_categories:
            assert case["metadata"]["tool_outcome"] == "failure"
            assert expected["expected_tools"]
            assert expected["max_retries"] == 0
            assert "COMPLETE" not in expected["allowed_statuses"]
        if category in {"contradictory", "stale-data", "authentication-failure", "dependency-failure"}:
            assert "COMPLETE" not in expected["allowed_statuses"]


def test_long_context_and_multi_turn_cases_exercise_the_claimed_shapes() -> None:
    for case in load_cases():
        category = case["metadata"]["category"]
        transcript = _transcript(case)
        if category == "long-context":
            assert len(transcript) >= 2500
            assert transcript.count("source hash") == 13
        elif category == "multi-turn":
            assert [m["role"] for m in case["input"]["messages"]] == ["user", "assistant", "user"]


def test_dataset_generation_is_byte_for_byte_deterministic(tmp_path, monkeypatch) -> None:
    from eval.canonical_scenarios import generate_responsibility_coverage as generator

    output = tmp_path / "generated.jsonl"
    monkeypatch.setattr(generator, "OUTPUT_PATH", output)
    monkeypatch.setattr(generator, "MANIFEST_PATH", tmp_path / "coverage_manifest.json")
    generator.main()
    assert output.read_bytes() == (ROOT / "eval/canonical/genesis_24.jsonl").read_bytes()
