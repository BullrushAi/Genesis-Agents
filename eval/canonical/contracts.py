"""Canonical, side-effect-free forced-tool contracts for local evaluation experiments."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from canonical_result_contract import (
    CANONICAL_AGENT_IDS,
    CANONICAL_RESULT_CONTRACT_VERSION,
    CANONICAL_STATUSES,
    CLARIFICATION_ANSWER_OPTIONS,
    CLARIFICATION_FIELDS,
    CLARIFICATION_URGENCIES,
    COMMON_RESULT_FIELDS,
    FINANCE_AGENT_IDS,
    FINANCE_NARRATIVE_FIELDS,
    FINANCE_PROPOSAL_FIELDS,
    NARRATIVE_INTENT_PREFIXES,
    NARRATIVE_RESULT_FIELDS,
    CANONICAL_RESULT_TOOL_NAME,
    canonical_provider_result_json_schema,
    canonical_result_json_schema,
    project_provider_canonical_result_with_metadata,
    validate_canonical_result,
)
from runtime.semantic_safety_judge import (
    JUDGE_TOOL_NAME,
    POLICY_VERSION,
    judge_tool_schema,
    role_contract_metadata,
)
from runtime.anthropic_wire import EXACT_FORCED_TOOL_RESPONSE_ERROR_CODES

CONTENT_FREE_CANDIDATE_FAILURE_FIELDS = frozenset({
    "genesis.llm.envelope_reason",
    "genesis.llm.transport_stage",
    "genesis.llm.transport_elapsed_ms",
    "genesis.llm.http_status_class",
})


JUDGE_QUALITIES = (
    "correctness",
    "faithfulness",
    "hallucination",
    "relevance",
    "tool_use",
    "conciseness",
    "refusal",
    "instruction_adherence",
)


class ContractViolation(ValueError):
    """A deterministic provider-output contract violation."""

    def __init__(
        self, code: str, *, representation_subtype: str | None = None,
        representation_missing_fields: tuple[str, ...] = (),
        representation_extra_fields: tuple[str, ...] = (),
        representation_missing_field_count: int = 0,
        representation_extra_field_count: int = 0,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.representation_subtype = representation_subtype
        self.representation_missing_fields = representation_missing_fields
        self.representation_extra_fields = representation_extra_fields
        self.representation_missing_field_count = representation_missing_field_count
        self.representation_extra_field_count = representation_extra_field_count


@dataclass(frozen=True)
class OutputContract:
    contract_id: str
    schema_version: int
    tool_name: str
    description: str
    json_schema: dict[str, Any]
    canonical_slug: str | None = None

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            {
                "contract_id": self.contract_id,
                "schema_version": self.schema_version,
                "tool_name": self.tool_name,
                "json_schema": self.json_schema,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def provider_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.tool_name,
                "description": self.description,
                "parameters": self.json_schema,
                "strict": True,
            },
        }

    def validate_with_metadata(self, value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        metadata: dict[str, Any] = {
            "representation_subtype": None,
            "representation_missing_fields": (),
            "representation_extra_fields": (),
            "representation_missing_field_count": 0,
            "representation_extra_field_count": 0,
        }
        validation_schema = self.json_schema
        if self.canonical_slug is not None:
            projection = project_provider_canonical_result_with_metadata(
                self.canonical_slug, value,
            )
            metadata = {
                "representation_subtype": projection.subtype,
                "representation_missing_fields": projection.missing_fields,
                "representation_extra_fields": projection.extra_fields,
                "representation_missing_field_count": projection.missing_field_count,
                "representation_extra_field_count": projection.extra_field_count,
            }
            if not projection.ok or projection.value is None:
                raise ContractViolation("submit_arguments_invalid", **metadata)
            value = projection.value
            validation_schema = canonical_result_json_schema(self.canonical_slug)
        errors = sorted(Draft202012Validator(validation_schema).iter_errors(value), key=lambda row: list(row.path))
        if errors:
            raise ContractViolation("submit_arguments_invalid", **metadata)
        if not isinstance(value, dict):  # Defensive; the schema already enforces this.
            raise ContractViolation("submit_arguments_invalid", **metadata)
        semantic_slug = self.canonical_slug or value.get("agent_id")
        if self.tool_name == CANONICAL_RESULT_TOOL_NAME and semantic_slug in CANONICAL_AGENT_IDS:
            if not validate_canonical_result(semantic_slug, value).ok:
                raise ContractViolation("submit_arguments_invalid", **metadata)
        return value, metadata

    def validate(self, value: Any) -> dict[str, Any]:
        return self.validate_with_metadata(value)[0]


def _intent_description(field: str) -> str:
    prefix = NARRATIVE_INTENT_PREFIXES[field]
    return (
        f"Every supplied value begins exactly with `{prefix}: `. The prefix classifies "
        "intent only and never attests semantic safety."
    )


def _intent_string(field: str) -> dict[str, Any]:
    prefix = NARRATIVE_INTENT_PREFIXES[field]
    return {
        "type": "string", "minLength": len(prefix) + 3,
        "pattern": rf"^{prefix}:\s+\S", "description": _intent_description(field),
    }


def _string_array(field: str | None = None) -> dict[str, Any]:
    item = _intent_string(field) if field is not None else {"type": "string", "minLength": 1}
    return {"type": "array", "items": item}


def _nullable(inner: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [inner, {"type": "null"}]}


def _finance_proposal_schema() -> dict[str, Any]:
    nullable_string = _nullable({"type": "string", "minLength": 1})
    properties: dict[str, Any] = {
        name: nullable_string
        for name in ("entity_id", "period")
    }
    properties.update({
        name: _nullable(_intent_string(f"finance_proposal.{name}"))
        for name in FINANCE_NARRATIVE_FIELDS
        if name not in {"assumptions", "exceptions"}
    })
    properties.update({name: _string_array() for name in ("source_refs", "policy_refs")})
    properties["assumptions"] = _string_array("finance_proposal.assumptions")
    properties["exceptions"] = _string_array("finance_proposal.exceptions")
    properties["confidence"] = _nullable({"type": "number", "minimum": 0, "maximum": 1})
    properties["requires_human_decision"] = _nullable({"type": "boolean"})
    return {
        "type": "object",
        "properties": properties,
        "required": list(FINANCE_PROPOSAL_FIELDS),
        "additionalProperties": False,
    }


def genesis_output_schema(slug: str | None = None) -> dict[str, Any]:
    if slug is not None and slug not in CANONICAL_AGENT_IDS:
        raise ValueError("unknown canonical Genesis slug")
    string_arrays = {
        name: _string_array(name if name in NARRATIVE_RESULT_FIELDS else None)
        for name in (
            "findings", "proposals", "evidence_refs", "assumptions", "conflicts",
            "unknowns", "risk_flags", "recommended_next_actions",
            "requested_human_decisions", "tool_calls_summary",
        )
    }
    properties: dict[str, Any] = {
        "agent_id": {"const": slug} if slug is not None else {"enum": sorted(CANONICAL_AGENT_IDS)},
        "task_id": {"type": "string", "minLength": 1},
        "scope": {"type": "string", "minLength": 1},
        "status": {"enum": list(CANONICAL_STATUSES)},
        "summary": _intent_string("summary"),
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        **string_arrays,
        "clarification_needed": {"type": "boolean"},
        "candidate_resolution": _nullable(_intent_string("candidate_resolution")),
        "reason_human_needed": _nullable(_intent_string("reason_human_needed")),
        "recommended_human_resolver": _nullable(_intent_string("recommended_human_resolver")),
        "materiality": _nullable({"type": "number", "minimum": 0, "maximum": 1}),
        "urgency": {"enum": [*CLARIFICATION_URGENCIES, None]},
        "resolution_attempts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_or_capability": {"type": "string", "minLength": 1},
                    "result": _intent_string("resolution_attempts.result"),
                    "timestamp": {"type": "string", "format": "date-time"},
                },
                "required": ["source_or_capability", "result", "timestamp"],
                "additionalProperties": False,
            },
        },
        "supporting_evidence_refs": _string_array(),
        "competing_state_refs": _string_array(),
        "recommended_answer_options": {
            "type": "array",
            "items": {"enum": list(CLARIFICATION_ANSWER_OPTIONS)},
            "uniqueItems": True,
        },
    }
    required = list(COMMON_RESULT_FIELDS)
    if slug in FINANCE_AGENT_IDS:
        properties["finance_proposal"] = _finance_proposal_schema()
        required.append("finance_proposal")
    elif slug is None:
        properties["finance_proposal"] = _finance_proposal_schema()
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "required": sorted(required),
        "additionalProperties": False,
        "allOf": [
            {
                "if": {
                    "properties": {"clarification_needed": {"const": True}},
                    "required": ["clarification_needed"],
                },
                "then": {
                    "required": list(CLARIFICATION_FIELDS),
                    "properties": {
                        "candidate_resolution": _intent_string("candidate_resolution"),
                        "reason_human_needed": _intent_string("reason_human_needed"),
                        "recommended_human_resolver": _intent_string("recommended_human_resolver"),
                        "materiality": {"type": "number", "minimum": 0, "maximum": 1},
                        "urgency": {"enum": list(CLARIFICATION_URGENCIES)},
                        "resolution_attempts": {"minItems": 1},
                        "supporting_evidence_refs": {"minItems": 1},
                        "recommended_answer_options": {"minItems": 1},
                    },
                },
            }
        ],
    }
    Draft202012Validator.check_schema(schema)
    return schema


def semantic_safety_judge_contract() -> OutputContract:
    """local evaluation mirror of the production forced semantic-judgment envelope."""
    return OutputContract(
        contract_id=POLICY_VERSION,
        schema_version=1,
        tool_name=JUDGE_TOOL_NAME,
        description="Non-executing, exact-model Genesis semantic safety judgment.",
        json_schema=judge_tool_schema(),
    )


def judge_results_schema() -> dict[str, Any]:
    row_schema = {
        "type": "object",
        "properties": {
            "name": {"enum": list(JUDGE_QUALITIES)},
            "score": {"type": "number", "minimum": 0, "maximum": 1},
            "label": {"enum": ["PASS", "FAIL"]},
            "explanation": {"type": "string", "minLength": 1},
        },
        "required": ["name", "score", "label", "explanation"],
        "additionalProperties": False,
    }
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": row_schema,
                "minItems": len(JUDGE_QUALITIES),
                "maxItems": len(JUDGE_QUALITIES),
                "allOf": [
                    {
                        "contains": {
                            "type": "object",
                            "properties": {"name": {"const": name}},
                            "required": ["name"],
                        },
                        "minContains": 1,
                        "maxContains": 1,
                    }
                    for name in JUDGE_QUALITIES
                ],
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }
    Draft202012Validator.check_schema(schema)
    return schema


def genesis_output_contract(slug: str) -> OutputContract:
    return OutputContract(
        contract_id=f"genesis_output:{CANONICAL_RESULT_CONTRACT_VERSION}:{slug}",
        schema_version=5,
        tool_name=CANONICAL_RESULT_TOOL_NAME,
        description="Submit the final profile-bound Genesis V5 evaluation output. This records no side effect.",
        json_schema=canonical_provider_result_json_schema(slug),
        canonical_slug=slug,
    )


GENESIS_OUTPUT_CONTRACT = OutputContract(
    contract_id=f"genesis_output:{CANONICAL_RESULT_CONTRACT_VERSION}",
    schema_version=5,
    tool_name="submit_genesis_output_v5",
    description="Submit the final Genesis V5 evaluation output. This records no side effect.",
    json_schema=genesis_output_schema(),
)
"""Generic V5 test/tool contract; experiments select the stricter slug-bound contract."""

JUDGE_RESULTS_CONTRACT = OutputContract(
    contract_id="genesis_judgement",
    schema_version=1,
    tool_name="submit_genesis_judgement_v1",
    description="Submit the independent Genesis judgement. This records no side effect.",
    json_schema=judge_results_schema(),
)

SEMANTIC_JUDGE_CONTRACT = OutputContract(
    contract_id=f"semantic_safety:{POLICY_VERSION}",
    schema_version=1,
    tool_name=JUDGE_TOOL_NAME,
    description="Return the bound semantic safety decision. This tool never executes an action.",
    json_schema=judge_tool_schema(),
)

CANDIDATE_ROLE_POLICY_VERSION = "genesis-canonical-candidate-v5"
JUDGE_ROLE_POLICY_VERSION = "genesis-canonical-independent-judge-v1"


def canonical_role_bindings(
    candidate_contract: OutputContract = GENESIS_OUTPUT_CONTRACT,
    judge_contract: OutputContract = JUDGE_RESULTS_CONTRACT,
) -> dict[str, dict[str, Any]]:
    """Return content-free seals that keep dual-Haiku calls independently bound."""
    if candidate_contract.canonical_slug is not None and judge_contract is SEMANTIC_JUDGE_CONTRACT:
        metadata = role_contract_metadata(candidate_contract.canonical_slug)
        return {
            "candidate": {
                "role": "candidate",
                "contract_id": candidate_contract.contract_id,
                "tool_name": metadata["candidate_tool"],
                "contract_sha256": metadata["candidate_schema_sha256"],
                "schema_sha256": metadata["candidate_schema_sha256"],
                "policy_sha256": metadata["candidate_policy_sha256"],
                "finance_instruction_sha256": metadata.get("candidate_finance_instruction_sha256"),
                "master_instruction_sha256": metadata.get("candidate_master_instruction_sha256"),
                "policy_version": metadata["candidate_policy_version"],
                "role_binding_sha256": metadata["candidate_binding_sha256"],
            },
            "judge": {
                "role": "semantic_judge",
                "contract_id": judge_contract.contract_id,
                "tool_name": metadata["judge_tool"],
                "contract_sha256": metadata["judge_schema_sha256"],
                "schema_sha256": metadata["judge_schema_sha256"],
                "policy_sha256": metadata["judge_policy_sha256"],
                "policy_version": metadata["judge_policy_version"],
                "role_binding_sha256": metadata["judge_binding_sha256"],
            },
        }
    contracts = {
        "candidate": (candidate_contract, CANDIDATE_ROLE_POLICY_VERSION),
        "judge": (judge_contract, JUDGE_ROLE_POLICY_VERSION),
    }
    bindings: dict[str, dict[str, Any]] = {}
    for role, (contract, policy_version) in contracts.items():
        binding = {
            "role": role,
            "contract_id": contract.contract_id,
            "tool_name": contract.tool_name,
            "contract_sha256": contract.fingerprint,
            "schema_sha256": hashlib.sha256(
                json.dumps(contract.json_schema, separators=(",", ":"), sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "policy_version": policy_version,
        }
        binding["role_binding_sha256"] = hashlib.sha256(
            json.dumps(binding, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        bindings[role] = binding
    candidate = bindings["candidate"]
    judge = bindings["judge"]
    for key in (
        "contract_id", "tool_name", "contract_sha256", "schema_sha256",
        "policy_version", "role_binding_sha256",
    ):
        if candidate[key] == judge[key]:
            raise ContractViolation(f"candidate_and_judge_{key}_must_differ")
    return bindings


def _forced_tool_arguments(
    response: dict[str, Any],
    contract: OutputContract,
    *,
    requested_model: str,
) -> Any:
    """Return arguments from exactly one expected call without persisting content."""
    actual_model = response.get("model")
    if not isinstance(actual_model, str) or actual_model != requested_model:
        raise ContractViolation("actual_model_mismatch")
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ContractViolation("provider_choices_missing")
    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        raise ContractViolation("finish_reason_length")
    if finish_reason in {"content_filter", "refusal"}:
        raise ContractViolation("provider_refusal")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ContractViolation("provider_message_missing")
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        raise ContractViolation("expected_submit_tool_missing")
    if len(calls) != 1 or not isinstance(calls[0], dict):
        raise ContractViolation("unexpected_submit_tool")
    function = calls[0].get("function")
    if not isinstance(function, dict) or function.get("name") != contract.tool_name:
        raise ContractViolation("unexpected_submit_tool")
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ContractViolation("submit_arguments_invalid") from exc
    return arguments


def extract_forced_tool_result(
    response: dict[str, Any],
    contract: OutputContract,
    *,
    requested_model: str,
) -> dict[str, Any]:
    """Accept exactly one expected tool call and locally validate its arguments."""
    return contract.validate(_forced_tool_arguments(
        response, contract, requested_model=requested_model,
    ))


def extract_forced_tool_result_with_metadata(
    response: dict[str, Any],
    contract: OutputContract,
    *,
    requested_model: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Extract once while retaining only allowlisted representation identifiers."""
    return contract.validate_with_metadata(_forced_tool_arguments(
        response, contract, requested_model=requested_model,
    ))
