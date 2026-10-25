"""Phase 7 pins: the standing clarification-representation footer.

Phase 6 proved the per-reason clarification hint works when it is the
retry's reason (runs 5/7 recovered the submission stage), but the dominant
remaining path was structural: a retry triggered by a deterministic reason
(finance/completion) regenerated a candidate that newly omitted the
`clarification` key, and that retry never carried the clarification
reminder (runs 6/10, both terminal).

Phase 7 wraps the single correction construction in each file so EVERY
correction ends with the standing footer. Per-reason hint bodies are
untouched (byte-exact pins live in
test_canonical_clarification_retry_hint.py). The validator
(canonical_result_contract.py) is untouched.
"""
from __future__ import annotations

import agent_runtime
import main

FOOTER = (
    "Standing requirement for every resubmission: include the key "
    "clarification exactly once with value null and every other required "
    "field; never emit clarification_needed, candidate_resolution, or any "
    "other flat clarification field."
)

ALL_REASONS = [
    "finance_execution_claim",
    "real_world_completion_claim",
    "private_durable_memory",
    "direct_human_contact",
    "unsupported_human_question",
    "provider_clarification_representation_invalid",
    # Reasons with no specific hint entry exercise the fallback path.
    "some_unknown_reason",
    "another_unmapped_reason",
]


def test_footer_constant_identical_in_both_modules():
    assert agent_runtime._CLARIFICATION_REPRESENTATION_FOOTER == FOOTER
    assert main._CLARIFICATION_REPRESENTATION_FOOTER == FOOTER


def test_agent_runtime_footer_on_every_reason():
    for reason in ALL_REASONS:
        for fields in ([], ["summary"], ["findings", "proposals"]):
            text = agent_runtime._contract_correction(reason, fields)
            assert text.count(FOOTER) == 1, (reason, fields)
            assert text.endswith(FOOTER), (reason, fields)
            # The footer is appended after the pre-existing final sentence,
            # never interleaved into the hint body.
            assert (
                "compliant V5 result tool call. " + FOOTER
            ) in text, (reason, fields)


def test_agent_runtime_footer_with_no_reason():
    text = agent_runtime._contract_correction(None, None)
    assert text.count(FOOTER) == 1
    assert text.endswith(FOOTER)
