#!/usr/bin/env python3
"""Standalone verification that the finance agent fix works correctly.

This script can be run without pytest to verify:
1. The schema has minLength constraints
2. Validation rejects empty strings
3. Validation accepts null and non-empty strings
"""

import json
from canonical_result_contract import (
    canonical_provider_result_json_schema,
    validate_canonical_result_structure,
)


def test_schema_has_minlength():
    """Verify minLength constraint is in the schema."""
    print("Testing schema has minLength constraint...")
    
    for slug in ["genesis-fin-close", "genesis-fin-controller",
                 "genesis-fin-payroll-tax", "genesis-fin-revenue"]:
        schema = canonical_provider_result_json_schema(slug)
        fp_schema = schema["properties"]["finance_proposal"]
        
        for field in ["entity_id", "period"]:
            field_schema = fp_schema["properties"][field]
            assert field_schema["type"] == ["string", "null"], \
                f"{slug}.{field} type is not ['string', 'null']"
            assert field_schema.get("minLength") == 1, \
                f"{slug}.{field} missing minLength constraint"
    
    print("  ✓ All agents have minLength: 1 for entity_id and period\n")


def test_validation_rejects_empty_strings():
    """Verify validation rejects empty strings."""
    print("Testing validation rejects empty strings...")
    
    result = {
        "agent_id": "genesis-fin-close",
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
        "finance_proposal": {
            "entity_id": "",  # Empty string - should fail
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
    }
    
    verdict = validate_canonical_result_structure("genesis-fin-close", result)
    assert not verdict.ok, "Empty string in entity_id should fail validation"
    assert verdict.reason == "invalid_finance_field_type", \
        f"Wrong error reason: {verdict.reason}"
    
    # Test with period
    result["finance_proposal"]["entity_id"] = None
    result["finance_proposal"]["period"] = ""
    verdict2 = validate_canonical_result_structure("genesis-fin-close", result)
    assert not verdict2.ok, "Empty string in period should fail validation"
    
    print("  ✓ Empty strings correctly rejected\n")


def test_validation_accepts_valid_values():
    """Verify validation accepts null and non-empty strings."""
    print("Testing validation accepts valid values...")
    
    result = {
        "agent_id": "genesis-fin-close",
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
        "finance_proposal": {
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
    }
    
    # Test with both null
    verdict1 = validate_canonical_result_structure("genesis-fin-close", result)
    assert verdict1.ok, f"Null values should pass: {verdict1.reason}"
    
    # Test with valid entity_id
    result["finance_proposal"]["entity_id"] = "E001"
    verdict2 = validate_canonical_result_structure("genesis-fin-close", result)
    assert verdict2.ok, f"Non-empty entity_id should pass: {verdict2.reason}"
    
    # Test with both valid
    result["finance_proposal"]["period"] = "2024-01"
    verdict3 = validate_canonical_result_structure("genesis-fin-close", result)
    assert verdict3.ok, f"Both non-empty should pass: {verdict3.reason}"
    
    print("  ✓ Null and non-empty strings correctly accepted\n")


def main():
    print("=" * 60)
    print("Finance Agent Fix Verification")
    print("=" * 60)
    print()
    
    try:
        test_schema_has_minlength()
        test_validation_rejects_empty_strings()
        test_validation_accepts_valid_values()
        
        print("=" * 60)
        print("✓ ALL TESTS PASSED")
        print("=" * 60)
        print()
        print("The fix is working correctly:")
        print("- Schema has minLength: 1 constraint")
        print("- Empty strings are rejected")
        print("- Null and non-empty strings are accepted")
        print()
        return 0
    
    except AssertionError as e:
        print()
        print("=" * 60)
        print("✗ TEST FAILED")
        print("=" * 60)
        print(f"\nError: {e}\n")
        return 1


if __name__ == "__main__":
    exit(main())
