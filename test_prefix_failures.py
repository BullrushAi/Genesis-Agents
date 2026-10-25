#!/usr/bin/env python3
"""Test various failure scenarios for finance agents."""

from canonical_result_contract import (
    validate_canonical_result_semantics,
    validate_canonical_result_structure,
    validate_canonical_result,
)

def create_base_result(slug):
    """Create a base valid result."""
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

print("Testing various failure scenarios with ALL validators:\n")

# Test 1: Missing ANALYSIS: prefix on transaction_or_schedule_type
slug = "genesis-fin-close"
result = create_base_result(slug)
result["finance_proposal"]["transaction_or_schedule_type"] = "Commission calculation schedule"  # Missing ANALYSIS:
structure = validate_canonical_result_structure(slug, result)
semantics = validate_canonical_result_semantics(slug, result)
combined = validate_canonical_result(slug, result)
print(f"1. Missing ANALYSIS: prefix on transaction_or_schedule_type:")
print(f"   Structure: ok={structure.ok}, reason={structure.reason}")
print(f"   Semantics: ok={semantics.ok}, reason={semantics.reason}")
print(f"   Combined: ok={combined.ok}, reason={combined.reason}, failing_fields={combined.failing_fields}\n")

# Test 2: Missing PROPOSAL: prefix on proposed_treatment
result = create_base_result(slug)
result["finance_proposal"]["proposed_treatment"] = "Review the calculated commission accrual"  # Missing PROPOSAL:
structure = validate_canonical_result_structure(slug, result)
semantics = validate_canonical_result_semantics(slug, result)
combined = validate_canonical_result(slug, result)
print(f"2. Missing PROPOSAL: prefix on proposed_treatment:")
print(f"   Structure: ok={structure.ok}, reason={structure.reason}")
print(f"   Semantics: ok={semantics.ok}, reason={semantics.reason}")
print(f"   Combined: ok={combined.ok}, reason={combined.reason}, failing_fields={combined.failing_fields}\n")

# Test 3: Missing ANALYSIS: prefix on summary (not finance-specific)
result = create_base_result(slug)
result["summary"] = "Endpoint receipt confirmed"  # Missing ANALYSIS:
structure = validate_canonical_result_structure(slug, result)
semantics = validate_canonical_result_semantics(slug, result)
combined = validate_canonical_result(slug, result)
print(f"3. Missing ANALYSIS: prefix on summary:")
print(f"   Structure: ok={structure.ok}, reason={structure.reason}")
print(f"   Semantics: ok={semantics.ok}, reason={semantics.reason}")
print(f"   Combined: ok={combined.ok}, reason={combined.reason}, failing_fields={combined.failing_fields}\n")

print("="*70)
