"""
Tests for the Conduit MCP verification engine in conduit_verifier.py.

These use a FAKE ConduitMcpSession (monkeypatched over
`conduit_mcp_client.ConduitMcpSession`) returning canned Conduit tool outputs,
so they are fast, hermetic, and never touch the network or a browser.

The real subprocess lifecycle is covered separately by test_conduit_mcp_client.py.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import conduit_mcp_client
import conduit_verifier
from conduit_mcp_client import ConduitMcpError, ConduitMcpUnavailable
from verification_models import VerificationContext, VerificationSpec

CHAIN_HASH = "a" * 64
BUNDLE_PATH = "/tmp/jobs/sess-1/conduit-proof/conduit_proof_abc_1.tar.gz"
GOOD_HASH = "b" * 64


def _context() -> VerificationContext:
    return VerificationContext(
        escrow_ref="escrow-123",
        verification_id="ver-123",
        request_id="req-123",
    )


# ---------------------------------------------------------------------------
# fake MCP session
# ---------------------------------------------------------------------------


class FakeSession:
    """Records every tool call and replays canned outputs."""

    def __init__(self, responses: dict, spawn_error: BaseException | None = None) -> None:
        self.responses = responses
        self.spawn_error = spawn_error
        self.calls: list[tuple[str, dict]] = []
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        if self.spawn_error is not None:
            raise self.spawn_error
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return False

    async def call(self, tool: str, **args):
        self.calls.append((tool, args))
        value = self.responses.get(tool, {})
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value(args)
        return value

    @property
    def tools_called(self) -> list[str]:
        return [c[0] for c in self.calls]


def _default_responses(**overrides):
    responses = {
        "conduit_navigate": {"url": "https://example.com/", "title": "Example Domain"},
        "conduit_wait_for": {"success": True, "condition": "selector", "value": "h1"},
        "conduit_extract": {"text": "Example Domain", "char_count": 14},
        "conduit_fingerprint": {"fingerprint": "f" * 64, "title": "Example Domain"},
        "conduit_screenshot": {"success": True, "path": "/tmp/shot.png"},
        "conduit_verify_deliverable": {
            "success": True,
            "actual_hash": GOOD_HASH,
            "expected_hash": "",
            "hash_match": None,
            "deliverable_verified": False,
            "verification_source": "python_fetch",
        },
        "conduit_export_proof": {
            "success": True,
            "path": BUNDLE_PATH,
            "action_count": 6,
            "chain_hash": CHAIN_HASH,
            "bundle_hash": "c" * 64,
        },
    }
    responses.update(overrides)
    return responses


@pytest.fixture(autouse=True)
def _mcp_engine(monkeypatch):
    """Default every test to the mcp engine and a neutral signing secret."""
    monkeypatch.setenv("CONDUIT_VERIFY_ENGINE", "mcp")
    monkeypatch.setattr(conduit_verifier, "_CONDUIT_INVOICE_SECRET", "test-secret")


def _install_session(monkeypatch, session: FakeSession) -> FakeSession:
    monkeypatch.setattr(
        conduit_mcp_client, "ConduitMcpSession", lambda *a, **k: session
    )
    return session


def _no_legacy(monkeypatch):
    """Make any legacy-engine call an explicit test failure."""

    async def _boom(*a, **k):
        raise AssertionError("legacy engine must NOT run here")

    monkeypatch.setattr(conduit_verifier, "_run_verification_legacy", _boom)


def _run(spec: VerificationSpec, session_id: str = "sess-1"):
    return asyncio.run(conduit_verifier.run_verification(session_id, spec, _context()))


# ---------------------------------------------------------------------------
# Track 1 — exact hash
# ---------------------------------------------------------------------------


def test_url_with_expected_hash_passes(monkeypatch):
    _no_legacy(monkeypatch)
    session = _install_session(
        monkeypatch,
        FakeSession(
            _default_responses(
                conduit_verify_deliverable={
                    "success": True,
                    "actual_hash": GOOD_HASH,
                    "expected_hash": GOOD_HASH,
                    "hash_match": True,
                    "deliverable_verified": True,
                    "verification_source": "python_fetch",
                }
            )
        ),
    )

    result = _run(
        VerificationSpec(url="https://example.com", selector="h1", expected_hash=GOOD_HASH)
    )

    assert result.passed is True
    assert result.verification_track == 1
    assert result.eval_result_hash == GOOD_HASH
    assert result.engine == "conduit-mcp"
    assert result.client_context["engine"] == "conduit-mcp"
    assert "conduit_verify_deliverable" in session.tools_called
    assert session.exited is True


def test_hash_mismatch_fails_and_does_not_fall_back(monkeypatch):
    # _no_legacy makes any fallback an outright failure.
    _no_legacy(monkeypatch)
    _install_session(
        monkeypatch,
        FakeSession(
            _default_responses(
                conduit_verify_deliverable={
                    "success": True,
                    "actual_hash": "d" * 64,
                    "expected_hash": GOOD_HASH,
                    "hash_match": False,
                    "deliverable_verified": False,
                    "verification_source": "python_fetch",
                }
            )
        ),
    )

    result = _run(VerificationSpec(url="https://example.com", expected_hash=GOOD_HASH))

    assert result.passed is False
    assert result.verification_track == 1
    assert "Hash mismatch" in (result.failure_reason or "")
    assert result.engine == "conduit-mcp"


def test_verify_deliverable_always_runs_even_without_expected_hash(monkeypatch):
    """The audit chain must always record the deliverable hash."""
    _no_legacy(monkeypatch)
    session = _install_session(monkeypatch, FakeSession(_default_responses()))

    result = _run(VerificationSpec(url="https://example.com", selector="h1"))

    assert "conduit_verify_deliverable" in session.tools_called
    assert result.eval_result_hash == GOOD_HASH
    # No expected_hash pre-commitment -> no v2 track, legacy evaluate_result decides.
    assert result.verification_track is None
    assert result.passed is True


# ---------------------------------------------------------------------------
# Track 2 — rubric
# ---------------------------------------------------------------------------


def test_inline_rubric_passes(monkeypatch):
    _no_legacy(monkeypatch)
    session = _install_session(
        monkeypatch,
        FakeSession(
            _default_responses(
                conduit_verify_rubric={
                    "success": True,
                    "rubric_pass": True,
                    "predicate_results": [{"predicate": "min_word_count", "passed": True}],
                    "word_count": 42,
                    "source": "inline",
                }
            )
        ),
    )

    result = _run(
        VerificationSpec(
            inline_content="hello world " * 30,
            rubric_json={"min_word_count": 5},
            rubric_hash="e" * 64,
        )
    )

    assert result.passed is True
    assert result.verification_track == 2
    assert result.rubric_result["rubric_pass"] is True
    # inline-only: no browser actions at all
    assert session.tools_called == ["conduit_verify_rubric", "conduit_export_proof"]
    assert [a.action for a in result.action_log] == [
        "INLINE_CONTENT",
        "VERIFY_RUBRIC",
        "EXPORT_PROOF",
    ]
    # inline_content forwarded, url omitted
    rubric_args = dict(session.calls[0][1])
    assert rubric_args["inline_content"].startswith("hello world")
    assert rubric_args.get("url") is None


def test_rubric_hash_mismatch_is_a_failed_verification_not_a_spawn_failure(monkeypatch):
    _no_legacy(monkeypatch)
    _install_session(
        monkeypatch,
        FakeSession(
            _default_responses(
                conduit_verify_rubric=ConduitMcpError(
                    "conduit_verify_rubric",
                    "rubric_hash mismatch - rubric may have been tampered",
                    {"success": False, "rubric_pass": False, "predicate_results": []},
                )
            )
        ),
    )

    result = _run(
        VerificationSpec(
            inline_content="whatever",
            rubric_json={"min_word_count": 5},
            rubric_hash="wrong",
        )
    )

    assert result.passed is False
    assert result.verification_track == 2
    assert "rubric_hash mismatch" in (result.failure_reason or "")
    assert result.engine == "conduit-mcp"


# ---------------------------------------------------------------------------
# Fallback semantics
# ---------------------------------------------------------------------------


def test_spawn_failure_falls_back_to_legacy(monkeypatch):
    _install_session(
        monkeypatch,
        FakeSession({}, spawn_error=ConduitMcpUnavailable("spawn", "mcp package unavailable")),
    )

    called: dict = {}

    async def _fake_legacy(session_id, spec, context):
        called["hit"] = True
        from verification_models import VerificationResult

        return VerificationResult(
            passed=True,
            proof_hash="legacy-hash",
            conduit_session_sig="sig",
            client_context=context.model_dump(),
        )

    monkeypatch.setattr(conduit_verifier, "_run_verification_legacy", _fake_legacy)

    result = _run(VerificationSpec(url="https://example.com"))

    assert called.get("hit") is True
    assert result.engine == "legacy-fallback"
    assert result.client_context["engine"] == "legacy-fallback"
    assert "mcp package unavailable" in result.client_context["engine_fallback_reason"]
    assert result.proof_hash == "legacy-hash"


def test_missing_server_file_falls_back(monkeypatch):
    _install_session(
        monkeypatch, FakeSession({}, spawn_error=FileNotFoundError("no server"))
    )

    async def _fake_legacy(session_id, spec, context):
        from verification_models import VerificationResult

        return VerificationResult(
            passed=False,
            proof_hash="h",
            conduit_session_sig="s",
            client_context=context.model_dump(),
        )

    monkeypatch.setattr(conduit_verifier, "_run_verification_legacy", _fake_legacy)

    result = _run(VerificationSpec(url="https://example.com"))
    assert result.engine == "legacy-fallback"


def test_legacy_engine_still_selectable(monkeypatch):
    monkeypatch.setenv("CONDUIT_VERIFY_ENGINE", "legacy")

    async def _boom_mcp(*a, **k):
        raise AssertionError("mcp engine must not run when engine=legacy")

    monkeypatch.setattr(conduit_verifier, "_run_verification_mcp", _boom_mcp)

    async def _fake_legacy(session_id, spec, context):
        from verification_models import VerificationResult

        return VerificationResult(
            passed=True,
            proof_hash="legacy",
            conduit_session_sig="s",
            client_context=context.model_dump(),
        )

    monkeypatch.setattr(conduit_verifier, "_run_verification_legacy", _fake_legacy)

    result = _run(VerificationSpec(url="https://example.com"))
    assert result.engine == "legacy"
    assert result.client_context["engine"] == "legacy"
    assert "engine_fallback_reason" not in result.client_context


def test_engine_env_resolution(monkeypatch):
    monkeypatch.delenv("CONDUIT_VERIFY_ENGINE", raising=False)
    assert conduit_verifier._verify_engine() == "mcp"
    monkeypatch.setenv("CONDUIT_VERIFY_ENGINE", "LEGACY")
    assert conduit_verifier._verify_engine() == "legacy"
    monkeypatch.setenv("CONDUIT_VERIFY_ENGINE", "nonsense")
    assert conduit_verifier._verify_engine() == "mcp"


# ---------------------------------------------------------------------------
# Action log ordering, costs, proof mapping
# ---------------------------------------------------------------------------


def test_action_log_ordering_and_costs(monkeypatch):
    _no_legacy(monkeypatch)
    _install_session(monkeypatch, FakeSession(_default_responses()))

    result = _run(VerificationSpec(url="https://example.com", selector="h1"))

    assert [a.action for a in result.action_log] == [
        "NAVIGATE",
        "WAIT_FOR",
        "EXTRACT",
        "FINGERPRINT",
        "SCREENSHOT",
        "VERIFY_DELIVERABLE",
        "EXPORT_PROOF",
    ]
    assert [a.index for a in result.action_log] == list(range(7))
    costs = {a.action: a.cost_cents for a in result.action_log}
    assert costs == {
        "NAVIGATE": conduit_verifier._ACTION_COSTS["NAVIGATE"],
        "WAIT_FOR": conduit_verifier._ACTION_COSTS["WAIT_FOR"],
        "EXTRACT": conduit_verifier._ACTION_COSTS["EXTRACT"],
        "FINGERPRINT": conduit_verifier._ACTION_COSTS["FINGERPRINT"],
        "SCREENSHOT": conduit_verifier._ACTION_COSTS["SCREENSHOT"],
        "VERIFY_DELIVERABLE": conduit_verifier._ACTION_COSTS["VERIFY_DELIVERABLE"],
        "EXPORT_PROOF": conduit_verifier._ACTION_COSTS["EXPORT_PROOF"],
    }
    assert all(a.duration_ms is not None for a in result.action_log)


def test_wait_for_skipped_without_selector(monkeypatch):
    _no_legacy(monkeypatch)
    _install_session(monkeypatch, FakeSession(_default_responses()))

    result = _run(VerificationSpec(url="https://example.com"))
    assert "WAIT_FOR" not in [a.action for a in result.action_log]


def test_proof_hash_is_the_chain_hash(monkeypatch):
    _no_legacy(monkeypatch)
    _install_session(monkeypatch, FakeSession(_default_responses()))

    result = _run(VerificationSpec(url="https://example.com", selector="h1"))

    assert result.proof_hash == CHAIN_HASH
    assert result.proof_bundle_ref == BUNDLE_PATH
    assert result.client_context["conduit_chain_hash"] == CHAIN_HASH
    assert result.client_context["conduit_bundle_path"] == BUNDLE_PATH
    assert result.client_context["conduit_commit"]
    assert result.conduit_session_sig


def test_screenshot_failure_is_ignored(monkeypatch):
    _no_legacy(monkeypatch)
    _install_session(
        monkeypatch,
        FakeSession(
            _default_responses(
                conduit_screenshot=ConduitMcpError("conduit_screenshot", "no display")
            )
        ),
    )

    result = _run(VerificationSpec(url="https://example.com", selector="h1"))

    shot = [a for a in result.action_log if a.action == "SCREENSHOT"][0]
    assert shot.success is False
    assert result.passed is True  # screenshot never fails the verification


def test_navigate_failure_fails_verification(monkeypatch):
    _no_legacy(monkeypatch)
    _install_session(
        monkeypatch,
        FakeSession(
            _default_responses(
                conduit_navigate=ConduitMcpError("conduit_navigate", "net::ERR_FAILED"),
                conduit_extract={"text": ""},
                conduit_fingerprint={},
            )
        ),
    )

    result = _run(VerificationSpec(url="https://example.com"))

    assert result.passed is False
    assert result.engine == "conduit-mcp"


def test_expected_content_still_evaluated_via_evaluate_result(monkeypatch):
    _no_legacy(monkeypatch)
    _install_session(monkeypatch, FakeSession(_default_responses()))

    ok = _run(
        VerificationSpec(
            url="https://example.com", selector="h1", expectedContent="Example Domain"
        )
    )
    assert ok.passed is True

    bad = _run(
        VerificationSpec(
            url="https://example.com", selector="h1", expectedContent="Not On The Page"
        )
    )
    assert bad.passed is False
    assert "Expected content not found" in (bad.failure_reason or "")


def test_session_signature_covers_engine_and_chain(monkeypatch):
    _no_legacy(monkeypatch)
    _install_session(monkeypatch, FakeSession(_default_responses()))

    result = _run(VerificationSpec(url="https://example.com"))

    expected = conduit_verifier._sign_proof(
        json.dumps(
            {
                "engine": "conduit-mcp",
                "conduit_bundle_path": BUNDLE_PATH,
                "chain_hash": CHAIN_HASH,
                "action_count": 6,
            },
            sort_keys=True,
        )
    )
    assert result.conduit_session_sig == expected


def test_export_proof_targets_the_job_directory(monkeypatch, tmp_path):
    _no_legacy(monkeypatch)
    monkeypatch.setenv("GENESIS_JOBS_ROOT", str(tmp_path))
    session = _install_session(monkeypatch, FakeSession(_default_responses()))

    _run(VerificationSpec(url="https://example.com"), session_id="job-77")

    export_args = [a for t, a in session.calls if t == "conduit_export_proof"][0]
    assert export_args["output_path"].endswith("conduit-proof")
    assert "job-77" in export_args["output_path"]
    # both spellings sent (Conduit reads output_dir, its schema advertises output_path)
    assert export_args["output_dir"] == export_args["output_path"]
    assert (tmp_path / "job-77" / "conduit-proof").is_dir()
