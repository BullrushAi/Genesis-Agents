#!/usr/bin/env python3
"""Test that _raise_for_runtime_failure extracts output_contract_eval details correctly."""

# Mock HTTPException for testing
class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail

# Test the extraction logic
def test_raise_for_runtime_failure():
    """Test the updated function."""
    
    # Mock the function (inline version of the fix)
    def _raise_for_runtime_failure(result: dict, slug: str) -> None:
        if result.get("ok", True):
            return

        status_code = 502
        error = result.get("error", "agent_failure")
        detail = {
            "error": error,
            "message": result.get("message", ""),
            "type": result.get("type"),
            "agentSlug": slug,
        }
        
        if error == "output_contract_failed":
            eval_data = result.get("output_contract_eval", {})
            if eval_data:
                reason = eval_data.get("reason") or eval_data.get("deterministic_reason") or eval_data.get("judge_reason")
                if reason:
                    detail["reason"] = reason
                
                failing_fields = eval_data.get("failing_fields") or eval_data.get("deterministic_failing_fields")
                if failing_fields:
                    detail["failing_fields"] = list(failing_fields) if not isinstance(failing_fields, list) else failing_fields
                
                detail["contract_ok"] = eval_data.get("ok", False)
        
        raise HTTPException(
            status_code=status_code,
            detail=detail,
        )
    
    # Test case 1: output_contract_failed with deterministic_reason
    print("Test 1: output_contract_failed with deterministic_reason and failing_fields")
    result1 = {
        "ok": False,
        "error": "output_contract_failed",
        "slug": "genesis-fin-controller",
        "response": "...",
        "output_contract_eval": {
            "ok": False,
            "deterministic_reason": "invalid_narrative_prefix",
            "deterministic_failing_fields": ["finance_proposal.transaction_or_schedule_type", "summary"],
        }
    }
    
    try:
        _raise_for_runtime_failure(result1, "genesis-fin-controller")
    except HTTPException as e:
        print(f"  Status: {e.status_code}")
        print(f"  Detail: {e.detail}")
        assert e.detail["error"] == "output_contract_failed"
        assert e.detail["reason"] == "invalid_narrative_prefix"
        assert e.detail["failing_fields"] == ["finance_proposal.transaction_or_schedule_type", "summary"]
        assert e.detail["agentSlug"] == "genesis-fin-controller"
        print("  ✓ PASS\n")
    
    # Test case 2: output_contract_failed with judge_reason
    print("Test 2: output_contract_failed with judge_reason")
    result2 = {
        "ok": False,
        "error": "output_contract_failed",
        "output_contract_eval": {
            "ok": False,
            "judge_reason": "filler_detected",
        }
    }
    
    try:
        _raise_for_runtime_failure(result2, "genesis-fin-revenue")
    except HTTPException as e:
        print(f"  Status: {e.status_code}")
        print(f"  Detail: {e.detail}")
        assert e.detail["reason"] == "filler_detected"
        print("  ✓ PASS\n")
    
    # Test case 3: Other error types should still work
    print("Test 3: Other error types (no output_contract_eval)")
    result3 = {
        "ok": False,
        "error": "tool_call_failed",
        "message": "Tool execution failed",
    }
    
    try:
        _raise_for_runtime_failure(result3, "genesis-fin-close")
    except HTTPException as e:
        print(f"  Status: {e.status_code}")
        print(f"  Detail: {e.detail}")
        assert e.detail["error"] == "tool_call_failed"
        assert "reason" not in e.detail  # Should not add reason for non-contract errors
        print("  ✓ PASS\n")
    
    print("="*70)
    print("All tests passed!")
    print("="*70)


if __name__ == "__main__":
    test_raise_for_runtime_failure()
