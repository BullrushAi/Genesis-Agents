from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path

import pytest

import agent_runtime
import main
import runtime.semantic_safety_judge as safety_judge
from agent_runtime import AgentRuntime
from bundle_loader import load_bundle
from canonical_profiles import CANONICAL_SLUGS, FINANCEOS_SLUGS, MASTER_OS_SLUGS
from canonical_result_contract import (
    CANONICAL_RESULT_TOOL_NAME,
    consume_canonical_result_submission,
    validate_canonical_result,
    validate_canonical_result_semantics,
    validate_canonical_result_structure,
)
from canonical_result_contract import canonical_result_tool_schema
from runtime.request_auth import Principal
from runtime.semantic_safety_judge import (
    CANDIDATE_READ_TIMEOUT_S,
    CANDIDATE_MODEL,
    ENDPOINT_READ_BUDGET_S,
    ENDPOINT_RESERVED_OVERHEAD_S,
    FINANCE_CANDIDATE_OUTPUT_TOKENS,
    JUDGE_MODEL,
    JUDGE_TOOL_NAME,
    JUDGE_READ_TIMEOUT_S,
    MASTER_CANDIDATE_OUTPUT_TOKENS,
    POLICY_VERSION,
    SafetyJudgeVerdict,
    call_exact_anthropic_candidate,
    call_exact_anthropic_judge,
    canonical_sha256,
    canonical_candidate_output_cap,
    exact_anthropic_messages_url,
    judge_candidate_semantics,
    project_candidate_narrative,
    require_canonical_native_anthropic_config,
)


CORPUS = Path(__file__).parent / "fixtures" / "v2_completion_claim_corpus.json"
LOCKED_CORPUS_SHA256 = "020c120fed54685b917aff8cbb1ecaa04e1a2bc0a1e2ae3e2dce13be34c1324f"


def _normal(slug: str) -> dict:
    value = {
        "agent_id": slug, "task_id": "task-judge", "scope": "tenant:scope",
        "status": "ANALYSIS_COMPLETE", "summary": "ANALYSIS: Bounded analysis is complete.",
        "findings": [], "proposals": [], "evidence_refs": [], "assumptions": [],
        "conflicts": [], "unknowns": [], "confidence": 0.9, "risk_flags": [],
        "recommended_next_actions": [], "requested_human_decisions": [],
        "tool_calls_summary": [], "clarification_needed": False,
        "candidate_resolution": "", "reason_human_needed": "",
        "recommended_human_resolver": "", "materiality": None, "urgency": "",
        "resolution_attempts": [], "supporting_evidence_refs": [],
        "competing_state_refs": [], "recommended_answer_options": [],
    }
    if slug.startswith("genesis-fin-"):
        value["finance_proposal"] = {
            "entity_id": None, "period": None, "transaction_or_schedule_type": None,
            "proposed_treatment": "PROPOSAL: Draft treatment for review.", "account_mapping": None,
            "tracking_mapping": None, "tax_treatment": None, "amount_logic": None,
            "source_refs": [], "policy_refs": [], "assumptions": [], "confidence": 0.8,
            "exceptions": [], "requires_human_decision": False,
            "recommended_financeos_action": "NEXT: Review the proposal.",
        }
    return value


_CLARIFICATION_DETAIL_FIELDS = (
    "candidate_resolution", "reason_human_needed", "recommended_human_resolver",
    "materiality", "urgency", "resolution_attempts", "supporting_evidence_refs",
    "competing_state_refs", "recommended_answer_options",
)


def _provider_candidate(candidate: dict) -> dict:
    """Build provider-only clarification shape without mutating the public V5 value."""
    provider_value = json.loads(json.dumps(candidate))
    needed = provider_value.pop("clarification_needed")
    if needed:
        provider_value["clarification"] = {
            field: provider_value.pop(field) for field in _CLARIFICATION_DETAIL_FIELDS
        }
    else:
        for field in _CLARIFICATION_DETAIL_FIELDS:
            provider_value.pop(field, None)
        provider_value["clarification"] = None
    return provider_value


def _raw_judge_response(candidate: dict, *, allow: bool, field: str = "summary") -> dict:
    projection = project_candidate_narrative(candidate)
    arguments = {
        "agent_id": candidate["agent_id"], "policy_version": POLICY_VERSION,
        "candidate_sha256": projection.candidate_sha256,
        "projection_sha256": projection.projection_sha256,
        "decision": "ALLOW" if allow else "DENY",
        "reason_code": "NONE" if allow else "AFFIRMATIVE_EXTERNAL_STATE",
        "offending_fields": [] if allow else [field],
    }
    return {
        "model": JUDGE_MODEL, "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": JUDGE_TOOL_NAME, "input": arguments}],
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }


def _forced_candidate_response(candidate: dict, *, provider_value: dict | None = None) -> dict:
    arguments = provider_value if provider_value is not None else _provider_candidate(candidate)
    return {
        "model": CANDIDATE_MODEL,
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {"content": None, "tool_calls": [{
                "id": "result", "type": "function",
                "function": {
                    "name": CANONICAL_RESULT_TOOL_NAME,
                    "arguments": json.dumps(arguments),
                },
            }]},
        }],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }
NARRATIVE_CASES = [
    row for row in json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]
    if row["text"] is not None
]


def test_locked_224_case_corpus_integrity_and_partition() -> None:
    raw = CORPUS.read_bytes()
    corpus = json.loads(raw)
    cases = corpus["cases"]
    assert hashlib.sha256(raw).hexdigest() == LOCKED_CORPUS_SHA256
    assert corpus["case_count"] == len(cases) == 224
    assert sum(row["text"] is not None for row in cases) == 176
    assert sum(row["text"] is None and row["field"] == "external_state_claims" for row in cases) == 48

    finance_holdout = [
        row for row in cases
        if row["id"].startswith("finance-production-")
    ]
    assert len(finance_holdout) == 16
    assert sum(not row["expected_ok"] for row in finance_holdout) == 8
    assert sum(row["expected_ok"] for row in finance_holdout) == 8
    assert len({row["field"] for row in finance_holdout}) == 8
    hostile_prefixes = {
        "summary": "ANALYSIS:",
        "findings": "ANALYSIS:",
        "finance_proposal.exceptions": "RISK:",
        "proposals": "PROPOSAL:",
        "recommended_next_actions": "NEXT:",
        "finance_proposal.recommended_financeos_action": "NEXT:",
        "assumptions": "UNKNOWN:",
        "finance_proposal.amount_logic": "PROPOSAL:",
    }
    assert all(
        row["text"].startswith(hostile_prefixes[row["field"]])
        for row in finance_holdout
    )


def test_finance_production_holdout_is_structurally_valid_and_safe_controls_remain_local_safe():
    cases = json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]
    for row in (item for item in cases if item["id"].startswith("finance-production-")):
        slug = {"genesis-fin-commissions": "genesis-fin-payroll-tax"}.get(row["slug"], row["slug"])
        candidate = _normal(slug)
        if row["field"].startswith("finance_proposal."):
            field = row["field"].split(".", 1)[1]
            candidate["finance_proposal"][field] = (
                [row["text"]]
                if isinstance(candidate["finance_proposal"].get(field), list)
                else row["text"]
            )
        else:
            candidate[row["field"]] = (
                [row["text"]]
                if isinstance(candidate.get(row["field"]), list)
                else row["text"]
            )
        assert validate_canonical_result_structure(slug, candidate).ok is True
        if row["expected_ok"]:
            assert validate_canonical_result_semantics(slug, candidate).ok is True


@pytest.mark.asyncio
@pytest.mark.parametrize("case", NARRATIVE_CASES, ids=lambda row: row["id"])
async def test_locked_176_narrative_cases_cross_exact_fake_judge_transport(case):
    candidate = _normal(case["slug"])
    field = case["field"]
    if field.startswith("finance_proposal."):
        finance_field = field.split(".", 1)[1]
        candidate["finance_proposal"][finance_field] = (
            [case["text"]]
            if isinstance(candidate["finance_proposal"].get(finance_field), list)
            else case["text"]
        )
    else:
        candidate[field] = [case["text"]] if isinstance(candidate.get(field), list) else case["text"]

    async def transport(_request):
        return _raw_judge_response(candidate, allow=case["expected_ok"], field=field)

    verdict = await judge_candidate_semantics(candidate, transport=transport)
    assert verdict.ok is case["expected_ok"]
    assert verdict.reason == case["expected_reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", sorted(CANONICAL_SLUGS))
async def test_all24_valid_clarification_narratives_are_allowed(slug):
    candidate = _normal(slug)
    candidate.update({
        "status": "HOLD", "clarification_needed": True,
        "candidate_resolution": "PROPOSAL: Retain the current bounded interpretation pending review.",
        "reason_human_needed": "ANALYSIS: Two authorized records conflict and require precedence judgment.",
        "recommended_human_resolver": "NEXT: Designated accountable owner",
        "materiality": 0.8, "urgency": "CLOSURE_BATCH",
        "resolution_attempts": [
            {"source_or_capability": "source-a", "result": "ANALYSIS: record conflicts", "timestamp": "2026-08-26T00:00:00Z"},
            {"source_or_capability": "source-b", "result": "ANALYSIS: record conflicts", "timestamp": "2026-08-26T00:01:00Z"},
        ],
        "supporting_evidence_refs": ["evidence:a"], "competing_state_refs": ["state:b"],
        "recommended_answer_options": ["CONFIRM", "CORRECT"],
    })
    assert validate_canonical_result(slug, candidate).ok

    async def transport(_request):
        return _raw_judge_response(candidate, allow=True)

    assert (await judge_candidate_semantics(candidate, transport=transport)).ok


def _clarification_candidate(slug: str) -> dict:
    candidate = _normal(slug)
    candidate.update({
        "status": "HOLD", "clarification_needed": True,
        "candidate_resolution": "PROPOSAL: Retain the bounded interpretation.",
        "reason_human_needed": "ANALYSIS: Two authorized records conflict.",
        "recommended_human_resolver": "NEXT: Accountable owner",
        "materiality": 0.8, "urgency": "CLOSURE_BATCH",
        "resolution_attempts": [
            {"source_or_capability": "source-a", "result": "ANALYSIS: Record conflicts.", "timestamp": "2026-08-26T00:00:00Z"},
            {"source_or_capability": "source-b", "result": "ANALYSIS: Record conflicts.", "timestamp": "2026-08-26T00:01:00Z"},
        ],
        "supporting_evidence_refs": ["evidence:a"],
        "competing_state_refs": ["state:b"],
        "recommended_answer_options": ["CONFIRM", "CORRECT"],
    })
    return candidate


def _deny_verdict() -> SafetyJudgeVerdict:
    return SafetyJudgeVerdict(False, "external_state_claim_in_narrative", "DENY", "AFFIRMATIVE_EXTERNAL_STATE", ("summary",), "a" * 64, "b" * 64, 100, JUDGE_MODEL, 10, 2, 0.00002, "estimated", "anthropic-public-2025-11-01-v1")


def _allow_verdict(candidate: dict) -> SafetyJudgeVerdict:
    projection = project_candidate_narrative(candidate)
    return SafetyJudgeVerdict(
        True, None, "ALLOW", "NONE", (), projection.candidate_sha256,
        projection.projection_sha256, projection.projection_bytes, JUDGE_MODEL,
        10, 2, 0.00002, "estimated", "anthropic-public-2025-11-01-v1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("clarification_needed", [False, True], ids=["null", "object"])
async def test_agent_runtime_projects_provider_clarification_to_unchanged_flat_v5(
    monkeypatch, tmp_path, clarification_needed,
):
    slug = "genesis-knowledge-evidence"
    candidate = _clarification_candidate(slug) if clarification_needed else _normal(slug)
    provider_value = _provider_candidate(candidate)
    provider_snapshot = json.loads(json.dumps(provider_value))
    candidate_snapshot = json.loads(json.dumps(candidate))
    seen: list[dict] = []

    async def candidate_call(*_args, **_kwargs):
        return _forced_candidate_response(candidate, provider_value=provider_value)

    async def allow(value, **_kwargs):
        seen.append(value)
        return _allow_verdict(value)

    monkeypatch.setenv("GENESIS_LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(AgentRuntime, "_call_llm", candidate_call)
    monkeypatch.setattr(agent_runtime, "judge_candidate_semantics", allow)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    result = await AgentRuntime(
        "https://api.anthropic.com/v1/messages", "redacted-test-key",
    )._run_loop(
        load_bundle(slug), "bounded", {}, "job-provider-clarification", tmp_path,
        None, "session-provider-clarification",
    )

    if clarification_needed:
        assert result["ok"] is False
        assert result["response"] == ""
        assert seen == []
        assert provider_value == provider_snapshot
        return
    assert result["ok"] is True
    assert json.loads(result["response"]) == candidate_snapshot
    assert seen == [candidate_snapshot]
    assert provider_value == provider_snapshot
    assert candidate == candidate_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("clarification_needed", [False, True], ids=["null", "object"])
async def test_live_test_projects_provider_clarification_to_unchanged_flat_v5(
    monkeypatch, clarification_needed,
):
    slug = "genesis-knowledge-evidence"
    candidate = _clarification_candidate(slug) if clarification_needed else _normal(slug)
    provider_value = _provider_candidate(candidate)
    provider_snapshot = json.loads(json.dumps(provider_value))
    candidate_snapshot = json.loads(json.dumps(candidate))
    seen: list[dict] = []

    async def candidate_call(*_args, **_kwargs):
        return _forced_candidate_response(candidate, provider_value=provider_value)

    async def allow(value, **_kwargs):
        seen.append(value)
        return _allow_verdict(value)

    monkeypatch.setenv("GENESIS_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_URL", "https://api.anthropic.com/v1/messages")
    monkeypatch.setenv("LLM_API_KEY", "redacted-test-key")
    monkeypatch.setattr(main, "call_exact_anthropic_candidate", candidate_call)
    monkeypatch.setattr(main, "judge_candidate_semantics", allow)
    principal = Principal(
        "test", "tenant", "client", frozenset({"agent:run"}), "test", 9999999999,
    )
    if clarification_needed:
        with pytest.raises(main.HTTPException) as raised:
            await main.run_agent(slug, main.RunRequest(prompt="bounded", mode="live_test"), principal)
        assert raised.value.detail["reason"] == "provider_clarification_representation_invalid"
        assert seen == []
        assert provider_value == provider_snapshot
        return
    response = await main.run_agent(
        slug, main.RunRequest(prompt="bounded", mode="live_test"), principal,
    )
    envelope = json.loads(response.response)
    assert json.loads(envelope["response"]) == candidate_snapshot
    assert seen == [candidate_snapshot]
    assert provider_value == provider_snapshot
    assert candidate == candidate_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["runtime", "live_test"])
async def test_invalid_provider_clarification_is_content_free_and_skips_judge(
    monkeypatch, tmp_path, path,
):
    slug = "genesis-knowledge-evidence"
    candidate = _clarification_candidate(slug)
    provider_value = _provider_candidate(candidate)
    provider_value["clarification"].pop("candidate_resolution")
    provider_value["clarification"]["private_payload"] = "must-not-leak"
    calls = {"judge": 0}
    output_contract_attrs: dict = {}

    async def candidate_call(*_args, **_kwargs):
        return _forced_candidate_response(candidate, provider_value=provider_value)

    async def forbidden_judge(*_args, **_kwargs):
        calls["judge"] += 1
        raise AssertionError("invalid provider representation must not reach the judge")

    monkeypatch.setenv("GENESIS_LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    if path == "runtime":
        monkeypatch.setattr(AgentRuntime, "_call_llm", candidate_call)
        monkeypatch.setattr(agent_runtime, "judge_candidate_semantics", forbidden_judge)
        result = await AgentRuntime(
            "https://api.anthropic.com/v1/messages", "redacted-test-key",
        )._run_loop(
            load_bundle(slug), "bounded", {}, "job-invalid-provider", tmp_path,
            None, "session-invalid-provider",
        )
        assert result["ok"] is False and result["response"] == ""
        assert result["output_contract_eval"]["reason"] == (
            "provider_clarification_representation_invalid"
        )
        evaluation = result["output_contract_eval"]
        assert evaluation["clarification_representation_subtype"] == "canonical_invalid"
        assert evaluation["clarification_representation_missing_fields"] == [
            "candidate_resolution"
        ]
        assert evaluation["clarification_representation_missing_field_count"] == 1
        assert evaluation["clarification_representation_extra_fields"] == []
        assert evaluation["clarification_representation_extra_field_count"] == 1
        assert "private_payload" not in str(evaluation)
        assert "must-not-leak" not in json.dumps(result)
    else:
        monkeypatch.setenv("LLM_API_URL", "https://api.anthropic.com/v1/messages")
        monkeypatch.setenv("LLM_API_KEY", "redacted-test-key")
        monkeypatch.setattr(main, "call_exact_anthropic_candidate", candidate_call)
        monkeypatch.setattr(main, "judge_candidate_semantics", forbidden_judge)

        principal = Principal(
            "test", "tenant", "client", frozenset({"agent:run"}), "test", 9999999999,
        )
        with pytest.raises(main.HTTPException) as raised:
            await main.run_agent(
                slug, main.RunRequest(prompt="bounded", mode="live_test"), principal,
            )
        assert raised.value.detail == {
            "error": "output_contract_failed",
            "reason": "provider_clarification_representation_invalid",
        }
        assert "must-not-leak" not in str(raised.value.detail)
    assert calls == {"judge": 0}


@pytest.mark.asyncio
async def test_agent_runtime_rejects_judge_denial_content_free(monkeypatch, tmp_path):
    slug = "genesis-knowledge-evidence"
    candidate = _normal(slug)
    async def candidate_call(*_args, **_kwargs):
        return _forced_candidate_response(candidate)
    async def deny(*_args, **_kwargs): return _deny_verdict()
    monkeypatch.setattr(AgentRuntime, "_call_llm", candidate_call)
    monkeypatch.setattr(agent_runtime, "judge_candidate_semantics", deny)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    result = await AgentRuntime("https://api.anthropic.com", "redacted")._run_loop(load_bundle(slug), "bounded", {}, "job", tmp_path, None, "session")
    assert result["ok"] is False
    assert result["error"] == "output_contract_failed"
    assert result["response"] == ""
    assert result["output_contract_eval"]["reason"] == "external_state_claim_in_narrative"
    assert result["output_contract_eval"]["deterministic_ok"] is None
    assert result["output_contract_eval"]["deterministic_failing_fields"] == []
    assert result["output_contract_eval"]["deterministic_failing_field_count"] == 0
    assert result["output_contract_eval"]["semantic_disagreement"] is False


@pytest.mark.asyncio
async def test_agent_runtime_structure_failure_makes_zero_judge_calls(monkeypatch, tmp_path):
    slug = "genesis-knowledge-evidence"
    candidate = _normal(slug)
    candidate["summary"] = "missing required intent prefix"
    calls = {"candidate": 0, "judge": 0}

    async def candidate_call(*_args, **_kwargs):
        calls["candidate"] += 1
        return _forced_candidate_response(candidate)

    async def forbidden_judge(*_args, **_kwargs):
        calls["judge"] += 1
        raise AssertionError("judge must not run for structural failure")

    monkeypatch.setattr(AgentRuntime, "_call_llm", candidate_call)
    monkeypatch.setattr(agent_runtime, "judge_candidate_semantics", forbidden_judge)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    result = await AgentRuntime("https://api.anthropic.com", "redacted")._run_loop(
        load_bundle(slug), "bounded", {}, "job", tmp_path, None, "session",
    )
    assert result["ok"] is False
    assert result["output_contract_eval"]["reason"] == "invalid_narrative_prefix"
    assert result["output_contract_eval"]["structure_ok"] is False
    # Phase 2 mirrors phase 1's corrective retry into the runtime path: the
    # structural rejection is retried exactly once with validator feedback
    # (candidate 2), and the judge still never runs on structural failure.
    assert calls == {"candidate": 2, "judge": 0}


@pytest.mark.asyncio
async def test_agent_runtime_judge_allow_never_overrides_deterministic_reject(
    monkeypatch, tmp_path,
):
    slug = "genesis-platform-engineer"
    candidate = _normal(slug)
    candidate["summary"] = "ANALYSIS: Deployment completed and independently verified."
    original_sha = canonical_sha256(candidate)
    calls = {"candidate": 0, "judge": 0}

    async def candidate_call(*_args, **_kwargs):
        calls["candidate"] += 1
        return _forced_candidate_response(candidate)

    async def allow(*_args, **_kwargs):
        calls["judge"] += 1
        return _allow_verdict(candidate)

    monkeypatch.setattr(AgentRuntime, "_call_llm", candidate_call)
    monkeypatch.setattr(agent_runtime, "judge_candidate_semantics", allow)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    result = await AgentRuntime("https://api.anthropic.com", "redacted")._run_loop(
        load_bundle(slug), "bounded", {}, "job", tmp_path, None, "session",
    )
    evaluation = result["output_contract_eval"]
    assert result["ok"] is False and result["response"] == ""
    assert evaluation["judge_decision"] == "ALLOW"
    assert evaluation["deterministic_reason"] == "real_world_completion_claim"
    assert evaluation["deterministic_failing_fields"] == ["summary"]
    assert evaluation["deterministic_failing_field_count"] == 1
    assert evaluation["semantic_disagreement"] is True
    assert evaluation["candidate_sha256"] == original_sha == canonical_sha256(candidate)
    # Phase 2 corrective-retry mirror: the deterministic rejection is
    # retried once; the judge ALLOWs both identical candidates and the
    # deterministic reject still wins both times.
    assert calls == {"candidate": 2, "judge": 2}


@pytest.mark.asyncio
async def test_live_test_rejects_same_judge_denial_content_free(monkeypatch):
    slug = "genesis-knowledge-evidence"
    monkeypatch.setenv("GENESIS_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_URL", "https://api.anthropic.com/v1/messages")
    monkeypatch.setenv("LLM_API_KEY", "redacted-test-key")
    async def candidate_router(*_args, **_kwargs):
        return _forced_candidate_response(_normal(slug))
    async def deny(*_args, **_kwargs): return _deny_verdict()
    monkeypatch.setattr(main, "call_exact_anthropic_candidate", candidate_router)
    monkeypatch.setattr(main, "judge_candidate_semantics", deny)
    principal = Principal("test", "tenant", "client", frozenset({"agent:run"}), "test", 9999999999)
    with pytest.raises(Exception) as caught:
        await main.run_agent(slug, main.RunRequest(prompt="bounded", mode="live_test"), principal)
    assert getattr(caught.value, "status_code", None) == 502
    assert caught.value.detail == {"error": "output_contract_failed", "reason": "external_state_claim_in_narrative"}


@pytest.mark.asyncio
async def test_live_test_structure_failure_makes_zero_judge_calls(monkeypatch):
    slug = "genesis-knowledge-evidence"
    candidate = _normal(slug)
    candidate["summary"] = "missing required intent prefix"
    calls = {"candidate": 0, "judge": 0}

    async def candidate_router(*_args, **_kwargs):
        calls["candidate"] += 1
        return _forced_candidate_response(candidate)

    async def forbidden_judge(*_args, **_kwargs):
        calls["judge"] += 1
        raise AssertionError("judge must not run for structural failure")

    monkeypatch.setenv("GENESIS_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_URL", "https://api.anthropic.com/v1/messages")
    monkeypatch.setenv("LLM_API_KEY", "redacted-test-key")
    monkeypatch.setattr(main, "call_exact_anthropic_candidate", candidate_router)
    monkeypatch.setattr(main, "judge_candidate_semantics", forbidden_judge)
    principal = Principal("test", "tenant", "client", frozenset({"agent:run"}), "test", 9999999999)
    with pytest.raises(main.HTTPException) as raised:
        await main.run_agent(slug, main.RunRequest(prompt="bounded", mode="live_test"), principal)
    assert raised.value.detail == {
        "error": "output_contract_failed", "reason": "invalid_narrative_prefix",
    }
    # The judge must never run for a structural failure (the invariant this
    # test protects). Since the Oct 21, 2026 corrective-retry fix, a contract
    # rejection is retried exactly once with validator feedback, so the
    # candidate is called twice; the rejection detail is unchanged.
    assert calls == {"candidate": 2, "judge": 0}


@pytest.mark.asyncio
async def test_live_test_judge_allow_never_overrides_deterministic_reject(monkeypatch):
    slug = "genesis-platform-engineer"
    candidate = _normal(slug)
    candidate["summary"] = "ANALYSIS: Deployment completed and independently verified."
    calls = {"candidate": 0, "judge": 0}
    semantic_span_attributes: dict = {}

    async def candidate_router(*_args, **_kwargs):
        calls["candidate"] += 1
        return _forced_candidate_response(candidate)

    async def allow(*_args, **_kwargs):
        calls["judge"] += 1
        return _allow_verdict(candidate)

    monkeypatch.setenv("GENESIS_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_URL", "https://api.anthropic.com/v1/messages")
    monkeypatch.setenv("LLM_API_KEY", "redacted-test-key")
    monkeypatch.setattr(main, "call_exact_anthropic_candidate", candidate_router)
    monkeypatch.setattr(main, "judge_candidate_semantics", allow)
    principal = Principal("test", "tenant", "client", frozenset({"agent:run"}), "test", 9999999999)
    with pytest.raises(main.HTTPException) as raised:
        await main.run_agent(slug, main.RunRequest(prompt="bounded", mode="live_test"), principal)
    assert raised.value.detail == {
        "error": "output_contract_failed", "reason": "real_world_completion_claim",
    }
    assert calls == {"candidate": 1, "judge": 1}


@pytest.mark.parametrize(
    "url",
    [
        "https://api.anthropic.com",
        "https://api.anthropic.com/",
        "https://api.anthropic.com/v1/messages",
        "https://api.anthropic.com:443/v1/messages/",
    ],
)
def test_exact_anthropic_url_accepts_only_normalized_authority(url):
    assert exact_anthropic_messages_url(url) == "https://api.anthropic.com/v1/messages"


@pytest.mark.parametrize(
    "url",
    [
        "http://api.anthropic.com/v1/messages",
        "https://api.anthropic.com.example.invalid/v1/messages",
        "https://example.invalid/api.anthropic.com",
        "https://user:secret@api.anthropic.com/v1/messages",
        "https://api.anthropic.com:444/v1/messages",
        "https://api.anthropic.com:notaport/v1/messages",
        "https://api.anthropic.com/v1/messages?next=evil",
        "https://api.anthropic.com/v1/messages#fragment",
        "https://api.anthropic.com/v1/other",
    ],
)
def test_exact_anthropic_url_rejects_hostile_authorities_content_free(url):
    with pytest.raises(RuntimeError) as raised:
        exact_anthropic_messages_url(url)
    assert str(raised.value) == "semantic_judge_transport_failed"


@pytest.mark.parametrize(
    ("provider", "url", "key", "ok"),
    [
        ("", "https://api.anthropic.com", "redacted-test-key", True),
        ("anthropic", "https://api.anthropic.com/v1/messages", "redacted-test-key", True),
        ("openai", "https://api.anthropic.com/v1/messages", "redacted-test-key", False),
        ("anthropic", "https://api.swarmsync.ai/v1/chat/completions", "redacted-test-key", False),
        ("anthropic", "https://api.anthropic.com/v1/messages", "", False),
    ],
)
def test_canonical_native_config_requires_route_provider_and_credentials(provider, url, key, ok):
    if ok:
        assert require_canonical_native_anthropic_config(
            api_url=url, api_key=key, declared_provider=provider,
        ) == "https://api.anthropic.com/v1/messages"
    else:
        with pytest.raises(RuntimeError) as raised:
            require_canonical_native_anthropic_config(
                api_url=url, api_key=key, declared_provider=provider,
            )
        assert str(raised.value) == "canonical_candidate_transport_failed"


@pytest.mark.asyncio
async def test_exact_candidate_transport_is_one_forced_haiku_call(monkeypatch):
    candidate = _normal("genesis-knowledge-evidence")
    seen: list[tuple[str, dict]] = []

    class Response:
        status_code = 200
        def json(self):
            return {
                "model": CANDIDATE_MODEL,
                "stop_reason": "tool_use",
                "content": [{
                    "type": "tool_use", "id": "result",
                    "name": CANONICAL_RESULT_TOOL_NAME, "input": _provider_candidate(candidate),
                }],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }

    class Client:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, url, *, headers, json):
            assert headers["x-api-key"] == "redacted-test-key"
            seen.append((url, json))
            return Response()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    result = await call_exact_anthropic_candidate(
        api_url="https://api.anthropic.com",
        api_key="redacted-test-key",
        slug=candidate["agent_id"],
        messages=[{"role": "user", "content": "bounded"}],
        max_tokens=400,
    )
    assert len(seen) == 1
    url, request = seen[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert request["model"] == CANDIDATE_MODEL
    assert request["tool_choice"] == {
        "type": "tool",
        "name": CANONICAL_RESULT_TOOL_NAME,
        "disable_parallel_tool_use": True,
    }
    assert len(request["tools"]) == 1
    assert result["choices"][0]["message"]["content"] is None


@pytest.mark.asyncio
async def test_exact_candidate_transport_rejects_provider_free_text_content_free(monkeypatch):
    async def raw_text(*_args, **_kwargs):
        return {
            "model": CANDIDATE_MODEL,
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "untrusted prose"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    monkeypatch.setattr(safety_judge, "_post_exact_anthropic", raw_text)
    with pytest.raises(ValueError) as raised:
        await call_exact_anthropic_candidate(
            api_url="https://api.anthropic.com",
            api_key="redacted-test-key",
            slug="genesis-knowledge-evidence",
            messages=[{"role": "user", "content": "bounded"}],
            max_tokens=400,
        )
    assert str(raised.value) == "exact_forced_tool_response_missing"
    assert "untrusted" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(("simulated_delay", "expected_ok"), [(60.0, True), (60.1, False)])
async def test_candidate_read_budget_is_bounded_one_request_and_content_free(
    monkeypatch, simulated_delay, expected_ok,
):
    import httpx

    candidate = _normal("genesis-fin-payroll-tax")
    observed = {"requests": 0, "timeout": None, "body": None}

    class Response:
        status_code = 200
        def json(self):
            return {
                "model": CANDIDATE_MODEL,
                "stop_reason": "tool_use",
                "content": [{
                    "type": "tool_use", "id": "result",
                    "name": CANONICAL_RESULT_TOOL_NAME, "input": _provider_candidate(candidate),
                }],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }

    class Client:
        def __init__(self, *, timeout): observed["timeout"] = timeout
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, _url, *, headers, json):
            assert headers["x-api-key"] == "redacted-test-key"
            observed["requests"] += 1
            observed["body"] = json
            if simulated_delay > observed["timeout"].read:
                raise httpx.ReadTimeout("simulated provider text must not escape")
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    kwargs = {
        "api_url": "https://api.anthropic.com/v1/messages",
        "api_key": "redacted-test-key",
        "slug": candidate["agent_id"],
        "messages": [{"role": "user", "content": "bounded"}],
        "max_tokens": 8_000,
        "declared_provider": "anthropic",
    }
    if expected_ok:
        assert (await call_exact_anthropic_candidate(**kwargs))["model"] == CANDIDATE_MODEL
    else:
        with pytest.raises(RuntimeError) as raised:
            await call_exact_anthropic_candidate(**kwargs)
        assert str(raised.value) == "canonical_candidate_transport_failed"
        assert "simulated" not in str(raised.value)
    assert observed["requests"] == 1
    assert observed["timeout"].read == CANDIDATE_READ_TIMEOUT_S
    assert observed["timeout"].connect == 15.0
    assert observed["timeout"].write == 15.0
    assert observed["timeout"].pool == 15.0
    assert observed["body"]["max_tokens"] == FINANCE_CANDIDATE_OUTPUT_TOKENS


@pytest.mark.asyncio
@pytest.mark.parametrize(("simulated_delay", "expected_ok"), [(5.0, True), (5.1, False)])
async def test_judge_read_budget_remains_separate_one_request_and_content_free(
    monkeypatch, simulated_delay, expected_ok,
):
    import httpx

    observed = {"requests": 0, "timeout": None}

    class Response:
        status_code = 200
        def json(self): return {"ok": True}

    class Client:
        def __init__(self, *, timeout): observed["timeout"] = timeout
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, _url, *, headers, json):
            assert headers["x-api-key"] == "redacted-test-key"
            assert json == {"judge": "request"}
            observed["requests"] += 1
            if simulated_delay > observed["timeout"].read:
                raise httpx.ReadTimeout("simulated judge text must not escape")
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    call = call_exact_anthropic_judge(
        {"judge": "request"},
        api_url="https://api.anthropic.com/v1/messages",
        api_key="redacted-test-key",
    )
    if expected_ok:
        assert await call == {"ok": True}
    else:
        with pytest.raises(RuntimeError) as raised:
            await call
        assert str(raised.value) == "semantic_judge_transport_failed"
        assert "simulated" not in str(raised.value)
    assert observed["requests"] == 1
    assert observed["timeout"].read == JUDGE_READ_TIMEOUT_S


def test_candidate_plus_judge_read_budgets_fit_endpoint_policy():
    assert CANDIDATE_READ_TIMEOUT_S == 60.0
    assert JUDGE_READ_TIMEOUT_S == 5.0
    assert ENDPOINT_RESERVED_OVERHEAD_S == 2.0
    total = (
        CANDIDATE_READ_TIMEOUT_S
        + JUDGE_READ_TIMEOUT_S
        + ENDPOINT_RESERVED_OVERHEAD_S
    )
    assert total == ENDPOINT_READ_BUDGET_S == 67.0
    assert total < 75.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "connect_timeout", "read_timeout", "write_timeout", "pool_timeout",
        "network_error", "http_status_error", "response_json_invalid",
        "response_shape_invalid",
    ],
)
async def test_exact_transport_failure_stages_are_sealed_and_content_free(
    monkeypatch, stage,
):
    import httpx

    exception_by_stage = {
        "connect_timeout": httpx.ConnectTimeout("must-not-persist"),
        "read_timeout": httpx.ReadTimeout("must-not-persist"),
        "write_timeout": httpx.WriteTimeout("must-not-persist"),
        "pool_timeout": httpx.PoolTimeout("must-not-persist"),
        "network_error": httpx.NetworkError("must-not-persist"),
    }

    class Response:
        status_code = 503 if stage == "http_status_error" else 200

        def json(self):
            if stage == "response_json_invalid":
                raise ValueError("must-not-persist")
            if stage == "response_shape_invalid":
                return ["must-not-persist"]
            return {"ok": True}

    class Client:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, *_args, **_kwargs):
            if stage in exception_by_stage:
                raise exception_by_stage[stage]
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    with pytest.raises(safety_judge.ExactAnthropicTransportError) as raised:
        await safety_judge._post_exact_anthropic(
            {"sealed": True}, api_url="https://api.anthropic.com/v1/messages",
            api_key="redacted-test-key", read_timeout_s=5.0,
            failure_reason="semantic_judge_transport_failed",
        )
    error = raised.value
    assert error.stage == stage
    assert error.elapsed_ms >= 0.0
    assert error.http_status_class == ("5xx" if stage == "http_status_error" else None)
    assert str(error) == "semantic_judge_transport_failed"
    assert "must-not-persist" not in repr(error)


def test_exact19_domain_candidate_cap_matrix_and_unknown_fail_closed():
    rows = {
        slug: canonical_candidate_output_cap(slug, 8_000)
        for slug in sorted(CANONICAL_SLUGS)
    }
    assert len(rows) == 19
    assert all(rows[slug] == MASTER_CANDIDATE_OUTPUT_TOKENS for slug in MASTER_OS_SLUGS)
    assert all(rows[slug] == FINANCE_CANDIDATE_OUTPUT_TOKENS for slug in FINANCEOS_SLUGS)
    assert len(MASTER_OS_SLUGS) == 10
    assert len(FINANCEOS_SLUGS) == 9
    assert canonical_candidate_output_cap("genesis-fin-ap", 913) == 913
    with pytest.raises(ValueError, match="canonical_candidate_cap_invalid_agent"):
        canonical_candidate_output_cap("legacy-agent", 8_000)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slug", "expected_cap"),
    [("genesis-knowledge-evidence", 4_096), ("genesis-fin-payroll-tax", 4_096)],
)
async def test_provider_body_uses_exact_domain_cap_never_requested_8000_or_2048(
    monkeypatch, slug, expected_cap,
):
    import httpx

    candidate = _normal(slug)
    seen = []

    class Response:
        status_code = 200
        def json(self):
            return {
                "model": CANDIDATE_MODEL, "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "id": "result", "name": CANONICAL_RESULT_TOOL_NAME, "input": _provider_candidate(candidate)}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }

    class Client:
        def __init__(self, *, timeout): self.timeout = timeout
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, _url, *, headers, json):
            seen.append(json["max_tokens"])
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    await call_exact_anthropic_candidate(
        api_url="https://api.anthropic.com/v1/messages",
        api_key="redacted-test-key",
        slug=slug,
        messages=[{"role": "user", "content": "bounded"}],
        max_tokens=8_000,
        declared_provider="anthropic",
    )
    assert seen == [expected_cap]
    assert expected_cap not in {8_000, 2_048}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_reason", "output_tokens", "expected_error"),
    [
        ("tool_use", 4_096, None),
        ("tool_use", 4_097, "canonical_candidate_transport_failed"),
        ("max_tokens", 800, "exact_forced_tool_response_truncated"),
        ("max_tokens", 4_096, "exact_forced_tool_response_truncated"),
    ],
)
async def test_canon_steward_master_4096_controls_fail_closed_once(
    monkeypatch, stop_reason, output_tokens, expected_error,
):
    import httpx

    slug = "genesis-knowledge-evidence"
    candidate = _normal(slug)
    compact_bytes = len(json.dumps(candidate, separators=(",", ":")).encode("utf-8"))
    candidate["summary"] += "x" * max(0, output_tokens * 4 - compact_bytes)
    calls = {"count": 0, "cap": None}

    class Response:
        status_code = 200
        def json(self):
            return {
                "model": CANDIDATE_MODEL,
                "stop_reason": stop_reason,
                "content": [] if stop_reason == "max_tokens" else [{
                    "type": "tool_use", "id": "result",
                    "name": CANONICAL_RESULT_TOOL_NAME, "input": _provider_candidate(candidate),
                }],
                "usage": {"input_tokens": 100, "output_tokens": output_tokens},
            }

    class Client:
        def __init__(self, *, timeout): self.timeout = timeout
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, _url, *, headers, json):
            calls["count"] += 1
            calls["cap"] = json["max_tokens"]
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    kwargs = {
        "api_url": "https://api.anthropic.com/v1/messages",
        "api_key": "redacted-test-key",
        "slug": slug,
        "messages": [{"role": "user", "content": "bounded"}],
        "max_tokens": 8_000,
        "declared_provider": "anthropic",
    }
    if expected_error is None:
        result = await call_exact_anthropic_candidate(**kwargs)
        assert consume_canonical_result_submission(slug, result).ok is True
    else:
        with pytest.raises((RuntimeError, ValueError)) as raised:
            await call_exact_anthropic_candidate(**kwargs)
        assert str(raised.value) == expected_error
    assert calls == {"count": 1, "cap": 4_096}


@pytest.mark.asyncio
@pytest.mark.parametrize("output_tokens", [913, 1_100, 1_200])
async def test_finance_913_to_1200_token_valid_controls_preserve_complete_v5_result(
    monkeypatch, output_tokens,
):
    import httpx

    slug = "genesis-fin-payroll-tax"
    candidate = _normal(slug)
    compact_bytes = len(json.dumps(candidate, separators=(",", ":")).encode("utf-8"))
    candidate["summary"] += "x" * max(0, output_tokens * 4 - compact_bytes)
    assert (
        len(json.dumps(candidate, separators=(",", ":")).encode("utf-8")) + 3
    ) // 4 == output_tokens
    seen = {"requests": 0, "max_tokens": None}

    class Response:
        status_code = 200
        def json(self):
            return {
                "model": CANDIDATE_MODEL,
                "stop_reason": "tool_use",
                "content": [{
                    "type": "tool_use", "id": "result",
                    "name": CANONICAL_RESULT_TOOL_NAME, "input": _provider_candidate(candidate),
                }],
                "usage": {"input_tokens": 100, "output_tokens": output_tokens},
            }

    class Client:
        def __init__(self, *, timeout): self.timeout = timeout
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, _url, *, headers, json):
            assert headers["x-api-key"] == "redacted-test-key"
            seen["requests"] += 1
            seen["max_tokens"] = json["max_tokens"]
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    result = await call_exact_anthropic_candidate(
        api_url="https://api.anthropic.com/v1/messages",
        api_key="redacted-test-key",
        slug=slug,
        messages=[{"role": "user", "content": "bounded"}],
        max_tokens=8_000,
        declared_provider="anthropic",
    )
    submission = consume_canonical_result_submission(slug, result)
    assert submission.ok is True
    assert submission.value == candidate
    assert result["usage"]["completion_tokens"] == output_tokens
    assert seen == {"requests": 1, "max_tokens": 4_096}


@pytest.mark.asyncio
async def test_finance_provider_output_above_cap_fails_once_content_free(monkeypatch):
    import httpx

    slug = "genesis-fin-payroll-tax"
    candidate = _normal(slug)
    calls = 0

    class Response:
        status_code = 200
        def json(self):
            return {
                "model": CANDIDATE_MODEL,
                "stop_reason": "tool_use",
                "content": [{
                    "type": "tool_use", "id": "result",
                    "name": CANONICAL_RESULT_TOOL_NAME, "input": _provider_candidate(candidate),
                }],
                "usage": {"input_tokens": 100, "output_tokens": 4_097},
            }

    class Client:
        def __init__(self, *, timeout): self.timeout = timeout
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    with pytest.raises(RuntimeError) as raised:
        await call_exact_anthropic_candidate(
            api_url="https://api.anthropic.com/v1/messages",
            api_key="redacted-test-key",
            slug=slug,
            messages=[{"role": "user", "content": "bounded"}],
            max_tokens=8_000,
            declared_provider="anthropic",
        )
    assert str(raised.value) == "canonical_candidate_transport_failed"
    assert calls == 1


@pytest.mark.asyncio
async def test_candidate_provider_truncation_at_cap_stops_once_content_free(monkeypatch):
    import httpx

    calls = 0

    class Response:
        status_code = 200
        def json(self):
            return {
                "model": CANDIDATE_MODEL,
                "stop_reason": "max_tokens",
                "content": [],
                "usage": {"input_tokens": 100, "output_tokens": 4_096},
            }

    class Client:
        def __init__(self, *, timeout): self.timeout = timeout
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    with pytest.raises(ValueError) as raised:
        await call_exact_anthropic_candidate(
            api_url="https://api.anthropic.com/v1/messages",
            api_key="redacted-test-key",
            slug="genesis-fin-payroll-tax",
            messages=[{"role": "user", "content": "bounded"}],
            max_tokens=8_000,
            declared_provider="anthropic",
        )
    assert str(raised.value) == "exact_forced_tool_response_truncated"
    assert calls == 1


@pytest.mark.asyncio
async def test_agent_runtime_observability_reports_requested_and_effective_finance_cap(monkeypatch):
    candidate = _normal("genesis-fin-payroll-tax")

    async def exact_candidate(**_kwargs):
        return _forced_candidate_response(candidate)

    monkeypatch.setattr(agent_runtime, "call_exact_anthropic_candidate", exact_candidate)
    result = await AgentRuntime(
        "https://api.anthropic.com/v1/messages", "redacted-test-key"
    )._call_anthropic(
        "anthropic/claude-sonnet-4-5",
        [{"role": "user", "content": "bounded"}],
        [canonical_result_tool_schema("genesis-fin-payroll-tax")],
        8_000,
    )
    obs = result["_genesis_observability"]
    assert obs["requested_max_tokens"] == 8_000
    assert obs["effective_max_tokens"] == 4_096


@pytest.mark.asyncio
async def test_canonical_agent_runtime_transport_failure_has_no_model_fallback(monkeypatch):
    calls = 0
    async def fail_once(**_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("semantic_judge_transport_failed")

    monkeypatch.setattr(agent_runtime, "call_exact_anthropic_candidate", fail_once)
    runtime = AgentRuntime("https://api.anthropic.com", "redacted-test-key")
    with pytest.raises(RuntimeError) as raised:
        await runtime._call_anthropic(
            "anthropic/claude-sonnet-4-5",
            [{"role": "user", "content": "bounded"}],
            [canonical_result_tool_schema("genesis-knowledge-evidence")],
            400,
        )
    assert str(raised.value) == "semantic_judge_transport_failed"
    assert calls == 1


@pytest.mark.asyncio
async def test_canonical_runtime_failure_never_invokes_persona_fallback(monkeypatch):
    class FailedRuntime:
        async def execute_agent(self, *_args, **_kwargs):
            raise RuntimeError("candidate failed")

    async def forbidden_fallback(*_args, **_kwargs):
        raise AssertionError("canonical fallback transport must not run")

    monkeypatch.setattr(main, "_get_runtime", lambda: FailedRuntime())
    monkeypatch.setattr(main, "call_exact_anthropic_candidate", forbidden_fallback)
    monkeypatch.setattr(main, "call_llm_router", forbidden_fallback)
    principal = Principal("test", "tenant", "client", frozenset({"agent:run"}), "test", 9999999999)
    with pytest.raises(main.HTTPException) as raised:
        await main.run_agent(
            "genesis-knowledge-evidence",
            main.RunRequest(prompt="bounded"),
            principal,
        )
    assert raised.value.status_code == 502
    assert raised.value.detail == {
        "error": "agent_runtime_failed",
        "reason": "canonical_candidate_transport_failed",
    }


@pytest.mark.asyncio
async def test_canonical_agent_runtime_swarmsync_uses_router_not_native(
    monkeypatch, tmp_path,
):
    slug = "genesis-knowledge-evidence"
    candidate_value = _normal(slug)
    candidate_value.update({
        "candidate_resolution": "", "reason_human_needed": "",
        "recommended_human_resolver": "", "materiality": None, "urgency": "",
        "resolution_attempts": [], "supporting_evidence_refs": [],
        "competing_state_refs": [], "recommended_answer_options": [],
    })
    calls = {"native": 0, "router": 0, "judge": 0}

    async def native_candidate(**_kwargs):
        calls["native"] += 1
        raise AssertionError("native anthropic transport must not run for the router")

    async def router_candidate(**_kwargs):
        calls["router"] += 1
        payload = _forced_candidate_response(candidate_value)
        payload["transport"] = "swarmsync-router"
        return payload

    async def router_judge(*_args, **_kwargs):
        calls["judge"] += 1
        return _raw_judge_response(candidate_value, allow=True)

    def forbidden_legacy_session(*_args, **_kwargs):
        raise AssertionError("legacy provider transport must not be entered")

    monkeypatch.delenv("GENESIS_LLM_PROVIDER", raising=False)
    monkeypatch.setattr(agent_runtime, "call_exact_anthropic_candidate", native_candidate)
    monkeypatch.setattr(agent_runtime, "call_router_canonical_candidate", router_candidate)
    monkeypatch.setattr(agent_runtime, "call_router_judge", router_judge)
    monkeypatch.setattr(agent_runtime, "_llm_client_session", forbidden_legacy_session)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    result = await AgentRuntime(
        "https://api.swarmsync.ai/v1/chat/completions", "redacted-test-key"
    )._run_loop(
        load_bundle(slug), "bounded", {}, "job",
        tmp_path, None, "session",
    )
    assert calls == {"native": 0, "router": 1, "judge": 1}
    assert result["ok"] is True
    assert result["transport"] == "swarmsync-router"


@pytest.mark.asyncio
async def test_canonical_agent_runtime_native_route_calls_candidate_and_judge_once(
    monkeypatch, tmp_path,
):
    slug = "genesis-knowledge-evidence"
    candidate_value = _normal(slug)
    candidate_value.update({
        "candidate_resolution": "", "reason_human_needed": "",
        "recommended_human_resolver": "", "materiality": None, "urgency": "",
        "resolution_attempts": [], "supporting_evidence_refs": [],
        "competing_state_refs": [], "recommended_answer_options": [],
    })
    calls = {"candidate": 0, "judge": 0}

    async def candidate(**_kwargs):
        calls["candidate"] += 1
        return _forced_candidate_response(candidate_value)

    async def judge(*_args, **_kwargs):
        calls["judge"] += 1
        return _raw_judge_response(candidate_value, allow=True)

    monkeypatch.setenv("GENESIS_LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(agent_runtime, "call_exact_anthropic_candidate", candidate)
    monkeypatch.setattr(agent_runtime, "call_exact_anthropic_judge", judge)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    result = await AgentRuntime(
        "https://api.anthropic.com/v1/messages", "redacted-test-key"
    )._run_loop(load_bundle(slug), "bounded", {}, "job", tmp_path, None, "session")
    assert result["ok"] is True
    returned = json.loads(result["response"])
    assert returned["candidate_resolution"] == ""
    assert returned["urgency"] == ""
    assert calls == {"candidate": 1, "judge": 1}


@pytest.mark.asyncio
async def test_canonical_live_test_swarmsync_uses_router_not_native(monkeypatch):
    slug = "genesis-knowledge-evidence"
    candidate_value = _normal(slug)
    candidate_value.update({
        "candidate_resolution": "", "reason_human_needed": "",
        "recommended_human_resolver": "", "materiality": None, "urgency": "",
        "resolution_attempts": [], "supporting_evidence_refs": [],
        "competing_state_refs": [], "recommended_answer_options": [],
    })
    calls = {"native": 0, "router": 0, "judge": 0}

    async def native_candidate(**_kwargs):
        calls["native"] += 1
        raise AssertionError("native anthropic transport must not run for the router")

    async def router_candidate(**_kwargs):
        calls["router"] += 1
        payload = _forced_candidate_response(candidate_value)
        payload["transport"] = "swarmsync-router"
        return payload

    async def router_judge(*_args, **_kwargs):
        calls["judge"] += 1
        return _raw_judge_response(candidate_value, allow=True)

    monkeypatch.delenv("GENESIS_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("LLM_API_URL", "https://api.swarmsync.ai/v1/chat/completions")
    monkeypatch.setenv("LLM_API_KEY", "redacted-test-key")
    monkeypatch.setattr(main, "call_exact_anthropic_candidate", native_candidate)
    monkeypatch.setattr(main, "call_router_canonical_candidate", router_candidate)
    monkeypatch.setattr(main, "call_router_judge", router_judge)
    principal = Principal("test", "tenant", "client", frozenset({"agent:run"}), "test", 9999999999)
    response = await main.run_agent(
        slug, main.RunRequest(prompt="bounded", mode="live_test"), principal,
    )
    payload = json.loads(response.response)
    assert payload["ok"] is True
    assert payload["transport"] == "swarmsync-router"
    assert calls == {"native": 0, "router": 1, "judge": 1}


@pytest.mark.asyncio
async def test_canonical_live_test_unknown_host_fails_closed(monkeypatch):
    calls = {"native": 0, "router": 0}

    async def native_candidate(**_kwargs):
        calls["native"] += 1
        raise AssertionError("native transport must not run")

    async def router_candidate(**_kwargs):
        calls["router"] += 1
        raise AssertionError("router transport must not run")

    monkeypatch.delenv("GENESIS_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("LLM_API_URL", "https://router.invalid/v1/chat/completions")
    monkeypatch.setenv("LLM_API_KEY", "redacted-test-key")
    monkeypatch.setattr(main, "call_exact_anthropic_candidate", native_candidate)
    monkeypatch.setattr(main, "call_router_canonical_candidate", router_candidate)
    principal = Principal("test", "tenant", "client", frozenset({"agent:run"}), "test", 9999999999)
    with pytest.raises(main.HTTPException) as raised:
        await main.run_agent(
            "genesis-knowledge-evidence",
            main.RunRequest(prompt="bounded", mode="live_test"),
            principal,
        )
    assert raised.value.status_code == 502
    assert raised.value.detail["error"] == "output_contract_failed"
    assert raised.value.detail["reason"] == "canonical_candidate_transport_failed"
    assert raised.value.detail["stage"] == "config_rejected"
    assert raised.value.detail["errorMessage"]
    assert calls == {"native": 0, "router": 0}


@pytest.mark.asyncio
async def test_empty_run_body_is_prompt_required():
    principal = Principal("test", "tenant", "client", frozenset({"agent:run"}), "test", 9999999999)
    with pytest.raises(main.HTTPException) as raised:
        await main.run_agent(
            "genesis-data-intelligence",
            main.RunRequest(),
            principal,
        )
    assert raised.value.status_code == 422
    assert raised.value.detail["error"] == "prompt_required"


@pytest.mark.asyncio
async def test_canonical_live_test_native_route_calls_candidate_and_judge_once(monkeypatch):
    slug = "genesis-knowledge-evidence"
    candidate_value = _normal(slug)
    candidate_value.update({
        "candidate_resolution": "", "reason_human_needed": "",
        "recommended_human_resolver": "", "materiality": None, "urgency": "",
        "resolution_attempts": [], "supporting_evidence_refs": [],
        "competing_state_refs": [], "recommended_answer_options": [],
    })
    calls = {"candidate": 0, "judge": 0}

    async def candidate(**_kwargs):
        calls["candidate"] += 1
        return _forced_candidate_response(candidate_value)

    async def judge(*_args, **_kwargs):
        calls["judge"] += 1
        return _raw_judge_response(candidate_value, allow=True)

    monkeypatch.setenv("GENESIS_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_URL", "https://api.anthropic.com/v1/messages")
    monkeypatch.setenv("LLM_API_KEY", "redacted-test-key")
    monkeypatch.setattr(main, "call_exact_anthropic_candidate", candidate)
    monkeypatch.setattr(main, "call_exact_anthropic_judge", judge)
    principal = Principal("test", "tenant", "client", frozenset({"agent:run"}), "test", 9999999999)
    response = await main.run_agent(
        slug, main.RunRequest(prompt="bounded", mode="live_test"), principal,
    )
    payload = json.loads(response.response)
    assert payload["ok"] is True
    returned = json.loads(payload["response"])
    assert returned["candidate_resolution"] == ""
    assert returned["urgency"] == ""
    assert calls == {"candidate": 1, "judge": 1}
