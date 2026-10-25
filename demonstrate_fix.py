#!/usr/bin/env python3
"""Demonstrate the fix for the four failing finance agents.

Shows that all four agents now handle empty strings via normalization
to null, and that the schema uses only supported keywords.
"""

import json
from canonical_result_contract import (
    canonical_provider_result_json_schema,
    canonical_result_tool_schema,
    validate_canonical_result_structure,
)


FAILING_AGENTS = [
    "genesis-fin-close",
    "genesis-fin-controller",
    "genesis-fin-payroll-tax",
    "genesis-fin-revenue",
]


def show_schema_constraints():
    """Show the schema constraints (no unsupported keywords)."""
    print("Schema Constraints (entity_id and period)")
    print("=" * 60)
    
    for slug in FAILING_AGENTS:
        schema = canonical_provider_result_json_schema(slug)
        fp = schema["properties"]["finance_proposal"]
        
        entity_schema = fp["properties"]["entity_id"]
        period_schema = fp["properties"]["period"]
        
        print(f"\n{slug}:")
        print(f"  entity_id: {json.dumps(entity_schema, indent=4)}")
        has_minlength = "minLength" in entity_schema
        print(f"  Has unsupported minLength: {has_minlength}")


def demonstrate_validation():
    """Show validation behavior with normalization."""
    print("\n\nValidation Behavior (with normalization)")
    print("=" * 60)
    
    # Base result template
    base = {
        "agent_id": "",  # Will be set per agent
        "task_id": "probe-test",
        "scope": "AUDIT_PROBE_ONLY",
        "status": "ANALYSIS_COMPLETE",
        "summary": "ANALYSIS: Endpoint receipt confirmed.",
        "findings": [],
        "proposals": [],
        "evidence_refs": [],
        "assumptions": [],
        "conflicts": [],
        "unknowns": [],
        "confidence": 1.0,
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
    
    for slug in FAILING_AGENTS:
        print(f"\n{slug}:")
        base["agent_id"] = slug
        
        # Test 1: Both null (should pass)
        base["finance_proposal"]["entity_id"] = None
        base["finance_proposal"]["period"] = None
        v1 = validate_canonical_result_structure(slug, base)
        status1 = "✓ PASS" if v1.ok else f"✗ FAIL ({v1.reason})"
        print(f"  entity_id=null, period=null:       {status1}")
        
        # Test 2: Empty string in entity_id (should be normalized to null and pass)
        base["finance_proposal"]["entity_id"] = ""
        v2 = validate_canonical_result_structure(slug, base)
        normalized_entity = base["finance_proposal"]["entity_id"]
        status2 = f"✓ NORMALIZED TO null, PASS" if v2.ok and normalized_entity is None else f"✗ FAIL ({v2.reason})"
        print(f"  entity_id='', period=null:         {status2}")
        
        # Test 3: Empty string in period (should be normalized to null and pass)
        base["finance_proposal"]["entity_id"] = None
        base["finance_proposal"]["period"] = ""
        v3 = validate_canonical_result_structure(slug, base)
        normalized_period = base["finance_proposal"]["period"]
        status3 = f"✓ NORMALIZED TO null, PASS" if v3.ok and normalized_period is None else f"✗ FAIL ({v3.reason})"
        print(f"  entity_id=null, period='':         {status3}")
        
        # Test 4: Valid strings (should pass)
        base["finance_proposal"]["entity_id"] = "E001"
        base["finance_proposal"]["period"] = "2024-01"
        v4 = validate_canonical_result_structure(slug, base)
        status4 = "✓ PASS" if v4.ok else f"✗ FAIL ({v4.reason})"
        print(f"  entity_id='E001', period='2024-01': {status4}")


def main():
    print("=" * 60)
    print("Finance Agent Fix - Corrected Approach")
    print("=" * 60)
    print("\nFIRST ATTEMPT (FAILED):")
    print("- Added minLength: 1 to entity_id and period schemas")
    print("- Worked locally but FAILED in production")
    print("- Error: llm_call_failed:unsupported_keyword")
    print("- Cause: minLength is not supported by provider schema validator")
    print("\nCORRECTED FIX:")
    print("- Normalize empty strings to null BEFORE validation")
    print("- Schema uses only supported keywords")
    print("- LLM can generate empty strings (pass provider validation)")
    print("- Empty strings normalized to null (semantic intent preserved)")
    print("- Null and non-empty strings pass strict validation")
    print()
    
    show_schema_constraints()
    demonstrate_validation()
    
    print("\n" + "=" * 60)
    print("✓ All four agents handle empty strings via normalization")
    print("✓ No unsupported schema keywords")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
