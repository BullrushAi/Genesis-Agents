#!/usr/bin/env python3
"""Simulate analysis-only probe for finance agents to identify failure discriminator.

This script creates minimal valid probe results for all finance agents and validates
them through the same contract pipeline used in production.
"""

import json
from canonical_result_contract import (
    validate_canonical_result_structure,
    validate_canonical_result_semantics,
    validate_canonical_result,
)

FAILING_AGENTS = [
    "genesis-fin-close",
    "genesis-fin-controller",
    "genesis-fin-payroll-tax",
    "genesis-fin-revenue",
]

PASSING_AGENTS = [
    "genesis-fin-ap",
    "genesis-fin-ar",
    "genesis-fin-fpa",
    "genesis-fin-reconciliation",
    "genesis-fin-treasury",
]


def create_probe_result(slug: str) -> dict:
    """Create a minimal analysis-only probe result."""
    return {
        "agent_id": slug,
        "task_id": "probe-123",
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


def test_agent(slug: str) -> dict:
    """Test an agent through the full validation pipeline."""
    result = create_probe_result(slug)
    
    # Structure validation
    structure = validate_canonical_result_structure(slug, result)
    if not structure.ok:
        return {
            "slug": slug,
            "pass": False,
            "stage": "structure",
            "reason": structure.reason,
            "missing_fields": structure.missing_fields,
            "failing_fields": structure.failing_fields,
        }
    
    # Semantic validation (deterministic)
    semantics = validate_canonical_result_semantics(slug, result)
    if not semantics.ok:
        return {
            "slug": slug,
            "pass": False,
            "stage": "semantics",
            "reason": semantics.reason,
            "missing_fields": semantics.missing_fields,
            "failing_fields": semantics.failing_fields,
        }
    
    # Combined validation
    combined = validate_canonical_result(slug, result)
    if not combined.ok:
        return {
            "slug": slug,
            "pass": False,
            "stage": "combined",
            "reason": combined.reason,
            "missing_fields": combined.missing_fields,
            "failing_fields": combined.failing_fields,
        }
    
    return {
        "slug": slug,
        "pass": True,
        "stage": "complete",
        "reason": None,
    }


def main():
    print("="*70)
    print("Finance Agent Probe Simulation - Identifying Failure Discriminator")
    print("="*70)
    print()
    
    print("Testing FAILING agents (genesis-fin-close, controller, payroll-tax, revenue):")
    print("-"*70)
    failing_results = []
    for slug in FAILING_AGENTS:
        result = test_agent(slug)
        status = "✓ PASS" if result["pass"] else f"✗ FAIL at {result['stage']}"
        print(f"{slug:30} {status}")
        if not result["pass"]:
            print(f"  Reason: {result['reason']}")
            if result.get("failing_fields"):
                print(f"  Failing fields: {result['failing_fields']}")
        failing_results.append(result)
    
    print()
    print("Testing PASSING agents (genesis-fin-ap, ar, fpa, reconciliation, treasury):")
    print("-"*70)
    passing_results = []
    for slug in PASSING_AGENTS:
        result = test_agent(slug)
        status = "✓ PASS" if result["pass"] else f"✗ FAIL at {result['stage']}"
        print(f"{slug:30} {status}")
        if not result["pass"]:
            print(f"  Reason: {result['reason']}")
            if result.get("failing_fields"):
                print(f"  Failing fields: {result['failing_fields']}")
        passing_results.append(result)
    
    print()
    print("="*70)
    print("Summary:")
    print("="*70)
    
    failing_pass_count = sum(1 for r in failing_results if r["pass"])
    passing_pass_count = sum(1 for r in passing_results if r["pass"])
    
    print(f"FAILING agents: {failing_pass_count}/{len(FAILING_AGENTS)} passed")
    print(f"PASSING agents: {passing_pass_count}/{len(PASSING_AGENTS)} passed")
    
    if failing_pass_count == len(FAILING_AGENTS):
        print("\n✓ All agents pass local validation!")
        print("  → The live failure must be in LLM response generation, not validation")
        print("  → Need to capture actual LLM responses from live gateway")
    else:
        print("\n✗ Some agents fail local validation")
        print("  → This IS the discriminator")
        
        # Analyze failure reasons
        failure_reasons = {}
        for r in failing_results:
            if not r["pass"]:
                reason = r["reason"]
                if reason not in failure_reasons:
                    failure_reasons[reason] = []
                failure_reasons[reason].append(r["slug"])
        
        if failure_reasons:
            print("\nFailure reasons:")
            for reason, slugs in failure_reasons.items():
                print(f"  {reason}:")
                for slug in slugs:
                    print(f"    - {slug}")


if __name__ == "__main__":
    main()
