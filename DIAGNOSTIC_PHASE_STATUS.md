# Finance Agent Failures - Diagnostic Phase

## Current Status

After PR #4 (empty-string normalization):
- ✅ **genesis-fin-ap** now passes (unsupported_keyword error fixed)
- ❌ **Four agents still fail** with `output_contract_failed`:
  - genesis-fin-close
  - genesis-fin-controller
  - genesis-fin-payroll-tax
  - genesis-fin-revenue

**Problem**: The live error has empty `message` and `null` type, making root cause diagnosis impossible.

---

## PR #5: Diagnostic Instrumentation

### Changes

**Improved Error Detail** - Added to `main.py`:
```python
detail={
    "error": "output_contract_failed",
    "reason": deterministic_reason,
    "failing_fields": list(deterministic_failing_fields),
    "agentSlug": canonical_slug,
}
```

**Diagnostic Tools**:
- `diagnose_finance_failures.py` - Local validation simulation
- `test_prefix_failures.py` - Narrative prefix testing

### Why This Is Needed

We've been fixing hypothetical issues without seeing actual failures:
1. **PR #3**: Added `minLength` (broke production with unsupported keyword)
2. **PR #4**: Added empty-string normalization (fixed unsupported keyword but four still fail)
3. **Current**: Don't know what's actually failing

**PR #5 makes the error visible** so we can fix the real issue.

---

## Investigation Findings

### ✅ Local Validation Passes
All finance agents (failing and passing) pass local validation with minimal probe results. This proves:
- The validation logic is correct
- The empty-string normalization works
- **The issue is in what the LLM generates in production**

### Potential Failure Modes

Based on validation code analysis:

1. **`invalid_narrative_prefix`**
   - LLM generates text without required prefixes (ANALYSIS:, PROPOSAL:, etc.)
   - Example: `"transaction_or_schedule_type": "Commission calculation"` (missing ANALYSIS:)
   - Confirmed: Structure validation fails without proper prefixes

2. **`clarification_contract_violated`**
   - LLM sets `clarification_needed=False` but includes clarification details
   - Or sets `clarification_needed=True` but missing required clarification fields

3. **`filler_detected`**
   - LLM uses placeholder text like "N/A", "None", "Not applicable"
   - Semantic safety judge rejects filler content

4. **Other semantic violations**
   - Unsafe claims about execution/completion
   - Missing required finance_proposal fields

### Metadata Patterns (Not Yet Causal)

**All four failing agents**:
- `long_context_required: True`
- `version: 1.0.0`

**But also**:
- genesis-fin-fpa has `long_context_required: True` and **passes**
- genesis-fin-revenue has `absorbed_from: []` (like passers) but **fails**

So neither `long_context_required` nor absorption fully explains the failures.

---

## Post-Deploy Actions

### 1. Run Probe Tests

```bash
for agent in genesis-fin-close genesis-fin-controller \
             genesis-fin-payroll-tax genesis-fin-revenue; do
  echo "=== $agent ==="
  curl -X POST "https://swarmsync-agents.onrender.com/agents/$agent/run" \
    -H "x-agent-gateway-secret: $SECRET" \
    -H "Content-Type: application/json" \
    -d '{"task":"AUDIT_PROBE_ONLY: confirm endpoint receipt","mode":"analysis"}' \
    | jq '.detail'
done
```

### 2. Expected Output (Example)

```json
{
  "error": "output_contract_failed",
  "reason": "invalid_narrative_prefix",
  "failing_fields": ["finance_proposal.transaction_or_schedule_type"],
  "agentSlug": "genesis-fin-close"
}
```

### 3. Analysis Matrix

| reason | meaning | likely fix |
|--------|---------|------------|
| `invalid_narrative_prefix` | Text missing ANALYSIS:/PROPOSAL: prefix | Normalize unprefixed text OR strengthen schema hints |
| `clarification_contract_violated` | Clarification representation wrong | Fix clarification field handling |
| `filler_detected` | LLM used placeholder text | Add filler normalization |
| `invalid_finance_field_type` | Wrong type in finance_proposal | Extend type normalization |

### 4. Once We Know the Reason

**If `invalid_narrative_prefix`**:
- Check which fields fail most often
- Consider normalizing unprefixed text to null (if semantically equivalent)
- Or add stronger schema patterns/descriptions

**If `clarification_contract_violated`**:
- Check if LLM is incorrectly setting clarification_needed
- Normalize clarification representation

**If `filler_detected`**:
- Normalize filler values to null/empty
- Strengthen prompts against filler

**If multiple different reasons**:
- Fix the most common one first
- Deploy, test, iterate

---

## Lessons from This Investigation

### ❌ What Didn't Work
1. **Speculative fixes without evidence**
   - We fixed empty strings, but that wasn't the whole problem
   - We tried minLength, but that broke production

2. **Local testing without live validation**
   - Everything passed locally
   - Production LLM behavior was different

3. **Insufficient error detail**
   - Couldn't diagnose without seeing failure reasons

### ✅ What We Need
1. **Actual failure data** (reason + failing_fields)
2. **Evidence-based fixes** (fix what actually fails)
3. **Regression tests with real failure cases** (not synthetic examples)

---

## Status

- ✅ PR #5 created: https://github.com/BenStoneAI/Genesis-Agents/pull/5
- ⏳ Awaiting merge and deployment
- ⏳ Will run probe tests after deploy
- ⏳ Will implement targeted fix based on actual error data

**Task is NOT done** - this is diagnostic instrumentation to enable the real fix.
