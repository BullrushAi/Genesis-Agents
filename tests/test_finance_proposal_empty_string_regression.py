"""Regression test for finance_proposal nullable field empty string bug.

This test captures the failure mode where entity_id and period fields
in finance_proposal could be empty strings instead of null, causing
output_contract_failed errors.

FIX: Empty strings are normalized to null in validate_canonical_result_structure
before validation, allowing the LLM to generate empty strings without breaking
the contract (empty strings have no business meaning, so normalizing to null
preserves semantic intent).
"""

from canonical_result_contract import (
    canonical_provider_result_json_schema,
    validate_canonical_result_structure,
)


def test_finance_proposal_empty_strings_are_normalized_to_null():
    """Empty strings in nullable finance fields should be normalized to null.
    
    Root cause: entity_id and period had no minLength constraint in the schema,
    so the LLM could generate empty strings. The first fix (adding minLength: 1)
    caused unsupported_keyword errors in production because minLength is not
    supported by the provider schema validator.
    
    Correct fix: Normalize empty strings to null in the validation layer before
    running strict validation. This allows the LLM to generate empty strings
    (which pass provider schema validation) while ensuring they don't fail local
    validation.
    """
    result = {
        "agent_id": "genesis-fin-close",
        "task_id": "test-empty-string",
        "scope": "regression test",
        "status": "ANALYSIS_COMPLETE",
        "summary": "ANALYSIS: Testing empty string handling.",
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
            "entity_id": "",  # Empty string should be normalized to None
            "period": "",     # Empty string should be normalized to None
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
    assert verdict.ok is True, (
        f"Empty strings should be normalized and pass: {verdict.reason}"
    )
    
    # Verify normalization actually happened
    assert result["finance_proposal"]["entity_id"] is None, (
        "entity_id empty string should be normalized to None"
    )
    assert result["finance_proposal"]["period"] is None, (
        "period empty string should be normalized to None"
    )
    
    # Test with only period as empty string
    result["finance_proposal"]["entity_id"] = "E001"
    result["finance_proposal"]["period"] = ""
    
    verdict2 = validate_canonical_result_structure("genesis-fin-close", result)
    assert verdict2.ok is True
    assert result["finance_proposal"]["period"] is None


def test_finance_proposal_entity_id_and_period_accept_null_and_nonempty():
    """Null and non-empty strings should be accepted for entity_id/period."""
    base_result = {
        "agent_id": "genesis-fin-close",
        "task_id": "test-valid",
        "scope": "regression test",
        "status": "ANALYSIS_COMPLETE",
        "summary": "ANALYSIS: Testing valid values.",
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
    
    # Both null should pass
    verdict1 = validate_canonical_result_structure("genesis-fin-close", base_result)
    assert verdict1.ok
    
    # entity_id with valid string, period null
    base_result["finance_proposal"]["entity_id"] = "E001"
    verdict2 = validate_canonical_result_structure("genesis-fin-close", base_result)
    assert verdict2.ok
    
    # Both with valid strings
    base_result["finance_proposal"]["period"] = "2024-01"
    verdict3 = validate_canonical_result_structure("genesis-fin-close", base_result)
    assert verdict3.ok


def test_schema_has_no_unsupported_keywords():
    """The provider schema should NOT have minLength (unsupported keyword).
    
    The first fix added minLength: 1 to prevent empty strings, but this caused
    llm_call_failed with reserved_schema_invalid:unsupported_keyword in production.
    
    This test ensures the schema uses only supported keywords.
    """
    for slug in ["genesis-fin-close", "genesis-fin-controller", 
                 "genesis-fin-payroll-tax", "genesis-fin-revenue"]:
        schema = canonical_provider_result_json_schema(slug)
        fp_schema = schema["properties"]["finance_proposal"]
        
        # Check entity_id has no minLength
        entity_id_schema = fp_schema["properties"]["entity_id"]
        assert entity_id_schema["type"] == ["string", "null"]
        assert "minLength" not in entity_id_schema, (
            f"{slug}: entity_id has unsupported minLength keyword"
        )
        
        # Check period has no minLength
        period_schema = fp_schema["properties"]["period"]
        assert period_schema["type"] == ["string", "null"]
        assert "minLength" not in period_schema, (
            f"{slug}: period has unsupported minLength keyword"
        )


def test_all_four_failing_agents_handle_empty_strings():
    """All four previously-failing agents should handle empty strings correctly."""
    failing_agents = [
        "genesis-fin-close",
        "genesis-fin-controller",
        "genesis-fin-payroll-tax",
        "genesis-fin-revenue",
    ]
    
    for slug in failing_agents:
        result = {
            "agent_id": slug,
            "task_id": "probe",
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
                "entity_id": "",  # LLM might generate this
                "period": "",     # LLM might generate this
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
        
        verdict = validate_canonical_result_structure(slug, result)
        assert verdict.ok is True, (
            f"{slug} should handle empty strings via normalization: {verdict.reason}"
        )
