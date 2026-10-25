from __future__ import annotations

from types import SimpleNamespace

import pytest

from canonical_profiles import FINANCEOS_SLUGS, MASTER_OS_SLUGS
from runtime.financeos_governed_input import (
    OPTIONAL_CONTEXT_FIELDS,
    bind_financeos_governed_context,
    extract_raw_context,
)


def test_all_12_finance_slugs_consume_present_context_and_omit_absent_fields() -> None:
    raw = {
        "entity": "NES Health LLC",
        "period": "2026-07",
        "data_product_id": "dp-revenue",
        "data_product_version": "3",
        "certification_status": "CERTIFIED",
        "freshness": "2026-08-27T00:00:00Z",
        "lineage": ["portal:order:1"],
        "portal_certified_revenue_present": True,
    }
    for slug in sorted(FINANCEOS_SLUGS):
        bound = bind_financeos_governed_context(slug, raw)
        assert bound.ok is True
        assert "data_product_id" in bound.present_fields
        assert "mapping_rule_version" in bound.absent_fields
        assert "  mapping_rule_version:" not in bound.prompt_block
        assert "do_not_fabricate_absent_fields: true" in bound.prompt_block
        assert bound.context["entity"] == "NES Health LLC"
        assert "semantic_definition_id" not in bound.context


def test_master_os_slugs_do_not_receive_financeos_prompt_block() -> None:
    raw = {"entity": "should-not-bind", "certification_status": "CERTIFIED"}
    for slug in sorted(MASTER_OS_SLUGS):
        bound = bind_financeos_governed_context(slug, raw)
        assert bound.ok is True
        assert bound.prompt_block == ""
        assert bound.context == {}


def test_missing_context_does_not_fabricate_fields() -> None:
    bound = bind_financeos_governed_context("genesis-fin-revenue", None)
    assert bound.ok is True
    assert bound.certification_status == "ABSENT"
    assert bound.present_fields == ()
    assert bound.context == {}
    assert bound.prompt_block == ""
    assert bound.absent_fields == OPTIONAL_CONTEXT_FIELDS


@pytest.mark.parametrize("status", ["HOLD", "STALE", "UNMAPPED", "CONFLICTED"])
def test_blocking_certification_is_not_treated_as_certified_actuals(status: str) -> None:
    bound = bind_financeos_governed_context(
        "genesis-fin-controller",
        {"certification_status": status},
    )
    assert bound.ok is True
    assert "do_not_treat_as_certified_actuals: true" in bound.prompt_block
    assert f"blocking_certification:{status}" in bound.source_authority_flags


def test_unknown_field_is_rejected_not_normalized() -> None:
    bound = bind_financeos_governed_context(
        "genesis-fin-revenue",
        {"entity": "x", "made_up_metric": "please invent"},
    )
    assert bound.ok is False
    assert bound.reason == "financeos_governed_context_unknown_field"


def test_invalid_certification_status_is_rejected() -> None:
    bound = bind_financeos_governed_context(
        "genesis-fin-revenue",
        {"certification_status": "PROBABLY_FINE"},
    )
    assert bound.ok is False
    assert bound.reason == "financeos_certification_status_invalid"


def test_extract_raw_context_does_not_merge_sources() -> None:
    body = SimpleNamespace(
        financeos_context={"entity": "direct"},
        input={"financeos_context": {"entity": "input"}},
        task={"financeos_context": {"entity": "task"}},
    )
    assert extract_raw_context(body) == {"entity": "direct"}
    body.financeos_context = None
    assert extract_raw_context(body) == {"entity": "input"}
