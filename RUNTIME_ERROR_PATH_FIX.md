# Finance Agent Failures - Runtime Error Path Fix

## Critical Discovery

PR #5 added error details but **only to the FastAPI judge path**. The actual live failures go through:

```
AgentRuntime → _raise_for_runtime_failure → HTTPException
```

This path was **ignoring** the `output_contract_eval` data that AgentRuntime already included.

---

## PR #6: The Real Fix

### What Was Wrong

`_raise_for_runtime_failure()` only extracted basic fields:
```python
detail = {
    "error": result.get("error"),
    "message": result.get("message"),
    "type": result.get("type"),
    "agentSlug": slug,
}
```

But `result["output_contract_eval"]` had rich details:
```python
{
    "ok": False,
    "deterministic_reason": "invalid_narrative_prefix",
    "deterministic_failing_fields": ["finance_proposal.transaction_or_schedule_type"],
}
```

### The Fix

Now extracts from `output_contract_eval` when `error == "output_contract_failed"`:
- `reason` (from deterministic_reason, judge_reason, or reason)
- `failing_fields` (from deterministic_failing_fields or failing_fields)
- `contract_ok` (for completeness)

---

## After PR #6 Deploys

### Live Error Will Include Details

Instead of:
```json
{
  "detail": {
    "error": "output_contract_failed",
    "message": "",
    "type": null,
    "agentSlug": "genesis-fin-controller"
  }
}
```

We'll get:
```json
{
  "detail": {
    "error": "output_contract_failed",
    "reason": "invalid_narrative_prefix",
    "failing_fields": ["finance_proposal.transaction_or_schedule_type"],
    "contract_ok": false,
    "agentSlug": "genesis-fin-controller"
  }
}
```

### Run Probe Tests

```bash
for agent in genesis-fin-controller genesis-fin-payroll-tax genesis-fin-revenue; do
  echo "=== Testing $agent ==="
  curl -X POST "https://swarmsync-agents.onrender.com/agents/$agent/run" \
    -H "x-agent-gateway-secret: $SECRET" \
    -H "Content-Type: application/json" \
    -d '{"task":"AUDIT_PROBE_ONLY: confirm endpoint receipt","mode":"analysis"}' \
    | jq '.detail'
  echo
done
```

### Analyze Results

Based on the `reason` field, implement targeted fixes:

| Reason | Fix Strategy |
|--------|-------------|
| `invalid_narrative_prefix` | Normalize unprefixed text to null OR strengthen schema |
| `clarification_contract_violated` | Fix clarification field handling |
| `filler_detected` | Add filler normalization |
| `invalid_finance_field_type` | Extend type normalization |
| `missing_finance_fields` | Check finance_proposal completeness |

---

## Why We've Had Multiple PRs

1. **PR #3**: Fixed hypothetical issue (minLength) → broke production
2. **PR #4**: Fixed production break, added empty-string normalization
3. **PR #5**: Added error detail to **wrong code path** (FastAPI judge)
4. **PR #6**: Added error detail to **correct code path** (AgentRuntime)

We kept fixing in the dark. PR #6 finally turns on the lights.

---

## Next Steps

1. ✅ **PR #6 created**: https://github.com/BenStoneAI/Genesis-Agents/pull/6
2. ⏳ **Merge + Deploy**: Get PR #6 to production
3. ⏳ **Run Probes**: Capture actual `reason` and `failing_fields`
4. ⏳ **Implement Fix**: Based on evidence, not speculation
5. ⏳ **Verify**: Confirm all four agents pass

---

## Current Agent Status

After PR #5 deploy (commit `02e75f5`):
- ✅ **genesis-fin-close**: Passed on this run (intermittent?)
- ✅ **genesis-fin-ap**: Passes consistently
- ❌ **genesis-fin-controller**: Still fails
- ❌ **genesis-fin-payroll-tax**: Still fails
- ❌ **genesis-fin-revenue**: Still fails

---

## Lesson Learned

**Don't fix what you can't see.** 

We need:
1. Detailed error information (now in PR #6)
2. Evidence-based fixes (once we see errors)
3. Regression tests with real failure cases (not synthetic)

PR #6 is the **prerequisite** for any meaningful fix to the remaining failures.
