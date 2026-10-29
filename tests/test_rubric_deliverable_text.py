"""The rubric scores the work inside a delivery envelope, not the envelope (live defect, negotiation fc83b663)."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vendor", "conduit", "tools"))

import rubric  # noqa: E402

SUMMARY = (
    "Adoption of automated bookkeeping likely varies by business size, industry, location and how mature "
    "the existing accounting setup is. The main barriers are data quality, accounting policy requirements, "
    "integration effort and worry about wrong categorizations. The benefits depend on human review: "
    "automation cuts routine work but shifts effort to catching exceptions. "
) * 3


def _genesis_delivery() -> str:
    output = {
        "summary": SUMMARY,
        "findings": ["Adoption varies by size.", "Barriers are data quality.", "Benefits need review."],
        "status": "partial",
        "provenance": {"model_id": "minimax/minimax-m2.5", "generated_at": "2026-10-29T22:31:04Z",
                       "result_sha256": "a" * 64, "signature_b64": "b" * 88},
    }
    envelope = {
        "service": "Genesis genesis-knowledge-evidence",
        "agent": "genesis-knowledge-evidence",
        "job": "bounded",
        "negotiation_id": "fc83b663-7040-4d10-ab3d-30fb25682cb8",
        "output": json.dumps(output),
    }
    return json.dumps(envelope, sort_keys=True)


def test_word_count_scores_the_deliverable_not_the_envelope():
    content = _genesis_delivery()
    words = len(rubric.deliverable_text(content).split())
    assert words < len(content.split())  # the envelope adds words that are not work
    result = rubric.evaluate_rubric(content, {"max_word_count": words, "min_length_chars": 200})
    assert result["rubric_pass"], result["predicate_results"]
    assert result["word_count"] == words


def test_metadata_is_not_counted():
    text = rubric.deliverable_text(_genesis_delivery())
    for leaked in ("fc83b663", "genesis-knowledge-evidence", "minimax", "aaaa", "partial", "bounded"):
        assert leaked not in text
    assert "Adoption of automated bookkeeping" in text
    assert "Benefits need review." in text


def test_over_length_work_still_fails():
    result = rubric.evaluate_rubric(_genesis_delivery(), {"max_word_count": 20})
    assert not result["rubric_pass"]


def test_must_not_contain_still_sees_metadata():
    content = _genesis_delivery()
    result = rubric.evaluate_rubric(content, {"must_not_contain": ["fc83b663"]})
    assert not result["rubric_pass"]


def test_plain_text_and_html_are_unchanged():
    assert rubric.deliverable_text("just words here") == "just words here"
    page = "<!doctype html><html><body>Hello</body></html>"
    assert rubric.deliverable_text(page) == page
    assert rubric.deliverable_text("[not json") == "[not json"


def test_json_hint_still_checks_the_full_content():
    result = rubric.evaluate_rubric(_genesis_delivery(), {"content_type_hint": "json"})
    assert result["rubric_pass"]


def test_camel_case_ids_are_metadata():
    text = rubric.deliverable_text(json.dumps({"negotiationId": "n-1", "requestId": "r-1", "answer": "The answer."}))
    assert text == "The answer."


def test_language_scores_the_deliverable_text():
    if not rubric._LANGDETECT_AVAILABLE:
        import pytest
        pytest.skip("langdetect not installed here")
    result = rubric.evaluate_rubric(_genesis_delivery(), {"language": "en"})
    assert result["rubric_pass"], result["predicate_results"]


def test_json_with_no_strings_is_not_scored_as_empty():
    assert rubric.deliverable_text('{"count": 42}') == "42"
    assert rubric.deliverable_text("[1, 2, 3]") == "1\n2\n3"
    only_metadata = json.dumps({"status": "ok", "negotiation_id": "n-1"})
    assert rubric.deliverable_text(only_metadata) == only_metadata
    assert rubric.evaluate_rubric('{"count": 42}', {"must_contain": ["42"], "min_word_count": 1})["rubric_pass"]


def test_bulk_text_under_a_metadata_key_still_counts_against_a_maximum():
    padded = json.dumps({"answer": "Short answer.", "status": "padding " * 200})
    assert "padding" in rubric.deliverable_text(padded)
    assert not rubric.evaluate_rubric(padded, {"max_word_count": 50})["rubric_pass"]


def test_text_nested_past_the_depth_limit_still_counts():
    deep: object = {"answer": "deep words here"}
    for _ in range(10):
        deep = {"wrap": deep}
    assert "deep words here" in rubric.deliverable_text(json.dumps(deep))
