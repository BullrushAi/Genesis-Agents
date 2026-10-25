"""Phase 5: clarification-representation steering in the candidate request.

Exactly one steering rule ships: the clarification-null override. It is
candidate-generation instruction only - it must appear exactly once, after
the hash-pinned policy instructions, on every canonical candidate request
(finance and master slugs alike), and it must never be duplicated when the
caller already supplied it. The dropped phase-4 rules (participle bans,
verb/noun separation, gate phrasing) must NOT be present. The validator,
the judge, and the pinned policy constants are untouched by it.
"""
from __future__ import annotations

import json
import math

from bundle_loader import load_bundle
from canonical_result_contract import (
    CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION,
    FINANCE_COMPACT_RESULT_INSTRUCTION,
)
from runtime.semantic_safety_judge import (
    CANONICAL_CANDIDATE_CLARIFICATION_INSTRUCTION,
    build_canonical_candidate_request,
)


def _build(slug: str, extra_messages: list[dict] | None = None) -> dict:
    bundle = load_bundle(slug)
    messages = [
        {"role": "system", "content": bundle["system_prompt"]},
        {"role": "user", "content": "bounded provider payload diagnostic"},
    ]
    if extra_messages:
        messages.extend(extra_messages)
    return build_canonical_candidate_request(
        slug=slug, messages=messages, requested_max_tokens=8_000,
    )


def test_clarification_rule_present_once_after_policy_instructions() -> None:
    body = _build("genesis-fin-controller")
    system = body["system"]
    assert system.count(CANONICAL_CANDIDATE_CLARIFICATION_INSTRUCTION) == 1
    assert system.count(CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION) == 1
    assert system.count(FINANCE_COMPACT_RESULT_INSTRUCTION) == 1
    assert system.index(CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION) < system.index(
        FINANCE_COMPACT_RESULT_INSTRUCTION
    )
    assert system.index(FINANCE_COMPACT_RESULT_INSTRUCTION) < system.index(
        CANONICAL_CANDIDATE_CLARIFICATION_INSTRUCTION
    )


def test_clarification_rule_present_for_master_slug() -> None:
    body = _build("genesis-automation-architect")
    system = body["system"]
    assert system.count(CANONICAL_CANDIDATE_CLARIFICATION_INSTRUCTION) == 1
    assert system.count(FINANCE_COMPACT_RESULT_INSTRUCTION) == 0


def test_clarification_rule_not_duplicated_when_caller_supplied_it() -> None:
    body = _build(
        "genesis-fin-controller",
        extra_messages=[
            {
                "role": "system",
                "content": CANONICAL_CANDIDATE_CLARIFICATION_INSTRUCTION,
            },
        ],
    )
    assert body["system"].count(CANONICAL_CANDIDATE_CLARIFICATION_INSTRUCTION) == 1


def test_clarification_rule_keeps_wire_payload_within_bound() -> None:
    body = _build("genesis-fin-payroll-tax")
    encoded = json.dumps(
        body, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    assert math.ceil(len(encoded) / 4) <= 4_700


def test_only_the_clarification_rule_ships() -> None:
    text = CANONICAL_CANDIDATE_CLARIFICATION_INSTRUCTION
    assert "key clarification" in text
    assert "exactly once, with value null" in text
    assert "Omitting the clarification key is invalid" in text
    assert "Never emit clarification_needed" in text
    # The dropped phase-4 rules must not ride along.
    assert "past participles" not in text
    assert "DECISION_NEEDED" not in text
    assert "'support file'" not in text
    assert "for controller review" not in text
