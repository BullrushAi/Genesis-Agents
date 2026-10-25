from __future__ import annotations

from runtime.financeos_governed_input import bind_financeos_governed_context


def test_portal_certified_revenue_blocks_xero_as_operational_primary() -> None:
    bound = bind_financeos_governed_context(
        "genesis-fin-revenue",
        {
            "portal_certified_revenue_present": True,
            "xero_revenue_asserted_primary": True,
            "revenue_operational_source": "PORTAL",
            "revenue_ledger_source": "XERO",
            "certification_status": "CERTIFIED",
        },
    )
    assert bound.ok is True
    assert "portal_operational_revenue_truth" in bound.source_authority_flags
    assert "xero_not_primary_operational_revenue" in bound.source_authority_flags
    assert "conflicted_xero_revenue_primary_rejected" in bound.source_authority_flags
    assert bound.context.get("revenue_operational_source") == "PORTAL"
    assert bound.context.get("xero_revenue_asserted_primary") is True


def test_commission_measures_stay_distinct_and_xero_gl_is_not_sole_truth() -> None:
    bound = bind_financeos_governed_context(
        "genesis-fin-payroll-tax",
        {
            "commission_measures": {
                "expected_commission": "portal_rule",
                "tracker_commission": "becky_susan",
                "booked_commission": "xero_gl",
                "paid_commission": "payroll",
            }
        },
    )
    assert bound.ok is True
    for name in (
        "expected_commission",
        "tracker_commission",
        "booked_commission",
        "paid_commission",
    ):
        assert f"commission_measure_preserved:{name}" in bound.source_authority_flags
    booked_only = bind_financeos_governed_context(
        "genesis-fin-payroll-tax",
        {"commission_measures": {"booked_commission": "xero_gl"}},
    )
    assert "xero_commission_gl_not_sole_truth" in booked_only.source_authority_flags
    assert "commission_measure_preserved:expected_commission" not in booked_only.source_authority_flags


def test_royalty_without_adopted_instrument_is_unresolved_intercompany_hold() -> None:
    bound = bind_financeos_governed_context(
        "genesis-fin-close",
        {
            "intercompany_classification": "royalty",
            "adopted_instrument_present": False,
            "certification_status": "HOLD",
        },
    )
    assert bound.ok is True
    assert "unresolved_intercompany_charge_hold" in bound.source_authority_flags
    assert "UNRESOLVED_INTERCOMPANY_CHARGE" in bound.prompt_block
    assert "do_not_treat_as_certified_actuals: true" in bound.prompt_block


def test_prompt_names_authorities_without_rewriting_caller_fields() -> None:
    bound = bind_financeos_governed_context(
        "genesis-fin-fpa",
        {
            "revenue_operational_source": "PORTAL",
            "revenue_ledger_source": "XERO",
            "portal_certified_revenue_present": True,
        },
    )
    assert "PORTAL=operational_revenue_truth" in bound.prompt_block
    assert "XERO=accounting_ledger_truth" in bound.prompt_block
    assert "FINANCEOS=governed_certified_representation" in bound.prompt_block
    assert bound.context["revenue_operational_source"] == "PORTAL"
    assert bound.context["revenue_ledger_source"] == "XERO"
