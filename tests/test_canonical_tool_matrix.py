from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent_runtime
from agent_runtime import AgentRuntime
from bundle_loader import load_bundle
from canonical_result_contract import CANONICAL_RESULT_TOOL_NAME
from runtime.tool_policy import HUMAN_ONLY_TOOLS, PROHIBITED_TOOLS, check_tool_policy, is_prohibited
from tools import get_tool, register_default_tools, tool_schemas_for


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = json.loads((ROOT / "proof-artifacts/system-inventory/system_inventory.json").read_text(encoding="utf-8"))
DECISION = json.loads((ROOT / "proof-artifacts/canonical-tool-policy-decision/policy_matrix.json").read_text(encoding="utf-8"))


def _canonical_master_result(slug: str) -> dict:
    return {
        "agent_id": slug,
        "task_id": "forced-provider-result",
        "scope": "bounded evidence review",
        "status": "ANALYSIS_COMPLETE",
        "summary": "ANALYSIS: Bounded analysis produced the requested evidence assessment.",
        "findings": ["ANALYSIS: Evidence was assessed within the requested scope."],
        "proposals": [],
        "evidence_refs": ["request"],
        "assumptions": [],
        "conflicts": [],
        "unknowns": [],
        "confidence": 0.9,
        "risk_flags": [],
        "recommended_next_actions": [],
        "requested_human_decisions": [],
        "tool_calls_summary": [],
        "clarification": None,
    }


def _tool_case(example_id: str) -> dict:
    for line in (ROOT / "eval/canonical/genesis_tool_matrix.jsonl").read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        if case["example_id"] == example_id:
            return case
    raise AssertionError(f"missing tool case: {example_id}")


def test_inventory_tool_matrix_matches_real_policy_and_model_exposure() -> None:
    register_default_tools()
    assert len(DECISION["agents"]) == len({row["slug"] for row in DECISION["agents"]}) == 19
    for agent in DECISION["agents"]:
        slug = agent["slug"]
        bundle = load_bundle(slug)
        advertised = bundle["tools_advertised"]
        actual_effective = [name for name in advertised if check_tool_policy(slug, name)["ok"]]
        assert advertised == agent["advertised_target"]
        assert actual_effective == agent["allowed_effective_target"]
        schemas = tool_schemas_for(actual_effective)
        assert [s["function"]["name"] for s in schemas] == actual_effective
        assert all(get_tool(name) is not None for name in actual_effective)


@pytest.mark.asyncio
async def test_direct_anthropic_canonical_result_tool_is_consumed_not_dispatched(
    monkeypatch, tmp_path
) -> None:
    bundle = load_bundle("genesis-knowledge-evidence")
    captured: list[list[dict]] = []
    calls = 0

    async def forced_result(_self, _model, _messages, tools, _max_tokens):
        nonlocal calls
        calls += 1
        captured.append(tools)
        return {
                "model": "claude-haiku-4-5-20251001",
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "reserved-result",
                        "type": "function",
                        "function": {
                            "name": CANONICAL_RESULT_TOOL_NAME,
                            "arguments": json.dumps(_canonical_master_result(bundle["slug"])),
                        },
                    }],
                },
            }],
            "usage": {"total_tokens": 10},
        }

    monkeypatch.setattr(AgentRuntime, "_call_llm", forced_result)
    async def allow_judge(*_args, **_kwargs):
        from runtime.semantic_safety_judge import SafetyJudgeVerdict
        return SafetyJudgeVerdict(True, None, "ALLOW", "NONE", (), "a" * 64, "b" * 64, 100, "claude-haiku-4-5-20251001", 10, 2, 0.00002, "estimated", "anthropic-public-2025-11-01-v1")
    monkeypatch.setattr(agent_runtime, "judge_candidate_semantics", allow_judge)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        agent_runtime,
        "get_tool",
        lambda _name: pytest.fail("reserved result tool reached business dispatcher"),
    )
    result = await AgentRuntime("https://api.anthropic.com", "redacted")._run_loop(
        bundle, "assess evidence", {}, "job-forced", tmp_path, None, "session-forced"
    )
    assert result["ok"] is True
    assert calls == 1
    assert [tool["name"] for tool in captured[0]] == [CANONICAL_RESULT_TOOL_NAME]
    assert result["trace"]["tool_calls"] == []
    assert result["resource_usage"]["llm_calls"] == 1


@pytest.mark.asyncio
async def test_direct_anthropic_missing_result_tool_fails_once_without_dispatch_or_content(
    monkeypatch, tmp_path
) -> None:
    bundle = load_bundle("genesis-knowledge-evidence")
    calls = 0

    async def missing_result(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "provider fallback must not escape", "tool_calls": []},
            }],
            "usage": {"total_tokens": 2},
        }

    monkeypatch.setattr(AgentRuntime, "_call_llm", missing_result)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    result = await AgentRuntime("https://api.anthropic.com", "redacted")._run_loop(
        bundle, "assess evidence", {}, "job-missing", tmp_path, None, "session-missing"
    )
    assert result["ok"] is False
    assert result["error"] == "output_contract_failed"
    assert result["response"] == ""
    assert result["output_contract_eval"]["reason"] == "canonical_result_text_fallback"
    assert result["trace"]["tool_calls"] == []
    # Phase 2 mirrors phase 1's corrective retry into this path: the first
    # rejection is retried exactly once with validator feedback, so a model
    # that never submits the result tool is called twice, still fails with
    # identical metadata, and nothing is dispatched or leaked either time.
    assert calls == result["resource_usage"]["llm_calls"] == 2


def test_wrong_unnecessary_and_prohibited_tools_are_fail_closed_for_all_24() -> None:
    register_default_tools()
    for agent in DECISION["agents"]:
        slug = agent["slug"]
        advertised = set(agent["advertised_target"])
        assert check_tool_policy(slug, "definitely_unknown_tool")["ok"] is False
        for name in PROHIBITED_TOOLS:
            assert is_prohibited(name, slug)
            assert check_tool_policy(slug, name)["ok"] is False
            assert get_tool(name) is None
        for name in HUMAN_ONLY_TOOLS:
            decision = check_tool_policy(slug, name)
            assert decision["ok"] is False
            assert decision["error"] == "human_action_required"
        assert {name for name in advertised if check_tool_policy(slug, name)["ok"]} == set(
            agent["allowed_effective_target"]
        )


def test_tool_argument_schema_is_closed_over_required_and_enum_constraints() -> None:
    register_default_tools()
    schemas = {s["function"]["name"]: s["function"]["parameters"] for s in tool_schemas_for(["code_format"])}
    code = schemas["code_format"]
    assert code["required"] == ["content"]
    assert set(code["properties"]["language"]["enum"]) == {"python", "javascript", "typescript", "json", "yaml"}
    assert "content" in code["properties"]


def test_platform_positive_cases_declare_external_readiness_gates() -> None:
    formatter = _tool_case("genesis-platform-engineer::tool::code_format::positive")
    execution = _tool_case("genesis-platform-engineer::tool::run_code::positive")

    assert formatter["metadata"]["execution_readiness"] == {
        "capability": "code_format",
        "required_provider": "black",
        "local_policy": "external_gated_when_unavailable",
        "release_blocker_for_claimed_support": True,
    }
    assert execution["metadata"]["execution_readiness"] == {
        "capability": "run_code",
        "required_provider": "bwrap",
        "local_policy": "external_gated_when_unavailable",
        "release_blocker_for_claimed_support": True,
    }


def test_eval_environment_reproducibly_declares_black_formatter() -> None:
    requirements = (ROOT / "eval/requirements.txt").read_text(encoding="utf-8").splitlines()
    declared = [line.strip() for line in requirements if line.strip().lower().startswith("black")]
    assert declared == ["black>=24.10,<26.0"]


def test_formatter_readiness_is_external_gated_when_black_is_missing(monkeypatch) -> None:
    from tools import sandbox_tool

    monkeypatch.setattr(sandbox_tool.shutil, "which", lambda name: None)

    assert sandbox_tool.code_format_readiness() == {
        "capability": "code_format",
        "required_provider": "black",
        "status": "external_gated",
        "available": False,
        "release_blocker_for_claimed_support": True,
        "evidence": {
            "provider_present": False,
            "reason": "black_not_installed",
        },
    }


@pytest.mark.asyncio
async def test_policy_denied_advertised_tool_does_not_make_safe_no_tool_answer_impossible(monkeypatch, tmp_path) -> None:
    """An optional, allowed finance artifact tool must not force a write."""
    bundle = load_bundle("genesis-fin-ap")
    canonical_answer = {
        "agent_id": "genesis-fin-ap",
        "task_id": "safe-no-tool",
        "scope": "bounded AP assessment",
        "status": "HOLD",
        "summary": "ANALYSIS: proposal only",
        "findings": [],
        "proposals": [],
        "evidence_refs": [],
        "assumptions": [],
        "conflicts": [],
        "unknowns": [],
        "confidence": 0.0,
        "risk_flags": [],
        "recommended_next_actions": [],
        "requested_human_decisions": [],
        "tool_calls_summary": [],
        "clarification": None,
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
            "requires_human_decision": False,
            "recommended_financeos_action": None,
        },
    }

    async def final_answer(*_a, **_k):
        return {
                "model": "claude-haiku-4-5-20251001",
            "choices": [{"finish_reason": "tool_calls", "message": {
                "content": None,
                "tool_calls": [{"id": "result", "type": "function", "function": {
                    "name": CANONICAL_RESULT_TOOL_NAME,
                    "arguments": json.dumps(canonical_answer),
                }}],
            }}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    async def allow_judge(*_a, **_k):
        from runtime.semantic_safety_judge import SafetyJudgeVerdict
        return SafetyJudgeVerdict(
            True, None, "ALLOW", "NONE", (), "a" * 64, "b" * 64, 100,
                "claude-haiku-4-5-20251001", 10, 2, 0.00002, "estimated",
            "anthropic-public-2025-11-01-v1",
        )

    monkeypatch.setenv("GENESIS_LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(AgentRuntime, "_call_llm", final_answer)
    monkeypatch.setattr(agent_runtime, "judge_candidate_semantics", allow_judge)
    monkeypatch.setattr(agent_runtime, "emit_event", lambda *_a, **_k: None)
    assert bundle["tools_advertised"] == ["file_write"]
    assert check_tool_policy(bundle["slug"], "file_write")["ok"] is True
    result = await AgentRuntime("https://api.anthropic.com/v1/messages", "redacted")._run_loop(
        bundle, "assess only", {}, "job-no-tool", tmp_path, None, "session-no-tool"
    )
    assert result["ok"] is True
    assert result["success_criteria_eval"] == {"ok": True, "failed": []}
    assert result["trace"]["tool_calls"] == []


@pytest.mark.asyncio
async def test_file_write_confines_paths_and_rejects_symlink_escape(tmp_path) -> None:
    from tools.file_write_tool import file_write

    job = tmp_path / "job"
    job.mkdir()
    sibling = tmp_path / "job-evil"
    sibling.mkdir()
    assert (await file_write(path="nested/report.md", content="ok", _job_dir=job))["ok"] is True
    for path in ("../escape.txt", str(tmp_path / "absolute.txt"), "../job-evil/prefix.txt"):
        assert (await file_write(path=path, content="blocked", _job_dir=job))["ok"] is False
    link = job / "outside"
    try:
        link.symlink_to(sibling, target_is_directory=True)
    except OSError:
        import sys
        if sys.platform != "win32":
            raise
        # Windows junctions exercise the same real directory escape without
        # requiring the host's symbolic-link privilege.
        import _winapi
        _winapi.CreateJunction(str(sibling), str(link))
    assert link.resolve() == sibling.resolve()
    assert (await file_write(path="outside/escape.txt", content="blocked", _job_dir=job))["ok"] is False
    assert not (sibling / "escape.txt").exists()


@pytest.mark.asyncio
async def test_run_code_refuses_when_bwrap_is_unavailable(monkeypatch, tmp_path) -> None:
    import tools.sandbox_tool as sandbox_tool
    from runtime import sandbox_manager

    monkeypatch.setattr(sandbox_manager, "_bwrap_works", lambda: False)
    monkeypatch.setattr(sandbox_tool, "run_in_sandbox", sandbox_manager.run_in_sandbox)
    result = await sandbox_tool.run_code(code="print('must not run')", _job_dir=tmp_path)
    assert result["ok"] is False
    assert result["error"] == "secure_sandbox_unavailable"
    assert result["sandbox_required"] == "bwrap"


@pytest.mark.asyncio
async def test_xero_missing_bridge_fails_closed_without_consuming_idempotency_key(tmp_path, monkeypatch) -> None:
    """create_draft_bill is a write operation. Under GENESIS NEVER WRITES XERO
    (2026-08-27) it is now refused structurally before XERO_MCP_BRIDGE_URL is
    ever read -- so both calls return genesis_write_disabled rather than
    xero_bridge_not_configured, on every repeat, and the idempotency key is
    still never consumed (there is nothing to deduplicate: nothing executed)."""
    import tools.xero_scoped_tool as xero

    monkeypatch.setattr(xero, "XERO_MCP_BRIDGE_URL", "")
    kwargs = dict(
        operation="create_draft_bill",
        verify_api_run_id="verify-api-run-12345678",
        # Exercise the real scoped implementation with its existing legacy
        # allowlist. Canonical finance agents remain proposal-only and do not
        # advertise this execution tool.
        _agent_slug="genesis-e4l-ap",
        _allowed_xero_operations=["create_draft_bill"],
        _job_dir=tmp_path,
    )
    first = await xero.xero_scoped_invoke(**kwargs)
    second = await xero.xero_scoped_invoke(**kwargs)
    for result in (first, second):
        assert result["ok"] is False
        assert result["error"] == "genesis_write_disabled"
        assert result["operation"] == "create_draft_bill"
        assert result["verify_api_run_id"] == "verify-api-run-12345678"
        assert "receipt" not in result
        assert "simulated" not in result
    assert second["error"] != "duplicate_verify_api_run"
