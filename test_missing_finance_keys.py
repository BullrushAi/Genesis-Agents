#!/usr/bin/env python3
"""Regression tests for missing finance_proposal keys normalization.

Root cause: LLM sometimes omits required finance_proposal subkeys on some
finance agents, causing missing_finance_fields errors.

Fix: Fill missing keys with safe defaults (null for scalars, [] for lists)
before validation, preserving "not applicable" semantics.
"""

from canonical_result_contract import (
    validate_canonical_result_structure,
    FINANCE_PROPOSAL_FIELDS,
)


def create_base_result(slug: str, finance_proposal: dict) -> dict:
    """Create a base result with the given finance_proposal."""
    return {
        "agent_id": slug,
        "task_id": "test",
        "scope": "test",
        "status": "ANALYSIS_COMPLETE",
        "summary": "ANALYSIS: Test.",
        "findings": [],
        "proposals": [],
        "evidence_refs": [],
        "assumptions": [],
        "conflicts": [],
        "unknowns": [],
        "confidence": 0.9,
        "risk_flags": [],
        "recommended_next_actions": [],
        "requested_human_decisions": [],
        "tool_calls_summary": [],
        "clarification_needed": False,
        "finance_proposal": finance_proposal,
    }


def test_missing_all_keys_gets_filled():
    """Finance proposal with no keys should be filled with safe defaults."""
    print("Test 1: Missing ALL keys (empty dict)")
    
    result = create_base_result("genesis-fin-close", {})
    verdict = validate_canonical_result_structure("genesis-fin-close", result)
    
    print(f"  Validation: ok={verdict.ok}, reason={verdict.reason}")
    assert verdict.ok, f"Should pass after filling keys: {verdict.reason}"
    
    # Check that keys were filled
    fp = result["finance_proposal"]
    print(f"  Keys after normalization: {len(fp)} (expected {len(FINANCE_PROPOSAL_FIELDS)})")
    assert len(fp) == len(FINANCE_PROPOSAL_FIELDS), "All keys should be present"
    
    # Check list fields are empty arrays
    assert fp["source_refs"] == []
    assert fp["policy_refs"] == []
    assert fp["assumptions"] == []
    assert fp["exceptions"] == []
    
    # Check scalar fields are None
    assert fp["entity_id"] is None
    assert fp["period"] is None
    assert fp["confidence"] is None
    assert fp["requires_human_decision"] is None
    
    print("  ✓ PASS\n")


def test_missing_some_keys_gets_filled():
    """Finance proposal with some keys missing should have them filled."""
    print("Test 2: Missing SOME keys")
    
    # Only include entity_id and period
    partial_fp = {
        "entity_id": "E001",
        "period": "2024-01",
    }
    
    result = create_base_result("genesis-fin-controller", partial_fp)
    verdict = validate_canonical_result_structure("genesis-fin-controller", result)
    
    print(f"  Validation: ok={verdict.ok}, reason={verdict.reason}")
    assert verdict.ok, f"Should pass after filling missing keys: {verdict.reason}"
    
    # Check existing keys preserved
    fp = result["finance_proposal"]
    assert fp["entity_id"] == "E001"
    assert fp["period"] == "2024-01"
    
    # Check missing keys were filled
    assert fp["transaction_or_schedule_type"] is None
    assert fp["proposed_treatment"] is None
    assert fp["source_refs"] == []
    
    print("  ✓ PASS\n")


def test_complete_valid_proposal_unchanged():
    """Complete valid finance_proposal should not be modified."""
    print("Test 3: Complete valid proposal (should remain unchanged)")
    
    complete_fp = {
        "entity_id": None,
        "period": None,
        "transaction_or_schedule_type": None,
        "proposed_treatment": None,
        "account_mapping": None,
        "tracking_mapping": None,
        "tax_treatment": None,
        "amount_logic": None,
        "source_refs": [],
        "policy_refs": [],
        "assumptions": [],
        "confidence": None,
        "exceptions": [],
        "requires_human_decision": None,
        "recommended_financeos_action": None,
    }
    
    result = create_base_result("genesis-fin-payroll-tax", complete_fp)
    verdict = validate_canonical_result_structure("genesis-fin-payroll-tax", result)
    
    print(f"  Validation: ok={verdict.ok}, reason={verdict.reason}")
    assert verdict.ok, f"Should pass: {verdict.reason}"
    
    # All values should remain None or []
    fp = result["finance_proposal"]
    assert all(
        fp[field] in (None, [])
        for field in FINANCE_PROPOSAL_FIELDS
    )
    
    print("  ✓ PASS\n")


def test_unexpected_keys_still_rejected():
    """Finance proposal with unexpected extra keys should still fail validation."""
    print("Test 4: Unexpected extra keys (should still fail)")
    
    fp_with_extra = {
        "entity_id": None,
        "period": None,
        "unexpected_key": "should fail",  # Extra key
        # ... rest will be filled
    }
    
    result = create_base_result("genesis-fin-revenue", fp_with_extra)
    verdict = validate_canonical_result_structure("genesis-fin-revenue", result)
    
    print(f"  Validation: ok={verdict.ok}, reason={verdict.reason}")
    assert not verdict.ok, "Should reject unexpected keys"
    assert verdict.reason == "unexpected_finance_fields"
    
    print("  ✓ PASS (correctly rejected)\n")


def test_all_four_failing_agents():
    """All four previously-failing agents should pass with empty finance_proposal."""
    print("Test 5: All four failing agents with empty finance_proposal")
    
    failing_agents = [
        "genesis-fin-close",
        "genesis-fin-controller",
        "genesis-fin-payroll-tax",
        "genesis-fin-revenue",
    ]
    
    for slug in failing_agents:
        result = create_base_result(slug, {})
        verdict = validate_canonical_result_structure(slug, result)
        status = "✓ PASS" if verdict.ok else f"✗ FAIL ({verdict.reason})"
        print(f"  {slug:30} {status}")
        assert verdict.ok, f"{slug} should pass after key filling"
    
    print()


def test_invalid_types_still_rejected():
    """Invalid types in filled proposal should still fail validation."""
    print("Test 6: Invalid types (should still fail)")
    
    # String where list expected
    result = create_base_result("genesis-fin-close", {})
    result["finance_proposal"]["source_refs"] = "not a list"
    
    verdict = validate_canonical_result_structure("genesis-fin-close", result)
    print(f"  String instead of list: ok={verdict.ok}, reason={verdict.reason}")
    assert not verdict.ok, "Should reject wrong type"
    
    # Number where string expected
    result2 = create_base_result("genesis-fin-close", {})
    result2["finance_proposal"]["entity_id"] = 12345  # Number instead of string/null
    
    verdict2 = validate_canonical_result_structure("genesis-fin-close", result2)
    print(f"  Number instead of string: ok={verdict2.ok}, reason={verdict2.reason}")
    assert not verdict2.ok, "Should reject wrong type"
    
    print("  ✓ PASS (correctly rejected)\n")


def main():
    print("="*70)
    print("Missing Finance Proposal Keys - Regression Tests")
    print("="*70)
    print()
    
    try:
        test_missing_all_keys_gets_filled()
        test_missing_some_keys_gets_filled()
        test_complete_valid_proposal_unchanged()
        test_unexpected_keys_still_rejected()
        test_all_four_failing_agents()
        test_invalid_types_still_rejected()
        
        print("="*70)
        print("✓ ALL TESTS PASSED")
        print("="*70)
        print()
        print("The fix correctly:")
        print("- Fills missing keys with safe defaults (null/[])")
        print("- Preserves existing valid values")
        print("- Still rejects unexpected keys")
        print("- Still rejects invalid types")
        print("- Works for all four failing agents")
        print()
        return 0
    
    except AssertionError as e:
        print()
        print("="*70)
        print("✗ TEST FAILED")
        print("="*70)
        print(f"\nError: {e}\n")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit(main())
