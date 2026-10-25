"""Authoritative Genesis V5 result contract and content-free validation.

The contract is intentionally independent of provider, transport, and prompt code so the
full AgentRuntime path and the Render-safe ``live_test`` path enforce identical semantics.
It validates supplied business data and never invents business facts. Representation-only
normalization is allowed where the schema defines an omitted nullable finance field as
semantically equivalent to explicit ``null`` or an omitted list as equivalent to ``[]``.
That normalization must not create identifiers, amounts, treatments, approvals, evidence,
or any other business fact.
"""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

CANONICAL_RESULT_CONTRACT_VERSION = "genesis-v5"
CANONICAL_RESULT_TOOL_NAME = "submit_genesis_result_v5"
CANONICAL_STATUSES = (
    "ANALYSIS_COMPLETE",
    "PARTIAL",
    "HOLD",
    "REFUSED",
    "FAILED",
)
CLARIFICATION_URGENCIES = ("IMMEDIATE", "CLOSURE_BATCH", "DEFERRED")
CLARIFICATION_ANSWER_OPTIONS = (
    "CONFIRM",
    "CORRECT",
    "NOT_MINE",
    "PRIVATE_DO_NOT_PROMOTE",
    "DEFER",
    "EXPLAIN",
)

COMMON_RESULT_FIELDS = (
    "agent_id",
    "task_id",
    "scope",
    "status",
    "summary",
    "findings",
    "proposals",
    "evidence_refs",
    "assumptions",
    "conflicts",
    "unknowns",
    "confidence",
    "risk_flags",
    "recommended_next_actions",
    "requested_human_decisions",
    "tool_calls_summary",
)
STRING_LIST_FIELDS = (
    "findings",
    "proposals",
    "evidence_refs",
    "assumptions",
    "conflicts",
    "unknowns",
    "risk_flags",
    "recommended_next_actions",
    "requested_human_decisions",
    "tool_calls_summary",
)
NARRATIVE_RESULT_FIELDS = (
    "summary",
    "findings",
    "proposals",
    "assumptions",
    "conflicts",
    "unknowns",
    "risk_flags",
    "recommended_next_actions",
    "requested_human_decisions",
    "candidate_resolution",
    "reason_human_needed",
    "recommended_human_resolver",
)
FINANCE_NARRATIVE_FIELDS = (
    "transaction_or_schedule_type",
    "proposed_treatment",
    "account_mapping",
    "tracking_mapping",
    "tax_treatment",
    "amount_logic",
    "assumptions",
    "exceptions",
    "recommended_financeos_action",
)
NARRATIVE_INTENT_PREFIXES = {
    "summary": "ANALYSIS",
    "findings": "ANALYSIS",
    "proposals": "PROPOSAL",
    "assumptions": "UNKNOWN",
    "conflicts": "RISK",
    "unknowns": "UNKNOWN",
    "risk_flags": "RISK",
    "recommended_next_actions": "NEXT",
    "requested_human_decisions": "DECISION_NEEDED",
    "candidate_resolution": "PROPOSAL",
    "reason_human_needed": "ANALYSIS",
    "recommended_human_resolver": "NEXT",
    "resolution_attempts.result": "ANALYSIS",
    "finance_proposal.transaction_or_schedule_type": "ANALYSIS",
    "finance_proposal.proposed_treatment": "PROPOSAL",
    "finance_proposal.account_mapping": "PROPOSAL",
    "finance_proposal.tracking_mapping": "PROPOSAL",
    "finance_proposal.tax_treatment": "PROPOSAL",
    "finance_proposal.amount_logic": "PROPOSAL",
    "finance_proposal.assumptions": "UNKNOWN",
    "finance_proposal.exceptions": "RISK",
    "finance_proposal.recommended_financeos_action": "NEXT",
}

CANONICAL_CANDIDATE_SYSTEM_INSTRUCTION = (
    "Genesis V5 intent prefixes are exact: ANALYSIS: for summary, findings, "
    "clarification reasons, attempt results, and finance transaction/schedule type; "
    "PROPOSAL: for proposals, candidate resolution, and proposed finance treatment, "
    "account, tracking, tax, or amount logic; UNKNOWN: for assumptions and unknowns; "
    "RISK: for conflicts, risk flags, and finance exceptions; NEXT: for recommended "
    "actions and human resolver; DECISION_NEEDED: for requested human decisions. "
    "Prefix every supplied nonempty narrative string or array item exactly. A prefix "
    "does not prove safety: never assert that external work was completed, posted, "
    "paid, approved, filed, deployed, or verified. ANALYSIS_COMPLETE means only the "
    "bounded Genesis analysis completed. In the forced provider tool, emit the required "
    "clarification key exactly once with value null. No server-verified resolution attempt "
    "provenance exists in this provider path, so clarification objects are forbidden even if "
    "you can write plausible source names, timestamps or evidence references. Never emit legacy "
    "flat clarification controls. The gateway projects null into unchanged public V5 fields. "
    "Never invent attempts, sources, timestamps or approvals. Preserve "
    "missing inputs in unknowns and recommended next actions. For missing-evidence assessments, "
    "report the evidence gap and bounded next step rather than expanding a hypothetical design. "
    "Use one concise item per narrative array where possible and short phrases for finance "
    "mapping fields. Add items whenever necessary to preserve distinct material conflicts or "
    "safety conditions. Do not repeat the same evidence gap across every field. Group related unknowns "
    "without omitting material conflicts or safety conditions. State unknowns as concise "
    "declarative missing-input facts, not questions about alleged completed external work. "
    "Never propose storing or retaining information in private or durable memory, ledgers or "
    "databases; any persistence design must remain an explicit Cato-owned control-plane decision. "
    "For synthetic evidence-gap assessments, give concise missing-input findings and bounded "
    "next steps; avoid lengthy hypothetical implementation, repeated role disclaimers or "
    "quoting prohibited completion claims. When source records are absent, do not enumerate "
    "implementation schemas, sample identifiers, example completed states, or hypothetical "
    "workflow transitions. Limit the answer to missing evidence and safe retrieval steps; "
    "use declarative statements in unknowns. Request read access decisions without directing "
    "financial actions or contacting people. When task-owner or provider clarification is needed, "
    "recommend returning the evidence gap to Cato's Clarification Engine for orchestrator-owned "
    "resolution. Never instruct anyone to contact, message, email or notify a person directly. "
    "Submit exactly one complete V5 result tool call."
)
FINANCE_COMPACT_RESULT_INSTRUCTION = (
    "Finance only, source-bound: submit the shortest complete V5 forced-tool input using only "
    "supplied or authorized source evidence; do not restate the task "
    "or duplicate facts across narrative fields. Use null or [] only for absent nullable structured "
    "non-narrative fields. Every supplied narrative string must be non-empty and begin with its "
    "canonical allowed prefix; prefix compliance overrides brevity. Every narrative is analysis, "
    "proposal, unknown, risk, next action, or decision-needed only, even under a valid prefix. "
    "Never state or imply that any posting, payment, filing, invoice, journal, close, execution, "
    "approval, verification, or completion occurred. Evidence references, provider submission, "
    "and phrasing do not prove external state; Genesis has no trusted runtime observation here. "
    "Target <=2400 output tokens; never omit any required field. The finance_proposal "
    "must contain all fifteen schema fields including recommended_financeos_action even when "
    "source evidence is absent; use its NEXT: prefixed evidence-retrieval recommendation."
)
FINANCE_NARRATIVE_SAFETY_DESCRIPTION = (
    "Finance intent only (analysis/proposal/unknown/risk/next/decision-needed), even with prefix; "
    "never affirm posting/payment/filing/invoice/journal/close/execution/approval/verification/"
    "completion occurred."
)
MASTER_COMPACT_RESULT_INSTRUCTION = (
    "Master only: submit the shortest complete source-bound V5 forced-tool input; do not "
    "restate the task or duplicate facts; retain every required field; every narrative string "
    "must use its canonical prefix and prefix compliance overrides brevity."
)

CLARIFICATION_FIELDS = (
    "clarification_needed",
    "candidate_resolution",
    "reason_human_needed",
    "recommended_human_resolver",
    "materiality",
    "urgency",
    "resolution_attempts",
    "supporting_evidence_refs",
    "competing_state_refs",
    "recommended_answer_options",
)
RESULT_CLARIFICATION_FIELDS = CLARIFICATION_FIELDS
_FALSE_CLARIFICATION_EMPTY_STRING_FIELDS = frozenset({
    "candidate_resolution",
    "reason_human_needed",
    "recommended_human_resolver",
    "urgency",
})
FINANCE_PROPOSAL_FIELDS = (
    "entity_id",
    "period",
    "transaction_or_schedule_type",
    "proposed_treatment",
    "account_mapping",
    "tracking_mapping",
    "tax_treatment",
    "amount_logic",
    "source_refs",
    "policy_refs",
    "assumptions",
    "confidence",
    "exceptions",
    "requires_human_decision",
    "recommended_financeos_action",
)
FINANCE_AGENT_IDS = frozenset(
    {
        "genesis-fin-revenue",
        "genesis-fin-ap",
        "genesis-fin-ar",
        "genesis-fin-reconciliation",
        "genesis-fin-treasury",
        "genesis-fin-close",
        "genesis-fin-fpa",
        "genesis-fin-payroll-tax",
        "genesis-fin-controller",
    }
)
MASTER_AGENT_IDS = frozenset(
    {
        "genesis-knowledge-evidence",
        "genesis-execution-control",
        "genesis-automation-architect",
        "genesis-integration-architect",
        "genesis-platform-engineer",
        "genesis-data-intelligence",
        "genesis-security-identity",
        "genesis-legal-risk",
        "genesis-commercial-growth",
        "genesis-people-operations",
    }
)
CANONICAL_AGENT_IDS = MASTER_AGENT_IDS | FINANCE_AGENT_IDS

_FILLER_VALUES = frozenset({"n/a", "na", "none", "null", "unknown", "tbd", "todo", "placeholder"})
_UNSUPPORTED_QUESTION = re.compile(
    r"^\s*(?:who owns this\??|what did you do today\??|please clarify\.?|can you clarify\??)\s*$",
    re.IGNORECASE,
)
_BROAD_DAILY_QUESTION = re.compile(
    r"\b(?:what|which|give\s+me|tell\s+me|rundown)\b.*"
    r"\b(?:task|work|thing|everything|accomplish|complete|do|did)\w*\b.*"
    r"\b(?:today|this\s+morning|this\s+afternoon|this\s+evening|since\s+yesterday)\b",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY = re.compile(
    r"\s*(?:[.!?;:\u2014\u2013\r\n]+|"
    r",\s*(?=(?:message|dm|email|ping|contact|notify|call|text|send|deliver|post|store|save|"
    r"retain|remember|persist|record|write|pay|issue|execute|run|file|submit|transfer|move|"
    r"refund|approve|deployment|release)\b)|"
    r",?\s+\b(?:but|however|yet|instead|then|and\s+then|and|or)\b\s+)\s*",
    re.IGNORECASE,
)
_DIRECT_CONTACT = re.compile(
    r"\b(?:message|dm|email|ping|contact|notify|call|text|send|deliver|post(?!-handoff\b))\b"
    r".{0,80}\b(?:employee|staff|worker|person|owner|human|slack|teams|email|sms)\b",
    re.IGNORECASE,
)
_PRIVATE_MEMORY = re.compile(
    r"\b(?:store|save|retain|remember|persist|record|write)\b.{0,100}"
    r"\b(?:private|durable|internal)\b.{0,40}\b(?:memory|ledger|database|store)\b",
    re.IGNORECASE,
)
# `period`, `invoice`, and bare `work` cannot share generic "complete/finished"
# verbs. Bounded summaries like "this work is complete" mean the analysis
# assignment finished, not a real-world posting, payment, or close.
_COMPLETION_NOUNS = (
    r"(?:deployment|release|payment|refund|filing|tax\s+return|close|journal|"
    r"business\s+task)"
)
_PERIOD_CLOSE_NOUN = r"(?:period\s+close|close\s+of(?:\s+the)?\s+period|accounting\s+period)"
_INVOICE_NOUN = r"(?:invoice|bill)"
_GENERAL_COMPLETION_VERBS = (
    r"(?:completed?|deployed|posted|paid|filed|approved|executed|verified|settled|"
    r"sent|issued|closed|finished)"
)
_INVOICE_EXECUTION_VERBS = (
    r"(?:deployed|posted|paid|filed|approved|executed|verified|settled|sent|issued|"
    r"closed)"
)
_REAL_WORLD_COMPLETION = re.compile(
    r"\b(?:we|I|the\s+(?:controller|approver))\s+(?:have\s+|has\s+)?approved\s+(?:the\s+)?(?:tax\s+|close\s+|commission\s+)?(?:policy|plan)\b|"
    rf"\b{_COMPLETION_NOUNS}\b.{{0,90}}\b{_GENERAL_COMPLETION_VERBS}\b|"
    rf"\b{_GENERAL_COMPLETION_VERBS}\b.{{0,90}}\b{_COMPLETION_NOUNS}\b|"
    rf"\b{_PERIOD_CLOSE_NOUN}\b.{{0,90}}\b{_GENERAL_COMPLETION_VERBS}\b|"
    rf"\b{_GENERAL_COMPLETION_VERBS}\b.{{0,90}}\b{_PERIOD_CLOSE_NOUN}\b|"
    rf"\bperiod\b.{{0,40}}\b(?:is|was|were|has\s+been|have\s+been)\s+closed\b|"
    rf"\b{_INVOICE_NOUN}\b.{{0,90}}\b{_INVOICE_EXECUTION_VERBS}\b|"
    rf"\b{_INVOICE_EXECUTION_VERBS}\b.{{0,90}}\b{_INVOICE_NOUN}\b|"
    rf"(?:\b(?:deployment|release|payment|refund|filing|journal|invoice|business\s+task)\b.{{0,90}}\bindependently\s+verified\b|"
    rf"\bindependently\s+verified\b.{{0,90}}\b(?:deployment|release|payment|refund|filing|journal|invoice|business\s+task)\b)",
    re.IGNORECASE,
)
_FINANCE_EXECUTION = re.compile(
    r"\b(?:pay|post|issue|execute|run|file|submit|transfer|move|refund|approve)\b.{0,90}"
    r"\b(?:supplier|payment|journal|xero|tax\s+filing|payroll|funds?|refund|invoice|bill)\b|"
    r"\b(?:supplier|payment|journal|xero|tax\s+filing|payroll|funds?|refund|invoice|bill)\b"
    r".{0,90}\b(?:pay|post|issue|execute|run|file|submit|transfer|move|refund|approve)\b",
    re.IGNORECASE,
)
_NEGATED_PROHIBITED_ACTION = re.compile(
    r"^\s*(?:do\s+not|don't|never(?!\s+mind\b)|must\s+not|cannot|can't)\s+"
    r"(?:claim\s+(?:that\s+|it\s+is\s+)?|directly\s+)?"
    r"(?:message|dm|email|ping|contact|notify|call|text|send|deliver|post|store|save|retain|"
    r"remember|persist|record|write|pay|issue|execute|run|file|submit|transfer|move|refund|"
    r"approve|ask|report|declare|describe|verify)\b",
    re.IGNORECASE,
)
_NEGATED_COMPLETION_FACT = re.compile(
    r"\b(?:deployment|release|payment|refund|filing|tax\s+return|close|period|journal|invoice|"
    r"business\s+task|work)\b.{0,40}\b(?:is|was|were|has|have|had)\s+not\s+"
    r"(?:been\s+)?(?:completed?|deployed|posted|paid|filed|approved|executed|verified|settled|"
    r"sent|issued|closed|finished)\b|"
    r"\bno\s+(?:supplier\s+)?(?:invoice|bill|journal|payment|filing)\b.{0,20}"
    r"(?:was\s+|were\s+|is\s+|has\s+been\s+)?"
    r"(?:posted|paid|filed|approved|issued|deployed|completed?)\b",
    re.IGNORECASE,
)
_SAFE_FINANCE_PROPOSAL = re.compile(
    r"^\s*(?:propose|draft|recommend|create)\b(?=.{0,140}\b(?:for\s+(?:financeos\s+)?review|"
    r"subject\s+to.{0,50}\bapproval|posting\s+intent)\b).*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ContractValidation:
    """A content-free validation verdict safe for logs and HTTP error details."""

    ok: bool
    reason: str | None = None
    missing_fields: tuple[str, ...] = ()
    failing_fields: tuple[str, ...] = ()

    def metadata(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "missing_fields": list(self.missing_fields),
            "failing_fields": list(self.failing_fields),
            "failing_field_count": len(self.failing_fields),
        }


@dataclass(frozen=True)
class ParsedCanonicalResult:
    ok: bool
    reason: str | None
    missing_fields: tuple[str, ...]
    normalized_code_fence: bool
    value: dict[str, Any] | None


@dataclass(frozen=True)
class CanonicalToolSubmission:
    """Content-free verdict for the reserved non-executing provider tool."""

    ok: bool
    reason: str | None
    value: dict[str, Any] | None
    representation_subtype: str | None = None
    representation_missing_fields: tuple[str, ...] = ()
    representation_extra_fields: tuple[str, ...] = ()
    representation_missing_field_count: int = 0
    representation_extra_field_count: int = 0


PROVIDER_CLARIFICATION_REPRESENTATION_SUBTYPES = frozenset({
    "canonical_object",
    "legacy_flat",
    "both_equal",
    "representation_missing",
    "legacy_partial",
    "canonical_invalid",
    "legacy_invalid",
    "both_mismatch",
})


@dataclass(frozen=True)
class ProviderClarificationProjection:
    """Content-free provider representation verdict and unchanged flat V5 value."""

    ok: bool
    reason: str | None
    value: dict[str, Any] | None
    subtype: str
    missing_fields: tuple[str, ...] = ()
    extra_fields: tuple[str, ...] = ()
    missing_field_count: int = 0
    extra_field_count: int = 0


def _failure(
    reason: str, missing: tuple[str, ...] = (), *, failing: tuple[str, ...] = (),
) -> ContractValidation:
    return ContractValidation(False, reason, missing, failing)


def _provider_nullable_type(type_name: str) -> dict[str, Any]:
    """Anthropic-compatible nullable scalar; semantic constraints remain local."""
    return {"type": [type_name, "null"]}


def _intent_string_schema(
    field: str, *, nullable: bool = False, allow_empty: bool = False,
) -> dict[str, Any]:
    """Return the compact provider shape; exact prefixes are enforced locally."""
    if field not in NARRATIVE_INTENT_PREFIXES:
        raise ValueError("unknown narrative field")
    schema = _provider_nullable_type("string") if nullable else {"type": "string"}
    prefix = NARRATIVE_INTENT_PREFIXES[field]
    schema["pattern"] = (
        rf"^(?:$|{prefix}:\s+\S)" if allow_empty else rf"^{prefix}:\s+\S"
    )
    return schema


def _intent_string_list_schema(field: str) -> dict[str, Any]:
    """Return the compact provider list shape; local validation owns intent."""
    if field not in NARRATIVE_INTENT_PREFIXES:
        raise ValueError("unknown narrative field")
    return {
        "type": "array",
        "items": {
            "type": "string",
            "pattern": rf"^{NARRATIVE_INTENT_PREFIXES[field]}:\s+\S",
        },
    }


def canonical_result_json_schema(slug: str) -> dict[str, Any]:
    """Public/local flat V5 schema derived from the authoritative validator constants."""
    if slug not in CANONICAL_AGENT_IDS:
        raise ValueError("unknown canonical agent")
    string_list = {"type": "array", "items": {"type": "string"}}
    properties: dict[str, Any] = {
        "agent_id": {"type": "string", "enum": [slug]},
        "task_id": {"type": "string"},
        "scope": {"type": "string"},
        "status": {
            "type": "string",
            "enum": list(CANONICAL_STATUSES),
            "description": "ANALYSIS_COMPLETE denotes bounded analysis only.",
        },
        "summary": _intent_string_schema("summary"),
        "confidence": {"type": "number"},
        "clarification_needed": {"type": "boolean"},
    }
    properties.update({field: dict(string_list) for field in STRING_LIST_FIELDS})
    for field in NARRATIVE_RESULT_FIELDS:
        if field == "summary":
            continue
        if field in STRING_LIST_FIELDS:
            properties[field] = _intent_string_list_schema(field)
        else:
            properties[field] = _intent_string_schema(field)
    properties.update({
        "materiality": _provider_nullable_type("number"),
        "urgency": {
            "type": ["string", "null"],
            "enum": [*CLARIFICATION_URGENCIES, "", None],
            "pattern": r"^(?:$|IMMEDIATE|CLOSURE_BATCH|DEFERRED)$",
        },
        "resolution_attempts": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source_or_capability", "result", "timestamp"],
                "properties": {
                    "source_or_capability": {"type": "string"},
                    "result": _intent_string_schema("resolution_attempts.result"),
                    "timestamp": {"type": "string"},
                },
            },
        },
        "supporting_evidence_refs": {**string_list, "type": ["array", "null"]},
        "competing_state_refs": {**string_list, "type": ["array", "null"]},
        "recommended_answer_options": {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": list(CLARIFICATION_ANSWER_OPTIONS)},
        },
    })
    for field in (
        "candidate_resolution", "reason_human_needed", "recommended_human_resolver",
    ):
        properties[field] = _intent_string_schema(field, nullable=True, allow_empty=True)
    required = [*COMMON_RESULT_FIELDS, "clarification_needed"]
    if slug in FINANCE_AGENT_IDS:
        finance_properties: dict[str, Any] = {
            field: _provider_nullable_type("string") for field in ("entity_id", "period")
        }
        finance_properties.update({
            field: _intent_string_schema(f"finance_proposal.{field}", nullable=True)
            for field in (
                "transaction_or_schedule_type", "proposed_treatment", "account_mapping",
                "tracking_mapping", "tax_treatment", "amount_logic",
                "recommended_financeos_action",
            )
        })
        finance_properties.update({
            field: dict(string_list)
            for field in ("source_refs", "policy_refs")
        })
        finance_properties["assumptions"] = _intent_string_list_schema("finance_proposal.assumptions")
        finance_properties["exceptions"] = _intent_string_list_schema("finance_proposal.exceptions")
        finance_properties["confidence"] = _provider_nullable_type("number")
        finance_properties["requires_human_decision"] = _provider_nullable_type("boolean")
        finance_properties["proposed_treatment"]["description"] = "Proposal for review only."
        finance_properties["recommended_financeos_action"]["description"] = (
            "Recommended review action only."
        )
        for field in NARRATIVE_RESULT_FIELDS:
            properties[field]["description"] = FINANCE_NARRATIVE_SAFETY_DESCRIPTION
        properties["resolution_attempts"]["items"]["properties"]["result"][
            "description"
        ] = FINANCE_NARRATIVE_SAFETY_DESCRIPTION
        for field in FINANCE_NARRATIVE_FIELDS:
            finance_properties[field]["description"] = FINANCE_NARRATIVE_SAFETY_DESCRIPTION
        properties["finance_proposal"] = {
            "type": "object",
            "additionalProperties": False,
            "required": list(FINANCE_PROPOSAL_FIELDS),
            "properties": finance_properties,
        }
        required.append("finance_proposal")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def canonical_provider_result_json_schema(slug: str) -> dict[str, Any]:
    """Provider V5 shape: clarification requires unavailable trusted provenance."""
    public = deepcopy(canonical_result_json_schema(slug))
    properties = public["properties"]
    public_required = public["required"]

    for field in CLARIFICATION_FIELDS:
        properties.pop(field, None)
    # No server-verified attempt registry exists. The public V5 object remains
    # available to trusted callers, but a provider cannot assert its provenance.
    properties["clarification"] = {"type": "null"}
    public["required"] = [
        *(field for field in public_required if field != "clarification_needed"),
        "clarification",
    ]
    return public


def _ordered_provider_fields(fields: set[str]) -> tuple[str, ...]:
    allowlist = ("clarification", *CLARIFICATION_FIELDS)
    return tuple(field for field in allowlist if field in fields)


def _projection_failure(
    subtype: str, *, missing: set[str] | None = None,
    extra: set[str] | None = None, extra_count: int | None = None,
) -> ProviderClarificationProjection:
    missing = missing or set()
    extra = extra or set()
    safe_missing = _ordered_provider_fields(missing)
    safe_extra = _ordered_provider_fields(extra)
    return ProviderClarificationProjection(
        False,
        "provider_clarification_representation_invalid",
        None,
        subtype,
        safe_missing,
        safe_extra,
        len(missing),
        len(extra) if extra_count is None else extra_count,
    )


def _project_canonical_clarification(value: Mapping[str, Any]) -> ProviderClarificationProjection:
    projected = deepcopy(dict(value))
    clarification = projected.pop("clarification")
    if clarification is None:
        projected.update({
            "clarification_needed": False,
            "candidate_resolution": "",
            "reason_human_needed": "",
            "recommended_human_resolver": "",
            "materiality": None,
            "urgency": "",
            "resolution_attempts": [],
            "supporting_evidence_refs": [],
            "competing_state_refs": [],
            "recommended_answer_options": [],
        })
        return ProviderClarificationProjection(True, None, projected, "canonical_object")
    expected = set(CLARIFICATION_FIELDS[1:])
    if not isinstance(clarification, Mapping):
        return _projection_failure("canonical_invalid", missing=expected)
    actual = set(clarification)
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        return _projection_failure(
            "canonical_invalid", missing=missing, extra=extra,
            extra_count=len(extra),
        )
    return _projection_failure("canonical_invalid")


def _exact_provider_value_equal(left: Any, right: Any) -> bool:
    try:
        return json.dumps(
            left, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ) == json.dumps(right, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return False


def project_provider_canonical_result_with_metadata(
    slug: str, value: Any,
) -> ProviderClarificationProjection:
    """Accept canonical provider shape or a provably complete legacy representation."""
    if slug not in CANONICAL_AGENT_IDS or not isinstance(value, Mapping):
        return _projection_failure("representation_missing", missing={"clarification"})
    has_canonical = "clarification" in value
    legacy_present = {field for field in CLARIFICATION_FIELDS if field in value}
    expected_legacy = set(CLARIFICATION_FIELDS)
    has_all_legacy = legacy_present == expected_legacy

    if not has_canonical and not legacy_present:
        return _projection_failure("representation_missing", missing={"clarification"})
    if legacy_present and not has_all_legacy:
        return _projection_failure(
            "legacy_partial", missing=expected_legacy - legacy_present,
            extra=legacy_present,
        )

    canonical_projection = (
        _project_canonical_clarification(value) if has_canonical else None
    )
    if canonical_projection is not None and not canonical_projection.ok:
        return canonical_projection

    if has_all_legacy:
        legacy = deepcopy(dict(value))
        legacy.pop("clarification", None)
        if legacy.get("clarification_needed") is not False:
            return _projection_failure("legacy_invalid")
        clarification_verdict = _validate_clarification(legacy)
        if not clarification_verdict.ok:
            return _projection_failure("legacy_invalid")
        if canonical_projection is None:
            return ProviderClarificationProjection(
                True, None, legacy, "legacy_flat",
            )
        assert canonical_projection.value is not None
        if not all(
            _exact_provider_value_equal(
                canonical_projection.value.get(field), legacy.get(field),
            )
            for field in CLARIFICATION_FIELDS
        ):
            return _projection_failure(
                "both_mismatch", extra=expected_legacy,
            )
        return ProviderClarificationProjection(
            True, None, canonical_projection.value, "both_equal",
        )

    assert canonical_projection is not None
    return canonical_projection


def project_provider_canonical_result(slug: str, value: Any) -> dict[str, Any]:
    """Compatibility wrapper returning only the unchanged flat V5 value."""
    projection = project_provider_canonical_result_with_metadata(slug, value)
    if not projection.ok or projection.value is None:
        raise ValueError("provider_clarification_representation_invalid")
    return projection.value


def canonical_result_tool_schema(slug: str) -> dict[str, Any]:
    """Return the one reserved Anthropic result-submission tool; never executable."""
    return {
        "name": CANONICAL_RESULT_TOOL_NAME,
        "description": "Submit the final Genesis V5 result for local contract validation.",
        "input_schema": canonical_provider_result_json_schema(slug),
        # Local-only binding used by the wire preflight; stripped before HTTP.
        "_genesis_agent_id": slug,
    }


def parse_canonical_result_submission(response: Any) -> CanonicalToolSubmission:
    """Parse exactly one reserved-tool envelope without judging its arguments."""
    if not isinstance(response, Mapping):
        return CanonicalToolSubmission(False, "canonical_result_tool_missing", None)
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], Mapping):
        return CanonicalToolSubmission(False, "canonical_result_tool_missing", None)
    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        return CanonicalToolSubmission(False, "provider_output_truncated", None)
    if finish_reason == "refusal":
        return CanonicalToolSubmission(False, "provider_refusal", None)
    message = choice.get("message")
    if not isinstance(message, Mapping):
        return CanonicalToolSubmission(False, "canonical_result_tool_missing", None)
    calls = message.get("tool_calls")
    calls = calls if isinstance(calls, list) else []
    content = message.get("content")
    has_text = isinstance(content, str) and bool(content.strip())
    if not calls:
        reason = "canonical_result_text_fallback" if has_text else "canonical_result_tool_missing"
        return CanonicalToolSubmission(False, reason, None)
    if len(calls) != 1:
        return CanonicalToolSubmission(False, "canonical_result_tool_multiple", None)
    call = calls[0]
    function = call.get("function") if isinstance(call, Mapping) else None
    if not isinstance(function, Mapping) or function.get("name") != CANONICAL_RESULT_TOOL_NAME:
        return CanonicalToolSubmission(False, "canonical_result_tool_mismatch", None)
    if has_text:
        return CanonicalToolSubmission(False, "canonical_result_text_with_tool", None)
    arguments = function.get("arguments")
    try:
        value = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (TypeError, ValueError):
        return CanonicalToolSubmission(False, "response_not_json_object", None)
    if not isinstance(value, dict):
        return CanonicalToolSubmission(False, "response_not_json_object", None)
    return CanonicalToolSubmission(True, None, value)


def consume_canonical_result_submission(slug: str, response: Any) -> CanonicalToolSubmission:
    """Parse the exact reserved envelope and enforce deterministic structure only."""
    parsed = parse_canonical_result_submission(response)
    if not parsed.ok or parsed.value is None:
        return parsed
    projection = project_provider_canonical_result_with_metadata(slug, parsed.value)
    metadata = (
        projection.subtype,
        projection.missing_fields,
        projection.extra_fields,
        projection.missing_field_count,
        projection.extra_field_count,
    )
    if not projection.ok or projection.value is None:
        return CanonicalToolSubmission(False, projection.reason, None, *metadata)
    projected = projection.value
    verdict = validate_canonical_result_structure(slug, projected)
    return CanonicalToolSubmission(
        verdict.ok, verdict.reason, projected if verdict.ok else None, *metadata,
    )


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_string_list(value: Any, *, nonempty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (not nonempty or bool(value))
        and all(_is_nonempty_string(item) for item in value)
    )


def _is_filler(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().rstrip(".") in _FILLER_VALUES


def _contains_unsupported_question(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_UNSUPPORTED_QUESTION.fullmatch(value) or _BROAD_DAILY_QUESTION.search(value))
    if isinstance(value, list):
        return any(_contains_unsupported_question(item) for item in value)
    return False


def _narrative_texts(value: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Return claim-bearing text only; evidence identifiers are deliberately excluded."""
    texts: list[tuple[str, str]] = []
    for field in NARRATIVE_RESULT_FIELDS:
        item = value.get(field)
        if isinstance(item, str):
            texts.append((field, item))
        elif isinstance(item, list):
            texts.extend((field, entry) for entry in item if isinstance(entry, str))
    attempts = value.get("resolution_attempts")
    if isinstance(attempts, list):
        texts.extend(
            ("resolution_attempts.result", attempt["result"])
            for attempt in attempts
            if isinstance(attempt, Mapping) and isinstance(attempt.get("result"), str)
        )
    finance = value.get("finance_proposal")
    if isinstance(finance, Mapping):
        for finance_field in FINANCE_NARRATIVE_FIELDS:
            item = finance.get(finance_field)
            if isinstance(item, str):
                texts.append((f"finance_proposal.{finance_field}", item))
            elif isinstance(item, list):
                texts.extend(
                    (f"finance_proposal.{finance_field}", entry)
                    for entry in item if isinstance(entry, str)
                )
    return texts


def _validate_narrative_prefixes(value: Mapping[str, Any]) -> ContractValidation:
    for field, text in _narrative_texts(value):
        if (
            value.get("clarification_needed") is False
            and field in _FALSE_CLARIFICATION_EMPTY_STRING_FIELDS
            and text == ""
        ):
            continue
        expected = NARRATIVE_INTENT_PREFIXES[field]
        if re.match(rf"^{re.escape(expected)}:\s+\S", text) is None:
            return _failure("invalid_narrative_prefix", failing=(field,))
    return ContractValidation(True)


def project_canonical_narrative(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project only semantic narrative for the safety judge.

    Request/result identifiers, evidence references, policy/source references,
    tool summaries, confidence values, and provider payloads never cross this
    boundary. Values are copied unchanged; the projector does not repair text.
    """
    projected: dict[str, Any] = {}
    for field in NARRATIVE_RESULT_FIELDS:
        item = value.get(field)
        if isinstance(item, str):
            projected[field] = item
        elif isinstance(item, list):
            projected[field] = [entry for entry in item if isinstance(entry, str)]
    attempts = value.get("resolution_attempts")
    if isinstance(attempts, list):
        projected["resolution_attempt_results"] = [
            attempt["result"]
            for attempt in attempts
            if isinstance(attempt, Mapping) and isinstance(attempt.get("result"), str)
        ]
    finance = value.get("finance_proposal")
    if isinstance(finance, Mapping):
        finance_projection: dict[str, Any] = {}
        for field in FINANCE_NARRATIVE_FIELDS:
            item = finance.get(field)
            if isinstance(item, str):
                finance_projection[field] = item
            elif isinstance(item, list):
                finance_projection[field] = [
                    entry for entry in item if isinstance(entry, str)
                ]
        projected["finance_proposal"] = finance_projection
    return projected


def _semantic_clauses(text: str) -> tuple[str, ...]:
    """Split independent clauses so a safe prefix cannot exempt a later command."""
    return tuple(clause.strip() for clause in _CLAUSE_BOUNDARY.split(text) if clause.strip())


def _safe_negated_clause(clause: str) -> bool:
    """Recognize negation only when it directly governs a prohibited predicate."""
    return bool(
        _NEGATED_PROHIBITED_ACTION.search(clause)
        or _NEGATED_COMPLETION_FACT.search(clause)
    )


def _completion_predicate_text(clause: str) -> str:
    """Exclude narrowly scoped grammatical modifiers, never the whole clause.

    The original narrative still reaches the independent semantic judge. Removing
    only these modifiers leaves every other affirmative execution predicate visible.
    """
    # Missing verified authorization is an evidence gap, not an external act.
    clause = re.sub(r"\bwithout\s+verified\s+authorization\b", "without authorization evidence", clause, flags=re.IGNORECASE)
    # Fail-closed names a safety policy, not a completed close or deployment.
    clause = re.sub(r"\bfail[ -]closed\b", "deny on uncertainty", clause, flags=re.IGNORECASE)
    # A future reporting instruction is not an assertion that its condition holds.
    clause = re.sub(
        r"\bonly\s+then\s+report\s+deployment\s+as\s+complete\b",
        "report only after the required evidence", clause, flags=re.IGNORECASE,
    )
    clause = re.sub(r"\b(?:bounded\s+)?(?:Genesis\s+)?analysis\s+(?:is\s+|was\s+)?completed?\b(?=\s*(?:[.;,]|$)|\s+(?:for|on|and|but|with)\b)", "analysis", clause, flags=re.IGNORECASE)
    # A shared "no" governs a comma-separated list of bare passive phrases,
    # not a subsequent finite assertion ("payment was sent") or new sentence.
    # An "until" continuation states the gate that lifts the absence
    # ("No journals posted until step 9 CFO release gate passes"); it is
    # part of the absence statement, not a new assertion.
    absent_item = r"(?:journals?|invoices?|payments?|balances?|close|filings?|deployment)\s+(?:posted|paid|locked|approved|filed|sent|closed|verified|executed)"
    clause = re.sub(
        rf"\bno\s+{absent_item}(?:,\s*{absent_item})*(?=\s*(?:[.;,]|$|\buntil\b))",
        "absent actions", clause, flags=re.IGNORECASE,
    )
    # A "non-" prefixed participle is an explicit negation of the
    # participle ("designed as a non-executed plan"); it asserts the
    # opposite of a completion claim.
    clause = re.sub(
        r"\bnon-(?:completed?|deployed|posted|paid|filed|approved|executed|verified|settled|sent|issued|closed|finished)\b",
        "unperformed", clause, flags=re.IGNORECASE,
    )
    # Named state-machine edges describe possible states, not actual execution.
    # Only the parenthesized edge list is removed; claims outside remain visible.
    clause = re.sub(
        r"\btransitions\s*\((?:prepared|reviewed|posted|verified|approved|paid|closed)"
        r"(?:\s*(?:→|->)\s*(?:prepared|reviewed|posted|verified|approved|paid|closed))+\)",
        "transitions", clause, flags=re.IGNORECASE,
    )
    # "We approved policy" is a finite verb, not an adjective. Only mask
    # approved inside a noun phrase introduced by an explicit grammatical cue.
    approved_modifier = re.compile(
        r"\bapproved\s+(?=(?:(?!(?:and|but|then|was|is|were|has|have|had)\b)[A-Za-z][A-Za-z/-]*\s+){0,3}"
        r"(?:polic(?:y|ies)|plans?|procedures?|rules?|documents?|authorization|registry|status|state|framework|contracts?|invoices?|schedules?|workflow|baseline|ledger|set|chart|canon|journals?|entries|entry)\b)",
        re.IGNORECASE,
    )
    noun_phrase_context = re.compile(
        r"(?:^|[:;,(]\s*|\(\d+\)\s*|\b(?:supply|provide|obtain|retrieve|request|require|establish|confirm|document|current|authoritative|outside|until|no|the|an?|any|without|from|under|with|against|requires?|requiring|needs?|using|per|of|for|by|on|review|compare|inspect|previously|to|all|these|those|each|every)\s+(?:effective-dated\s+)?)$",
        re.IGNORECASE,
    )
    original = clause
    clause = approved_modifier.sub(
        lambda match: "" if (
            noun_phrase_context.search(original[:match.start()])
            or (re.search(r"\band\s+$", original[:match.start()], re.IGNORECASE)
                and re.search(r"(?:^|:\s*)(?:obtain\s+and\s+)?(?:review|request|collect|inspect)\b", original[:match.start()], re.IGNORECASE))
        ) else match.group(), clause,
    )
    # Executed SOW is a requested document modifier, not "we executed".
    original = clause
    clause = re.sub(
        r"\bexecuted\s+(?=(?:SOW|statement\s+of\s+work|contract)\b)",
        lambda match: "" if noun_phrase_context.search(original[:match.start()]) else match.group(),
        clause, flags=re.IGNORECASE,
    )
    # Generic write categories describe rollback risk, not an executed filing.
    # Match the closed noun list and modal risk predicate; finite claims outside
    # this fragment remain visible to the completion guard and semantic judge.
    clause = re.sub(
        r"(\b(?:some|certain)\s+writes\s*\(e\.g\.,?\s*journal\s+entries,\s*)filed(?=\s+documents\)\s+may\s+be\s+non-reversible\b)",
        r"\1archived", clause, flags=re.IGNORECASE,
    )
    # Shared negative auxiliary governs this coordinated declaration as well.
    # Only remove the governed infinitive; later finite claims remain visible.
    clause = re.sub(
        r"(\bdoes\s+not\s+post,\s*approve,\s*or\s+)declare\s+close\s+complete\b",
        r"\1make a completion claim", clause, flags=re.IGNORECASE,
    )
    clause = re.sub(
        r"(\bcurrent\s+state\s+of\s+each\s+close\s+entry\s*)"
        r"\(prepared\s+in\s+FinanceOS,\s*reviewed,\s*posted\s+to\s+Xero,\s*verified\s+by\s+reconciliation\s+or\s+audit\)",
        r"\1(possible evidence stages)", clause, flags=re.IGNORECASE,
    )
    # Closed vocabulary within a named lifecycle is a list of possible labels.
    # Do not erase any finite claim in or following the parentheses.
    state = r"(?:prepared|reviewed|posted|verified|approved|paid|closed|open|in-review|locked|submitted|rejected|invoiced|scheduled)"
    clause = re.sub(
        rf"(\b(?:status\s+lifecycle|close\s+(?:workflow\s+)?states?(?:\s+evidence)?|period\s+lock\s+status|payment\s+obligations)\s*)\({state}(?:\s*[,/]\s*(?:or\s+)?{state})+(?:\s+(?:evidence|flags))?\)",
        r"\1(possible labels)", clause, flags=re.IGNORECASE,
    )
    clause = re.sub(
        rf"(\bprior\s+ledger\s+state\s*)\(invoice\s+{state}(?:\s*,\s*(?:or\s+)?{state})+\)",
        r"\1(possible labels)", clause, flags=re.IGNORECASE,
    )
    clause = re.sub(
        rf"\b{state}(?:/{state})+\s+state\s+flags\b",
        "possible state flags", clause, flags=re.IGNORECASE,
    )
    clause = re.sub(
        rf"\bdistinguish\s+{state}(?:,\s*{state})*,?\s+and\s+{state}\s+states\b",
        "distinguish possible states", clause, flags=re.IGNORECASE,
    )
    clause = re.sub(
        r"\bdeployment\s+readiness\s*(?:≠|!=|does\s+not\s+equal)\s*deployed\b",
        "readiness is not execution", clause, flags=re.IGNORECASE,
    )
    clause = re.sub(
        r"(\bpayment\s+schedule\s*)\(invoiced,\s*approved,\s*committed\s+obligations\)",
        r"\1(possible obligation labels)", clause, flags=re.IGNORECASE,
    )
    clause = re.sub(
        r"\bdo\s+not\s+claim\s+(?:deployment|payment|filing|invoice)\s+(?:complete|completed|verified)\b",
        "completion claim prohibited", clause, flags=re.IGNORECASE,
    )
    original = clause
    clause = re.sub(
        r"\bdeclare\s+(?:close|deployment|payment|filing)\s+complete\b",
        lambda match: "claim a terminal state" if re.search(
            r"\bno\s+authority\s+to\b(?:(?!\b(?:but|however|instead|then)\b)[^.;!?])*\Z",
            original[:match.start()], re.IGNORECASE,
        ) else match.group(), clause, flags=re.IGNORECASE,
    )
    # A bare label serving as subject of "requires" defines evidence criteria.
    clause = re.sub(
        r"(^|[;:]\s*)(?:posted|verified|approved|paid|closed)(?=\s+requires?\b)",
        r"\1state label", clause, flags=re.IGNORECASE,
    )
    # Explicit refusal to promote a quoted status is not a completion claim.
    clause = re.sub(
        r"\bnot\s+promoted\s+to\s+['\"](?:verified|completed|approved|posted)\s+(?:deployment|payment|invoice|filing)['\"]\s+without\s+(?:external|independent)\s+evidence\b",
        "unsubstantiated status prohibited", clause, flags=re.IGNORECASE,
    )
    clause = re.sub(r"\bno\s+(?:approved|posted|paid|filed)\s+(?=(?:invoice|bill|journal|payment|filing)\b)", "no ", clause, flags=re.IGNORECASE)
    original = clause
    clause = re.sub(
        r"\bposted\s+and\s+unposted\s+(?=(?:transactions|records|entries)\b)",
        # Keep the shared object attached to the finite predicate through clause
        # splitting; otherwise "we posted" loses its later journal/invoice noun.
        lambda match: "" if noun_phrase_context.search(original[:match.start()]) else "posted/unposted ",
        clause, flags=re.IGNORECASE,
    )
    # Document descriptors do not state that a transaction happened.
    clause = re.sub(r"\bpaid-invoice(?=\s+history\b)", "invoice", clause, flags=re.IGNORECASE)
    # A filed return named as a document is a reference, not a filing
    # claim ("the tax payment amount tied to the filed return"). The
    # mask requires "filed" directly against the document name, so
    # "we filed the tax return" keeps its article and stays a claim.
    clause = re.sub(r"\bfiled\s+(?:tax\s+)?return\b", "return document", clause, flags=re.IGNORECASE)
    original = clause
    clause = re.sub(
        r"\bposted\s+(?=journal\s+count\b)",
        lambda match: "" if noun_phrase_context.search(original[:match.start()]) else match.group(),
        clause, flags=re.IGNORECASE,
    )
    clause = re.sub(r"\bcomplete(?=\s+(?:evidence|language|release\s+trigger/condition\s+documentation|(?:Xero\s+)?ledger\s+extract)\b)", "full", clause, flags=re.IGNORECASE)
    # Attributive "complete" is an adjective meaning "full" ("the
    # complete payment history", "complete invoice detail"), not a
    # completion assertion. Mask it with a determiner before it, or in
    # a noun chain after it -- but never at a clause start or after a
    # modal, where "complete" is the imperative/infinitival verb, and
    # never in "is complete", which asserts a state.
    clause = re.sub(
        r"\b(the|a|an|this|that|their|our|your|its)\s+complete\b",
        r"\1 full", clause, flags=re.IGNORECASE,
    )
    original = clause
    clause = re.sub(
        r"\bcomplete(?=\s+(?:"
        r"(?:invoice|bill|payment|journal|ledger|filing|account|period|return|deployment|release)\s+[A-Za-z]+\b"
        r"|[A-Za-z0-9]+\s+(?:documentation|details?|history|records?|extract|listing|data|set|package|files?|reports?|schedule|log|evidence|information|breakdown|summary|close|request)\b"
        r"))",
        lambda match: "complete" if re.search(
            r"(?:^|[.!?;:]\s*|\(\d+\)\s*|\b(?:must|should|shall|will|would|to)\s*)$",
            original[: match.start()], re.IGNORECASE,
        ) else "full",
        clause, flags=re.IGNORECASE,
    )
    clause = re.sub(r"\bno\s+completed\s+work\b", "no external work", clause, flags=re.IGNORECASE)
    clause = re.sub(
        r"\b(?:cannot|can't|must\s+not|should\s+not|may\s+not)\s+be\s+"
        r"(?:completed?|deployed|posted|paid|filed|approved|executed|verified|settled|sent|issued|closed|finished)\b",
        "unconfirmed action", clause, flags=re.IGNORECASE,
    )
    # Only labels in an explicit definition are masked; parenthetical contents
    # and any actual finite assertion remain subject to the normal checks.
    original = clause
    clause = re.sub(
        r"\b(?:prepared|reviewed|posted|verified)(?=\s*\()",
        lambda match: "state label" if re.search(
            r"\bclose\s+state\s+requires\s+explicit\s+distinction\s+between\b[^.;!?]*$",
            original[:match.start()], re.IGNORECASE,
        ) else match.group(), clause, flags=re.IGNORECASE,
    )
    # An unmet temporal condition is not an observation of completed work.
    clause = re.sub(
        r"(\buntil\b(?:(?!\b(?:and|or|but)\b)[^,;:.!?]){0,80}\b(?:is|are)\s+)"
        r"(?:approved|verified|posted|paid|filed|completed?)\b",
        r"\1pending", clause, flags=re.IGNORECASE,
    )
    # Future obligations are not completed external actions. Mask only the
    # participle governed by the modal, retaining every other predicate.
    clause = re.sub(
        r"\b(?:must|should|shall|will|would|could|may|might)\s+be\s+"
        r"(?:completed?|deployed|posted|paid|filed|approved|executed|verified|settled|sent|issued|closed|finished)\b",
        "future action", clause, flags=re.IGNORECASE,
    )
    # Interrogatives and an explicitly negative condition do not assert that
    # the governed passive event occurred. Other predicates remain untouched.
    clause = re.sub(
        r"(\bwhether\b(?:(?!\b(?:and|or|but)\b)[^,;:.!?]){0,100}\b(?:is|was|has\s+been|had\s+been)\s+)"
        r"(?:completed?|deployed|posted|paid|filed|approved|executed|verified|settled|sent|issued|closed|finished)\b",
        r"\1undetermined", clause, flags=re.IGNORECASE,
    )
    clause = re.sub(
        r"(\bif\s+no\s+[^,;:.!?]{1,80}\s+(?:is|are)\s+)"
        r"(?:completed?|deployed|posted|paid|filed|approved|executed|verified|settled|sent|issued|closed|finished)\b",
        r"\1absent", clause, flags=re.IGNORECASE,
    )
    # Decision questions invert modal and subject ("Should the October
    # retainer invoice be issued?"): the governed participle is posed
    # for a human decision, not asserted. Anchored to a sentence start
    # so declarative modal order (subject-modal-be, masked above) and
    # embedded claims stay untouched; an empty span is the declarative
    # order the modal rule already handles.
    clause = re.sub(
        r"((?:^|[.!?]\s+|:\s+)(?:should|must|shall|would|could|may|might|will)\s+"
        r"(?:(?!\b(?:and|or|but)\b)[^.;!?]){1,80}?\bbe\s+)"
        r"(?:completed?|deployed|posted|paid|filed|approved|executed|verified|settled|sent|issued|closed|finished)\b",
        r"\1undetermined", clause, flags=re.IGNORECASE,
    )
    # Auxiliary-initial questions ("Has the supplier invoice been
    # issued?") likewise pose the event rather than assert it. The
    # question mark must fall in the same sentence, so a declarative
    # report ("Management confirmed the invoice was issued.") keeps
    # its claim.
    clause = re.sub(
        r"((?:^|[.!?]\s+|:\s+)(?:has|have|had|did|was|were|is|are)\s+"
        r"(?:(?!\b(?:and|or|but)\b)[^.;!?]){0,80}?\b(?:been\s+)?)"
        r"(?:completed?|deployed|posted|paid|filed|approved|executed|verified|settled|sent|issued|closed|finished)\b"
        r"(?=[^.!?]*\?)",
        r"\1undetermined", clause, flags=re.IGNORECASE,
    )
    hypothetical_claim = re.compile(
        r"\bclaim\s+external\s+work(?:\s*\([^)]*\))?\s+completed\b", re.IGNORECASE,
    )
    original = clause
    clause = hypothetical_claim.sub(
        lambda match: "hypothetical claim" if re.search(
            r"\bif\s+Genesis\s+were\s+to\b(?:(?!\b(?:but|however|instead|then)\b)[^.;!?])*\Z",
            original[:match.start()], re.IGNORECASE,
        ) else match.group(), clause,
    )
    return clause


def _finance_predicate_text(clause: str) -> str:
    """Separate refund evidence nouns from instructions to refund money."""
    clause = re.sub(
        r"(\bclose\s+state\s+distinction\s*)\(prepare/review/post/verify\)",
        r"\1(possible stages)", clause, flags=re.IGNORECASE,
    )
    clause = re.sub(
        r"\brefund\b(?=\s+(?:data|details?|basis|sources?|records?|polic(?:y|ies)|evidence|terms|status)\b)",
        "reimbursement", clause, flags=re.IGNORECASE,
    )
    clause = re.sub(r"\bapprove\s+(?:data|read-only|read)\s+access\b", "authorize read access", clause, flags=re.IGNORECASE)
    clause = re.sub(
        r"\b(?:does|do|did)\s+not\s+(?:transfer|pay|post|issue|execute|run|file|submit|move|refund|approve)\b",
        "no action", clause, flags=re.IGNORECASE,
    )
    clause = re.sub(r"\bno\s+(?:allocation\s+)?proposal\s+is\s+safe\s+to\s+submit\b", "unsafe proposal", clause, flags=re.IGNORECASE)
    clause = re.sub(r"\bapprove\s+(?:(?:FinanceOS|Xero|payroll)[/\s]+){1,3}read\s+access\b", "authorize read access", clause, flags=re.IGNORECASE)
    clause = re.sub(r"(\binvoice/)transfer\b", r"\1transaction", clause, flags=re.IGNORECASE)
    clause = re.sub(r"\b(funds?\s+)transfer\s+(?=execution\b)", r"\1movement ", clause, flags=re.IGNORECASE)
    return clause


_FINANCE_VERB = r"(?:pay|post|issue|execute|run|file|submit|transfer|move|refund|approve)"
# Finance noun words. A verb-list word immediately preceded by one of these
# is the head of a noun compound ("tax fund transfer", "payroll payment
# run"), i.e. a thing-name, not an execution predicate.
_FINANCE_NOUN_WORDS = frozenset({
    "supplier", "payment", "journal", "xero", "tax", "filing", "payroll",
    "fund", "funds", "refund", "invoice", "bill", "batch", "bank",
})
# Strong immediacy turns step text into a live execution directive ("pay the
# supplier immediately", "do not wait"). It defeats framing wherever it sits
# in the clause; a bare "now" defeats framing only inside the matched
# predicate's neighborhood ("post the bill ... now").
_FINANCE_STRONG_IMMEDIACY = re.compile(
    r"\b(?:immediately|right\s+away|at\s+once|asap|without\s+delay|do\s+not\s+wait|don't\s+wait)\b",
    re.IGNORECASE,
)
_FINANCE_FRAMING_GOVERNOR = re.compile(
    r"\b(?:recommend(?:s|ed|ing|ation)?|propos(?:e|es|ed|ing)|suggest(?:s|ed|ing|ion)?)\b",
    re.IGNORECASE,
)
# Abstract nouns that turn a preceding verb-list word into a noun adjunct
# ("transfer authorization", "transfer amount"): the word names a thing,
# it does not predicate an execution.
_FINANCE_ABSTRACT_NOUNS = frozenset({
    "authorization", "amount", "timing", "deadline", "deadlines",
    "frequency", "schedule", "window", "status", "date", "total",
    "register", "order", "request", "confirmation", "readiness",
    "settlement", "requirement", "requirements",
})
# Words that make a following verb-list word a noun, never a predicate:
# determiners and possessives ("the transfer", "their payment") and the
# compound modifiers "over/under" ("over/under transfer").
_FINANCE_NOMINAL_PREV = frozenset({
    "the", "a", "an", "this", "that", "each", "every", "any",
    "over", "under", "their", "its", "our", "your",
    # Ordinal / superlative / schedule adjectives: "single largest
    # transfer", "next payment run", "scheduled filing". A comma after
    # the adjective breaks adjacency, so the sentence-initial
    # imperative "First, transfer the funds" stays genuine.
    "largest", "biggest", "smallest", "single", "first", "next",
    "last", "final", "scheduled", "planned", "pending", "upcoming",
    "recurring", "monthly", "annual", "weekly", "daily",
})
# "pay" as a compensation noun: "gross pay", "net pay", "pay period".
_PAY_NOUN_PREV = frozenset({
    "net", "gross", "base", "hourly", "regular", "overtime", "monthly",
    "annual", "weekly", "prior", "retroactive", "back", "holiday",
    "severance", "sick", "vacation", "bonus",
})
_PAY_NOUN_NEXT = frozenset({"period", "date", "cycle", "rate", "day"})
_FINANCE_SUBORDINATOR = re.compile(
    r"\b(?:before|after|until|once|when|whenever|if|unless|as\s+soon\s+as)\b",
    re.IGNORECASE,
)
_FINANCE_PROCESS_SUBJECTS = _FINANCE_NOUN_WORDS | frozenset({
    "invoices", "payments", "journals", "refunds", "bills", "filings",
    "entries", "accruals", "remittances", "settlements", "withholdings",
})


# Sentence-scoped requester attribution anchors: an instruction restated
# as the requester's direction ("the request attempts to direct Genesis
# to execute or approve ...") frames the coordinated predicates in the
# clauses that follow it within the same sentence, exactly as a
# recommendation governor does.
_FINANCE_ATTRIBUTION_ANCHOR = re.compile(
    r"\b(?:as\s+requested|per\s+(?:your|the)\s+request|you\s+asked|requester'?s\s+(?:plan|request))\b"
    r"|\b(?:the\s+)?(?:task|request|user|requester|client|prompt)\s+"
    r"(?:requests?|asks?|asked|wants?|seeks?|directs?|directed|instructs?|instructed|tells?|told"
    r"|attempts?\s+to\s+(?:direct|instruct|get))\b",
    re.IGNORECASE,
)


def _finance_subordinate_governed(predicate: str, verb_start: int) -> bool:
    """Whether the verb at ``verb_start`` is a generic process event inside
    a temporal/conditional subordinate clause ("...before supplier
    invoices post against new period", "...before funds move"): its
    immediate subject is a finance-process noun and a subordinator
    introduces the span shortly before that subject, with no clause
    punctuation in between. A fronted subordinate ("When the office
    opens, transfer the funds") ends at its comma, so the main-clause
    verb is never governed."""
    i = verb_start
    while i > 0 and predicate[i - 1] == " ":
        i -= 1
    subject = re.search(r"([A-Za-z]+)$", predicate[:i])
    if subject is None:
        return False
    subject_word = subject.group(1).lower()
    # The subject slot may instead hold a gerund governing the verb word
    # as its object ("before initiating transfer"): that use is nominal.
    if subject_word not in _FINANCE_PROCESS_SUBJECTS and not (
        len(subject_word) >= 5 and subject_word.endswith("ing")
    ):
        return False
    subject_start = i - len(subject.group(1))
    governor = None
    for candidate in _FINANCE_SUBORDINATOR.finditer(predicate, 0, subject_start):
        governor = candidate
    if governor is None:
        return False
    between = predicate[governor.end():subject_start]
    return len(between) <= 60 and not re.search(r"[,;:.!?]", between)


_FINANCE_PLAN_TOKEN = re.compile(
    r"\b(?:plan|plans|step|steps|step-by-step|checklist|runbook|procedure|sequence)\b",
    re.IGNORECASE,
)


def _finance_step_like_clause(clause: str) -> bool:
    """A clause shaped like one plan step, not a finite assertion.

    Imperative steps (optionally enumerated or ordinal-led), step-labelled
    clauses, and second-person instructions qualify. A declarative
    subject-first clause ("FinanceOS will execute the payment") never does.
    """
    if re.match(
        rf"^\s*(?:\(\d+\)\s*|\d+\s*[.)]\s*|[-•*]\s*)?"
        rf"(?:(?:first|second|third|fourth|next|finally|lastly)\s*,?\s*)?"
        rf"(?:to\s+)?{_FINANCE_VERB}\b",
        clause, re.IGNORECASE,
    ):
        return True
    if re.search(r"\bstep\s+\d+\b", clause, re.IGNORECASE):
        return True
    return bool(re.match(
        rf"^\s*you\s+(?:should\s+|need\s+to\s+|must\s+|will\s+|can\s+)?{_FINANCE_VERB}\b",
        clause, re.IGNORECASE,
    ))


def _finance_text_plan_declared(text: str) -> bool:
    """Whether a field text declares a plan structure for its steps.

    A plan must be declared with plan vocabulary (plan/steps/checklist/...)
    somewhere in the text -- two conjoined bare commands are not a plan --
    and either the text carries at least two step-like clauses or one
    clause is a bare plan header ("Recommended plan", "Close plan for
    Friday", "Proposed plan to clear the backlog"). A lone label such as
    "Proposal --" declares nothing, exactly as a safe prefix cannot exempt
    a later command.
    """
    if _FINANCE_PLAN_TOKEN.search(text) is None:
        return False
    clauses = _semantic_clauses(text)
    if sum(1 for clause in clauses if _finance_step_like_clause(clause)) >= 2:
        return True
    for clause in clauses:
        if len(clause) > 70 or _FINANCE_EXECUTION.search(_finance_predicate_text(clause)):
            continue
        token = _FINANCE_PLAN_TOKEN.search(clause)
        if token is None:
            continue
        if len(clause) - token.end() <= 15:
            return True
        if re.match(
            r"^\s*(?:the\s+|a\s+)?(?:recommended|proposed|draft)\s+[^.;:]{0,30}?"
            r"\b(?:plan|steps|checklist|runbook|procedure|sequence)\b",
            clause, re.IGNORECASE,
        ):
            return True
    return False


def _finance_clause_framed(
    field: str,
    text: str,
    clause: str,
    *,
    sentence_governor: bool = False,
    sentence_attribution: bool = False,
) -> bool:
    """Decide whether a finance verb+noun clause is framing, not assertion.

    The finance rule stops execution assertions and live execution
    directives. A clause that merely frames an action -- governed by a
    recommendation verb, posed as the subject of a requested human
    decision, or written as a step inside a declared plan -- asserts
    nothing and directs nothing immediate, so it must not trip the rule.
    Completion claims are judged by the separate completion predicate and
    are never exempted here. Immediacy always defeats framing.
    """
    predicate = _finance_predicate_text(clause)
    matches = list(_FINANCE_EXECUTION.finditer(predicate))
    if not matches:
        return True
    if _FINANCE_STRONG_IMMEDIACY.search(clause):
        return False

    def _genuine_verb_positions() -> list[int]:
        """Start offsets (in the clause predicate) of genuine verb uses.

        Phase 2 judged genuineness from the match span alone; a compound
        whose head verb started the span ("Tax transfer amount = ...")
        had no preceding word inside the span and was misjudged genuine.
        The clause is the right window. A verb-list word is not a genuine
        predicate when a finance noun word immediately precedes it in the
        clause (spaces and hyphens do not break a compound:
        "fund-transfer"), when it heads a hyphenated modifier
        ("post-9/30" is the prefix "post-", never a verb), when an
        abstract noun immediately follows it ("transfer
        authorization"), or when "pay" is a compensation noun ("gross
        pay", "pay period").
        """
        quoted_spans = [
            span.span()
            for span in re.finditer(r"'[^']*'|\"[^\"]*\"", predicate)
        ]

        def _mentioned(position: int) -> bool:
            return any(begin <= position < finish for begin, finish in quoted_spans)

        positions: list[int] = []
        for match in matches:
            if (
                match.end() < len(predicate)
                and predicate[match.end()] == "-"
                and match.end() + 1 < len(predicate)
                and predicate[match.end() + 1].isalnum()
            ):
                # The match's final token continues into a hyphenated
                # compound ("submit for payment-run sequencing"): the
                # noun completing the match is a modifier inside a
                # process name, not the verb's object.
                continue
            for verb_match in re.finditer(rf"\b{_FINANCE_VERB}\b", match.group(), re.IGNORECASE):
                start = match.start() + verb_match.start()
                end = match.start() + verb_match.end()
                if _mentioned(start):
                    # A verb inside quotes is mentioned, not used:
                    # "Directive framing ('issue', 'post', 'run',
                    # 'transfer') assumes ..." talks about the words.
                    continue
                tokens = predicate[:start].rstrip().rsplit(None, 1)
                if tokens and "$" in tokens[-1]:
                    # A money amount directly before the verb word makes
                    # it a noun: "the $6,200 transfer". (A bare numeral
                    # does not: "in 2026 transfer the funds" stays a
                    # directive.)
                    continue
                if (
                    end < len(predicate)
                    and predicate[end] == "-"
                    and end + 1 < len(predicate)
                    and predicate[end + 1].isalnum()
                ):
                    continue
                if predicate[end:end + 1] == "(" or predicate[end:end + 2] == " (":
                    # A verb word directly glossed by a parenthetical is
                    # a category noun: "each material transfer (tax
                    # fund, supplier payment run, payroll funding)".
                    continue
                i = start
                while i > 0 and predicate[i - 1] in " -/":
                    i -= 1
                prev = re.search(r"([A-Za-z]+)$", predicate[:i])
                prev_word = prev.group(1).lower() if prev is not None else None
                if prev_word in _FINANCE_NOUN_WORDS:
                    continue
                if prev_word in _FINANCE_NOMINAL_PREV:
                    continue
                j = end
                while j < len(predicate) and predicate[j] in " -/":
                    j += 1
                nxt = re.match(r"([A-Za-z]+)", predicate[j:])
                next_word = nxt.group(1).lower() if nxt is not None else None
                if next_word in {"is", "was", "are", "were", "be", "been"}:
                    # A verb word directly followed by a copula heads
                    # the clause's subject noun phrase ("If transfer is
                    # authorized before payroll ..."): the transfer is
                    # the thing being discussed, not an act performed.
                    continue
                if next_word in _FINANCE_ABSTRACT_NOUNS or (
                    next_word is not None
                    and next_word.rstrip("s") in _FINANCE_ABSTRACT_NOUNS
                ):
                    continue
                if (
                    "/" in predicate[end:j]
                    and next_word in _FINANCE_NOUN_WORDS
                ):
                    # First element of a slash compound label
                    # ("transfer/payment sequencing"): a thing-name.
                    continue
                if verb_match.group().lower() == "pay" and (
                    prev_word in _PAY_NOUN_PREV or next_word in _PAY_NOUN_NEXT
                ):
                    continue
                positions.append(start)
        return positions

    genuine_positions = _genuine_verb_positions()

    # If every match rests only on noun-compound heads ("...invoice
    # issuance, journal entries, payroll processing, tax fund transfer"),
    # the clause names finance actions; it neither asserts nor directs
    # their execution, so there is no execution predicate to reject.
    if not genuine_positions:
        return True
    for match in matches:
        window = predicate[max(0, match.start() - 30): match.end() + 30]
        if re.search(r"\bnow\b", window, re.IGNORECASE):
            return False

    # A recommendation governor earlier in the same sentence governs its
    # conjoined verb phrases ("recommend that you issue the invoice and
    # post the journal entries"): the clause splitter separates conjuncts,
    # but the governor's scope is the whole sentence. The caller computes
    # this from the field text; immediacy above has already defeated it.
    if sentence_governor:
        return True

    # An attributed instruction earlier in the same sentence frames its
    # coordinated predicates ("the request attempts to direct Genesis to
    # execute or approve ..."): the clause restates what was asked, it
    # does not direct it. The caller computes this from the field text.
    if sentence_attribution:
        return True

    # The subject of a requested human decision is posed, not asserted: the
    # field exists so a human decides whether the action happens at all.
    if field == "requested_human_decisions":
        if re.match(rf"^\s*{_FINANCE_VERB}\b", clause, re.IGNORECASE):
            return True
        if re.search(rf"\bwhether\s+(?:to\s+)?{_FINANCE_VERB}\b", clause, re.IGNORECASE):
            return True

    first_verb = re.search(rf"\b{_FINANCE_VERB}\b", clause, re.IGNORECASE)
    if first_verb is not None:
        # A recommendation/proposal governor ahead of the finance verb
        # frames the predicate ("recommend that FinanceOS post the journal").
        governor = _FINANCE_FRAMING_GOVERNOR.search(clause)
        if governor is not None and governor.start() < first_verb.start():
            return True
        # An infinitive governed by a plan noun is a stated intention, not
        # an assertion ("the plan is to issue the invoice").
        if re.search(
            rf"\b(?:plan|steps?|proposal|intent|intention)\b[^.;:]{{0,40}}?\bto\s+{_FINANCE_VERB}\b",
            clause, re.IGNORECASE,
        ):
            return True
        # An attributed instruction restates the requester's direction --
        # either an explicit attribution phrase, or the clause-initial
        # subject-request form ("Task requests a step-by-step plan for
        # four finance actions (...)"), which describes the ask rather
        # than directing anyone. Anchored to the clause start so a
        # mid-clause "requests" noun cannot launder a directive list.
        attribution = re.search(
            r"\b(?:as\s+requested|per\s+(?:your|the)\s+request|you\s+asked|requester'?s\s+(?:plan|request))\b"
            r"|^\s*(?:the\s+)?(?:task|request|user|requester|client|prompt)\s+(?:requests?|asks?|asked|wants?|seeks?)\b",
            clause, re.IGNORECASE,
        )
        if attribution is not None and attribution.start() < first_verb.start():
            return True

    # Generic process events inside temporal/conditional subordinate
    # clauses state sequencing conditions or workflow mechanics, not
    # executed acts or live directives. Every genuine verb must be so
    # governed, so a main-clause directive ("Post the journal entries
    # before ...") defeats the route.
    if all(
        _finance_subordinate_governed(predicate, position)
        for position in genuine_positions
    ):
        return True

    # An embedded authority question ("who can approve their own
    # accruals, who must review payroll ...") maps who holds authority;
    # it poses the action for a control matrix and directs nothing.
    # Every genuine verb must sit in the interrogative-modal frame, so
    # a capability assertion about a named actor ("the clerk can
    # approve ...") is not exempted.
    if all(
        re.search(
            r"\b(?:who|what|which|whose)\s+(?:can|could|must|should|may|might)\s+(?:also\s+)?$",
            predicate[:position],
            re.IGNORECASE,
        )
        for position in genuine_positions
    ):
        return True

    # One item of an enumerated plan, detached from the list head by
    # clause splitting ("...(3) submit payroll register for approval,
    # (4) calculate ..."): the verb directly follows its "(N)" marker,
    # the clause carries at least two markers, and the field text
    # declares the plan structure that frames the list.
    if (
        len(re.findall(r"\(\d+\)", clause)) >= 2
        and _FINANCE_PLAN_TOKEN.search(text) is not None
        and all(
            re.search(r"\(\d+\)\s*$", predicate[:position])
            for position in genuine_positions
        )
    ):
        return True

    # A step of an enumerated plan or scenario whose "(N)" markers live
    # in the field text while clause splitting detached this step into
    # its own clause ("... then run supplier payments if liquidity
    # remains"): the enumeration is the declaration, and the clause
    # itself must be step-like.
    if (
        len(re.findall(r"\(\d+\)", text)) >= 2
        and (
            _FINANCE_PLAN_TOKEN.search(text) is not None
            or re.search(r"\bscenarios?\b", text, re.IGNORECASE) is not None
        )
        and _finance_step_like_clause(clause)
    ):
        return True

    # A step whose stated purpose is routing work to a human decision
    # ("Submit for controller approval before Xero posting") executes
    # nothing itself: submission-for-approval IS the control. The clause
    # must be step-like and name the decision it routes to, so an
    # execution routing ("Submit the payment for processing") stays
    # genuine.
    if _finance_step_like_clause(clause) and re.search(
        r"\bfor\s+(?:\w+\s+){0,2}?(?:approval|sign[ -]?off|authori[sz]ation|review)\b",
        clause, re.IGNORECASE,
    ):
        return True

    # A declared plan structure frames its own steps; the clause itself must
    # be step-like so a declarative assertion elsewhere in the text is not
    # laundered by a plan mention. A clause that labels itself a step
    # ("Execute Step 1 (...) after gate approval") declares its own plan
    # context: each recommended_next_actions entry is its own text, so a
    # lone self-labelled step cannot borrow a second clause to reach the
    # two-step declaration threshold.
    return (
        _finance_text_plan_declared(text)
        or re.search(r"\bstep\s+\d+\b", clause, re.IGNORECASE) is not None
    ) and _finance_step_like_clause(clause)


def _negated_action_list_text(text: str) -> str:
    """Remove only verbs governed by explicit negation in a coordinated list.

    New finite assertions, contrast words, and sentence boundaries are retained.
    The original candidate still reaches the independent semantic judge.
    """
    text = re.sub(
        r"\bno\s+transfer(?:,\s*(?:or\s+)?(?:payment|escrow|filing|posting))+\s+occurs\s+outside\b",
        "no financial event occurs outside", text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bno\s+record\s+of\s+(?:(?!\b(?:and|but|however|then)\b)[^.;!?]){1,100}"
        r"\bshould\s+persist\s+in\s+(?:private|durable)\s+memory\b",
        "private retention prohibited", text, flags=re.IGNORECASE,
    )
    verbs = r"(?:self-)?(?:claim|mutate|mark|propose|process|override|forgive|authorize|alter|bypass|approve|pay|post|issue|execute|run|file|submit|transfer|move|refund|message|email|contact|store|save|retain|persist|record|write)"
    span = re.compile(
        rf"\b(?:cannot|can't|must\s+not|may\s+not|does\s+not|do\s+not|shall\s+not)\s+{verbs}\b"
        r"(?:(?!\b(?:but|however|instead|then)\b)[^.;:!?\n])*", re.IGNORECASE,
    )
    def mask(match):
        segment = match.group()
        segment = re.sub(rf"(\b(?:cannot|can't|not)\s+){verbs}\b", r"\1refrain", segment, count=1, flags=re.IGNORECASE)
        return re.sub(rf"((?:,\s*(?:or\s+)?|\bor\s+)){verbs}\b", r"\1refrain", segment, flags=re.IGNORECASE)
    return span.sub(mask, text)


def _sentence_governor_flags(clauses: list[str], masked_text: str) -> list[bool]:
    """Per-clause flags: a recommendation governor appeared earlier in the
    same sentence. The clause splitter separates conjoined verb phrases,
    but a governor's scope is its sentence ("recommend that you issue the
    invoice and post the journal entries"). Computed by replaying the
    splitter per sentence segment; segment boundaries are a subset of
    clause boundaries, so the segment clause lists concatenate in the
    same order as the whole-text list. Any misalignment fails closed
    (all flags False = pre-phase-2 behavior)."""
    flags: list[bool] = []
    for segment in re.split(r"(?<=[.!?;\n])\s+", masked_text):
        governor_seen = False
        for clause in _semantic_clauses(segment):
            flags.append(governor_seen)
            governor = _FINANCE_FRAMING_GOVERNOR.search(clause)
            if governor is not None:
                verb = re.search(rf"\b{_FINANCE_VERB}\b", clause, re.IGNORECASE)
                if verb is None or governor.start() < verb.start():
                    governor_seen = True
    if len(flags) != len(clauses):
        return [False] * len(clauses)
    return flags


def _sentence_attribution_flags(clauses: list[str], masked_text: str) -> list[bool]:
    """Per-clause flags: a requester-attribution anchor appeared earlier
    in the same sentence. Mirrors _sentence_governor_flags: the clause
    splitter separates coordinated infinitives ("... to execute or
    approve ..."), but the attribution's scope is the whole sentence.
    Any misalignment fails closed (all flags False)."""
    flags: list[bool] = []
    for segment in re.split(r"(?<=[.!?;\n])\s+", masked_text):
        attribution_seen = False
        for clause in _semantic_clauses(segment):
            flags.append(attribution_seen)
            anchor = _FINANCE_ATTRIBUTION_ANCHOR.search(clause)
            if anchor is not None:
                verb = re.search(rf"\b{_FINANCE_VERB}\b", clause, re.IGNORECASE)
                if verb is None or anchor.start() < verb.start():
                    attribution_seen = True
    if len(flags) != len(clauses):
        return [False] * len(clauses)
    return flags


def _validate_semantics(slug: str, value: Mapping[str, Any]) -> ContractValidation:
    texts = _narrative_texts(value)
    proposal_fields = {"proposals", "finance_proposal.recommended_financeos_action"}
    for field, text in texts:
        masked_text = _negated_action_list_text(text)
        field_clauses = _semantic_clauses(masked_text)
        governor_flags = (
            _sentence_governor_flags(field_clauses, masked_text)
            if slug in FINANCE_AGENT_IDS
            else [False] * len(field_clauses)
        )
        attribution_flags = (
            _sentence_attribution_flags(field_clauses, masked_text)
            if slug in FINANCE_AGENT_IDS
            else [False] * len(field_clauses)
        )
        for clause_index, clause in enumerate(field_clauses):
            if _safe_negated_clause(clause):
                continue
            if _BROAD_DAILY_QUESTION.search(clause):
                return _failure("unsupported_human_question", failing=(field,))
            # FinanceOS Worker is the named execution service, not a person.
            contact_clause = re.sub(r"\bFinanceOS\s+Worker\b", "FinanceOS service", clause, flags=re.IGNORECASE)
            if _DIRECT_CONTACT.search(contact_clause):
                return _failure("direct_human_contact", failing=(field,))
            memory_clause = clause
            if slug == "genesis-automation-architect" and field == "proposals" and not re.search(r"\b(?:private|internal)\b", clause, re.IGNORECASE):
                # Architecture proposals may specify the shared control-plane
                # ledger; this is not private agent memory or a runtime write.
                memory_clause = re.sub(
                    r"^store\s+in\s+durable\s+control-plane\s+ledger\b",
                    "propose shared control-plane storage", clause, flags=re.IGNORECASE,
                )
            if _PRIVATE_MEMORY.search(memory_clause):
                return _failure("private_durable_memory", failing=(field,))
            if slug in FINANCE_AGENT_IDS and _FINANCE_EXECUTION.search(_finance_predicate_text(clause)):
                if field not in proposal_fields or not _SAFE_FINANCE_PROPOSAL.fullmatch(clause):
                    if not _finance_clause_framed(
                        field,
                        text,
                        clause,
                        sentence_governor=governor_flags[clause_index],
                        sentence_attribution=attribution_flags[clause_index],
                    ):
                        return _failure("finance_execution_claim", failing=(field,))
        for clause in _semantic_clauses(_completion_predicate_text(text)):
            if not _safe_negated_clause(clause) and _REAL_WORLD_COMPLETION.search(clause):
                return _failure("real_world_completion_claim", failing=(field,))
    return ContractValidation(True)


def _validate_finance_proposal(value: Any) -> ContractValidation:
    if not isinstance(value, Mapping):
        return _failure("invalid_finance_field_type")
    missing = tuple(field for field in FINANCE_PROPOSAL_FIELDS if field not in value)
    if missing:
        return _failure("missing_finance_fields", missing)
    if set(value) != set(FINANCE_PROPOSAL_FIELDS):
        return _failure("unexpected_finance_fields")

    nullable_strings = {
        "entity_id",
        "period",
        "transaction_or_schedule_type",
        "proposed_treatment",
        "account_mapping",
        "tracking_mapping",
        "tax_treatment",
        "amount_logic",
        "recommended_financeos_action",
    }
    for field in nullable_strings:
        item = value[field]
        if item is not None and not _is_nonempty_string(item):
            return _failure("invalid_finance_field_type")
    for field in ("source_refs", "policy_refs", "assumptions", "exceptions"):
        if not _is_string_list(value[field]):
            return _failure("invalid_finance_field_type")
    confidence = value["confidence"]
    if confidence is not None and not _is_number(confidence):
        return _failure("invalid_finance_field_type")
    if confidence is not None and not 0 <= confidence <= 1:
        return _failure("invalid_finance_field_range")
    if value["requires_human_decision"] is not None and not isinstance(
        value["requires_human_decision"], bool
    ):
        return _failure("invalid_finance_field_type")
    return ContractValidation(True)


def _validate_clarification(value: Mapping[str, Any]) -> ContractValidation:
    needed = value.get("clarification_needed", False)
    if not isinstance(needed, bool):
        return _failure("invalid_clarification_type")

    if not needed:
        list_fields = (
            "resolution_attempts",
            "supporting_evidence_refs",
            "competing_state_refs",
            "recommended_answer_options",
        )
        if any(
            value.get(field) not in (None, "")
            for field in _FALSE_CLARIFICATION_EMPTY_STRING_FIELDS
        ):
            return _failure("unexpected_clarification_value")
        if value.get("materiality") is not None:
            return _failure("unexpected_clarification_value")
        if any(value.get(field) not in (None, []) for field in list_fields):
            return _failure("unexpected_clarification_value")
        if _contains_unsupported_question(value.get("requested_human_decisions", [])):
            return _failure("unsupported_human_question")
        return ContractValidation(True)

    required = RESULT_CLARIFICATION_FIELDS[1:]
    missing = tuple(field for field in required if field not in value)
    if missing:
        return _failure("missing_clarification_fields", missing)

    for field in ("candidate_resolution", "reason_human_needed", "recommended_human_resolver"):
        item = value[field]
        if not _is_nonempty_string(item) or _is_filler(item):
            return _failure("invalid_clarification_value")
        if _contains_unsupported_question(item):
            return _failure("unsupported_human_question")

    materiality = value["materiality"]
    if not _is_number(materiality) or not 0 <= materiality <= 1:
        return _failure("invalid_clarification_value")
    if value["urgency"] not in CLARIFICATION_URGENCIES:
        return _failure("invalid_clarification_value")

    attempts = value["resolution_attempts"]
    if not isinstance(attempts, list) or not attempts:
        return _failure("invalid_clarification_value")
    expected_attempt_fields = {"source_or_capability", "result", "timestamp"}
    for attempt in attempts:
        if not isinstance(attempt, Mapping) or set(attempt) != expected_attempt_fields:
            return _failure("invalid_clarification_value")
        if not all(_is_nonempty_string(attempt[field]) and not _is_filler(attempt[field]) for field in expected_attempt_fields):
            return _failure("invalid_clarification_value")
        try:
            datetime.fromisoformat(str(attempt["timestamp"]).replace("Z", "+00:00"))
        except ValueError:
            return _failure("invalid_clarification_value")
    independent_sources = {
        str(attempt["source_or_capability"]).strip().casefold() for attempt in attempts
    }
    if len(independent_sources) < 2:
        return _failure("insufficient_resolution_attempts")

    if not _is_string_list(value["supporting_evidence_refs"], nonempty=True):
        return _failure("invalid_clarification_value")
    if not _is_string_list(value["competing_state_refs"]):
        return _failure("invalid_clarification_value")
    options = value["recommended_answer_options"]
    if (
        not _is_string_list(options, nonempty=True)
        or len(set(options)) != len(options)
        or any(option not in CLARIFICATION_ANSWER_OPTIONS for option in options)
    ):
        return _failure("invalid_clarification_value")
    if _contains_unsupported_question(value.get("requested_human_decisions", [])):
        return _failure("unsupported_human_question")
    return ContractValidation(True)


def validate_canonical_result_structure(slug: str, value: Any) -> ContractValidation:
    """Validate envelope-independent shape, status, source exhaustion, and prefixes."""
    if slug not in CANONICAL_AGENT_IDS:
        return _failure("unknown_agent_id")
    if not isinstance(value, Mapping):
        return _failure("response_not_json_object")
    missing = tuple(field for field in COMMON_RESULT_FIELDS if field not in value)
    if missing:
        return _failure("missing_required_keys", missing)

    allowed = set(COMMON_RESULT_FIELDS) | set(RESULT_CLARIFICATION_FIELDS)
    if slug in FINANCE_AGENT_IDS:
        allowed.add("finance_proposal")
    unexpected = tuple(sorted(set(value) - allowed))
    if unexpected:
        return _failure("unexpected_fields", unexpected)

    if value["agent_id"] != slug:
        return _failure("agent_id_mismatch")
    for field in ("agent_id", "task_id", "scope", "summary"):
        if not _is_nonempty_string(value[field]):
            return _failure("invalid_field_type")
    if value["status"] not in CANONICAL_STATUSES:
        return _failure("invalid_status")
    for field in STRING_LIST_FIELDS:
        if not _is_string_list(value[field]):
            return _failure("invalid_field_value")
    confidence = value["confidence"]
    if not _is_number(confidence):
        return _failure("invalid_field_type")
    if not 0 <= confidence <= 1:
        return _failure("invalid_field_range")

    clarification = _validate_clarification(value)
    if not clarification.ok:
        return clarification
    if slug in FINANCE_AGENT_IDS:
        if "finance_proposal" not in value:
            return _failure("missing_finance_fields", ("finance_proposal",))
        
        fp = value["finance_proposal"]
        if not isinstance(fp, dict):
            return _failure("invalid_finance_field_type")
        
        # Representation-only normalization: provider tool output may omit nullable
        # finance_proposal members even though the canonical public shape requires
        # all fifteen keys. Missing nullable scalars become None and missing list
        # members become []; no business fact is synthesized. Non-empty values,
        # unexpected keys, identifiers, amounts, treatments, approvals, and evidence
        # remain subject to the same fail-closed validation below.
        for field in FINANCE_PROPOSAL_FIELDS:
            if field not in fp:
                # Determine safe default based on field type
                if field in ("source_refs", "policy_refs", "assumptions", "exceptions"):
                    fp[field] = []  # Empty list for list fields
                else:
                    fp[field] = None  # Null for scalar fields (strings, numbers, booleans)
        
        # Normalize empty strings to null for nullable string fields
        # This handles LLM-generated empty strings that pass provider schema
        # validation but would fail local validation (empty string is not a valid
        # business value, but null is valid for "not applicable")
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
    return _validate_narrative_prefixes(value)


def validate_canonical_result_semantics(slug: str, value: Any) -> ContractValidation:
    """Apply the deterministic narrative firewall without repairing supplied text."""
    if slug not in CANONICAL_AGENT_IDS:
        return _failure("unknown_agent_id")
    if not isinstance(value, Mapping):
        return _failure("response_not_json_object")
    return _validate_semantics(slug, value)


def validate_canonical_result(slug: str, value: Any) -> ContractValidation:
    """Compose structural and deterministic semantic validation for offline callers."""
    structural = validate_canonical_result_structure(slug, value)
    if not structural.ok:
        return structural
    return validate_canonical_result_semantics(slug, value)


def parse_and_validate_canonical_result(slug: str, content: Any) -> ParsedCanonicalResult:
    """Normalize one complete JSON fence, parse an object, then validate it."""
    candidate = content if isinstance(content, str) else ""
    stripped = candidate.strip()
    normalized_fence = False
    lines = stripped.splitlines()
    if len(lines) >= 3 and lines[0].strip().lower() in {"```json", "```"} and lines[-1].strip() == "```":
        body = "\n".join(lines[1:-1]).strip()
        if "```" not in body:
            stripped = body
            normalized_fence = True
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    def reject_nonfinite(_value: str) -> None:
        raise ValueError("non-finite number")

    try:
        parsed = json.loads(
            stripped,
            object_pairs_hook=unique_object,
            parse_constant=reject_nonfinite,
        )
    except (TypeError, ValueError):
        return ParsedCanonicalResult(False, "response_not_json_object", (), normalized_fence, None)
    if not isinstance(parsed, dict):
        return ParsedCanonicalResult(False, "response_not_json_object", (), normalized_fence, None)
    verdict = validate_canonical_result(slug, parsed)
    return ParsedCanonicalResult(
        verdict.ok,
        verdict.reason,
        verdict.missing_fields,
        normalized_fence,
        parsed,
    )


def output_contract_descriptor(*, finance: bool) -> dict[str, Any]:
    """Return a stable capability-card projection of the conditional contract."""
    return {
        "version": CANONICAL_RESULT_CONTRACT_VERSION,
        "allowed_statuses": list(CANONICAL_STATUSES),
        "required_fields": list(COMMON_RESULT_FIELDS),
        "finance_proposal_required": finance,
        "finance_proposal_fields": list(FINANCE_PROPOSAL_FIELDS) if finance else [],
        "clarification": {
            "required_when": "clarification_needed=true",
            "fields": list(RESULT_CLARIFICATION_FIELDS),
            "urgencies": list(CLARIFICATION_URGENCIES),
            "answer_options": list(CLARIFICATION_ANSWER_OPTIONS),
            "ordinary_result_details": "absent_or_null",
            "source_exhaustion_required": True,
            "minimum_independent_attempts": 2,
        },
    }
