# Finance Agent Fix - CORRECTED After Production Failure

## Timeline

### Initial Problem (2026-09-14 PM)
Four finance agents failing with `output_contract_failed` on live gateway:
- genesis-fin-close
- genesis-fin-controller
- genesis-fin-payroll-tax
- genesis-fin-revenue

### First Fix Attempt (PR #3) - FAILED
- Added `minLength: 1` to entity_id and period schemas
- Worked perfectly in local tests
- **FAILED in production** with `llm_call_failed:unsupported_keyword`
- Cause: `minLength` is not supported by Anthropic's tool schema validator

### Corrected Fix (PR #4) - PRODUCTION-SAFE
- Normalize empty strings to `null` in validation layer
- Uses ONLY supported schema keywords
- Preserves semantic intent without breaking production

---

## The Root Cause

The `entity_id` and `period` fields in `finance_proposal` were defined as:
```json
{"type": ["string", "null"]}
```

This meant:
1. ✅ LLM could generate `null` (valid)
2. ✅ LLM could generate non-empty strings like `"E001"` (valid)
3. ❌ LLM could also generate empty strings `""` (technically valid per schema)

Empty strings would:
- Pass Anthropic's provider schema validation
- Fail local validation with `invalid_finance_field_type`
- Result in HTTP 502 `output_contract_failed`

---

## Why First Fix Failed

The first fix added `minLength: 1` to prevent empty strings at the schema level:

```python
{
    "type": ["string", "null"],
    "minLength": 1  # ❌ NOT SUPPORTED by Anthropic
}
```

While `minLength` is valid JSON Schema and worked locally, Anthropic's tool schema processor doesn't support it. This caused:

```json
{
  "detail": {
    "error": "llm_call_failed",
    "message": "reserved_schema_invalid:unsupported_keyword",
    "type": "ValueError"
  }
}
```

---

## The Correct Fix

Instead of constraining the schema, **normalize empty strings in the validation layer**:

### Location
`canonical_result_contract.py`, `validate_canonical_result_structure()` function

### Code
```python
if slug in FINANCE_AGENT_IDS:
    if "finance_proposal" not in value:
        return _failure("missing_finance_fields", ("finance_proposal",))
    
    # Normalize empty strings to null for nullable string fields
    fp = value["finance_proposal"]
    if isinstance(fp, dict):
        nullable_string_fields = {
            "entity_id", "period",
            "transaction_or_schedule_type", "proposed_treatment",
            "account_mapping", "tracking_mapping",
            "tax_treatment", "amount_logic",
            "recommended_financeos_action"
        }
        for field in nullable_string_fields:
            if field in fp and fp[field] == "":
                fp[field] = None
    
    finance = _validate_finance_proposal(fp)
    if not finance.ok:
        return finance
```

### Flow
```
LLM generates empty string ""
↓
Passes Anthropic schema validation (valid string type)
↓
Enters validation layer
↓
Normalized to null (empty = "not applicable")
↓
Passes strict local validation
↓
HTTP 200 with ANALYSIS_COMPLETE
```

---

## Why This is Correct

### Semantic Equivalence
Empty strings have **no business meaning** distinct from `null` for fields like `entity_id` and `period`:
- `""` = no identifier provided = not applicable = `null`
- This normalization preserves semantic intent

### Contract Preservation
- Empty strings are still not accepted as valid business identifiers
- They're coerced to the semantically equivalent `null`
- Non-empty strings must still be meaningful values
- All validation rules remain enforced

### Production Safety
- Uses only supported schema keywords
- No risk of `unsupported_keyword` errors
- Compatible with all provider validators

---

## Regression Coverage

Updated `tests/test_finance_proposal_empty_string_regression.py` with:

1. **test_finance_proposal_empty_strings_are_normalized_to_null**
   - Verifies `""` → `null` normalization
   - Confirms validation passes after normalization

2. **test_finance_proposal_entity_id_and_period_accept_null_and_nonempty**
   - Verifies `null` still works
   - Verifies non-empty strings still work

3. **test_schema_has_no_unsupported_keywords**
   - Ensures schema has NO `minLength`
   - Prevents regression to unsupported keywords

4. **test_all_four_failing_agents_handle_empty_strings**
   - Tests all four agents
   - Confirms consistent behavior

All tests pass: ✅

---

## Deployment Verification

After deploying PR #4 to `https://swarmsync-agents.onrender.com`:

```bash
for agent in genesis-fin-close genesis-fin-controller \
             genesis-fin-payroll-tax genesis-fin-revenue; do
  curl -X POST "https://swarmsync-agents.onrender.com/agents/$agent/run" \
    -H "x-agent-gateway-secret: $SECRET" \
    -H "Content-Type: application/json" \
    -d '{"task":"AUDIT_PROBE_ONLY: confirm endpoint receipt with ANALYSIS_COMPLETE","mode":"analysis"}' \
    | jq -r '.status, .output_contract_eval.ok'
done
```

Expected for each:
```
ANALYSIS_COMPLETE
true
```

Should NOT see:
- `unsupported_keyword` (PR #3 error)
- `invalid_finance_field_type` (original error)

---

## Files Changed

### Core Fix
- `canonical_result_contract.py` (lines 497-499, 1317-1332)

### Tests
- `tests/test_finance_proposal_empty_string_regression.py` (completely rewritten)

### Documentation
- `FINANCE_AGENT_FIX_REPORT.md` (updated with corrected approach)
- `demonstrate_fix.py` (updated to show normalization)
- `CORRECTED_FIX_SUMMARY.md` (this file)

---

## Lessons Learned

1. **Local success ≠ Production success**
   - Schema keywords that work locally may not be supported by live providers
   - Always verify provider-specific keyword support

2. **Normalization > Schema Constraints (when keywords aren't supported)**
   - If schema-level fixes use unsupported keywords, handle edge cases in normalization layer
   - This approach is more flexible and provider-agnostic

3. **Semantic Equivalence Enables Safe Normalization**
   - Empty strings for business identifiers have no meaning distinct from null
   - Normalizing `""` → `null` preserves intent without weakening contracts

4. **Test at Every Level**
   - Schema generation (supported keywords)
   - Local validation (semantic rules)
   - Provider validation (keyword support)
   - End-to-end integration (live gateway)

---

## Status

- ✅ PR #4 created: https://github.com/BenStoneAI/Genesis-Agents/pull/4
- ✅ All local tests passing
- ✅ No unsupported schema keywords
- ⏳ Awaiting merge and production deployment
- ⏳ Live verification pending

---

## Summary

The corrected fix normalizes empty strings to `null` in the validation layer instead of using unsupported schema keywords. This production-safe approach preserves semantic intent, maintains contract integrity, and works with all provider validators.
