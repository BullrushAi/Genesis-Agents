from pathlib import Path

from fastapi import HTTPException

import json

import pytest

from main import RunRequest, _llm_api_key, _llm_api_url, _raise_for_runtime_failure, run_agent


def _test_principal():
    """run_agent resolves its principal via Depends() under FastAPI.

    These tests call the handler directly, so they must supply the principal
    the dependency would have produced. Passing the AP2 identity (not the
    legacy gateway one) keeps the owner-scoping path under test.
    """
    from runtime.request_auth import Principal

    return Principal(
        principal_id="service:cato",
        tenant_id="e4l",
        client_id="cato",
        scopes=frozenset({"agent.invoke", "job.read"}),
        auth_method="ap2",
        expires_at=0,
    )

_REPO_ROOT = Path(__file__).resolve().parent
_MAIN_PY = _REPO_ROOT / "main.py"
_TOOLS_DIR = _REPO_ROOT / "tools"

# Direct LLM-provider endpoints. Any Genesis code path that hits these bypasses
# the SwarmSync router (no routing metadata, cost tracking, or tier selection).
_DIRECT_LLM_HOSTS = (
    "generativelanguage.googleapis.com",
    "api.openai.com",
    "api.anthropic.com",
    "openrouter.ai/api",
    "api.x.ai",
    "api.groq.com",
)

# Known, intentional direct-provider exceptions (NOT on the SwarmSync router).
# Each entry is a file that is allowed to call a provider directly today.
# Adding a NEW bypass — or removing one of these without migrating it to the
# router — must fail this regression so the decision is explicit.
#   - vision_tool.py: GPT-4o vision API (no SwarmSync vision route yet). Tracked
#     for router migration; gated behind OPENAI_API_KEY and only reachable via
#     the vision agent's tool calls, never the persona/negotiate path.
# Files permitted to name an LLM provider host directly. Each entry is a deliberate decision,
# not an oversight — the guard exists so a bypass cannot be added silently.
#
#   vision_tool.py  — pre-existing: OpenAI vision has no SwarmSync equivalent.
#   agent_runtime.py — OPERATOR DECISION (2026-08-12): the SwarmSync gateway key is dead
#       (401 "Invalid or disabled API key" from api.swarmsync.ai), so no agent could run at all.
#       Rather than rotate a key we do not control, Genesis can now speak the Anthropic Messages
#       API directly, selected by GENESIS_LLM_PROVIDER (or an api.anthropic.com URL). The
#       SwarmSync/OpenAI path is UNCHANGED and remains the documented default in .env.example —
#       this is an added provider, not a bypass of routing. The single occurrence in
#       agent_runtime.py is the docstring for _is_anthropic; the wire format itself lives in
#       runtime/anthropic_wire.py.
_KNOWN_DIRECT_LLM_FILES = {"vision_tool.py", "agent_runtime.py"}


def test_runtime_failure_raises_non_200_status():
    try:
        _raise_for_runtime_failure(
            {
                "ok": False,
                "error": "llm_call_failed",
                "message": 'LLM HTTP 429: {"error":"TOO_MANY_REQUESTS"}',
            },
            "genesis-test",
        )
    except HTTPException as exc:
        assert exc.status_code == 429
        assert exc.detail["agentSlug"] == "genesis-test"
        assert exc.detail["error"] == "llm_call_failed"
    else:
        raise AssertionError("expected HTTPException")


def test_timeout_maps_to_gateway_timeout():
    try:
        _raise_for_runtime_failure({"ok": False, "error": "timeout"}, "genesis-test")
    except HTTPException as exc:
        assert exc.status_code == 504
    else:
        raise AssertionError("expected HTTPException")


def test_llm_router_defaults_to_swarmsync(monkeypatch):
    monkeypatch.delenv("LLM_API_URL", raising=False)

    assert _llm_api_url() == "https://api.swarmsync.ai/v1/chat/completions"


def test_openrouter_key_is_not_default_genesis_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("GENESIS_ALLOW_OPENROUTER_FALLBACK", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    assert _llm_api_key() == ""


def test_openrouter_key_never_used_even_with_legacy_flag(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("GENESIS_ALLOW_OPENROUTER_FALLBACK", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    assert _llm_api_key() == ""


def test_main_py_has_no_direct_google_generative_language_api():
    text = _MAIN_PY.read_text(encoding="utf-8")
    assert "generativelanguage.googleapis.com" not in text
    assert "call_gemini_fallback" not in text
    assert "GEMINI_API_KEY" not in text


def test_no_direct_llm_provider_calls_outside_known_allowlist():
    """Lock the LLM-call surface: persona/negotiate (main.py) and every tool
    must route through SwarmSync, except the explicitly allowlisted files."""
    scanned = [_MAIN_PY, _REPO_ROOT / "agent_runtime.py", *sorted(_TOOLS_DIR.glob("*.py"))]

    offenders: dict[str, list[str]] = {}
    for path in scanned:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        hits = [host for host in _DIRECT_LLM_HOSTS if host in text]
        if hits and path.name not in _KNOWN_DIRECT_LLM_FILES:
            offenders[path.name] = hits

    assert not offenders, (
        f"New direct LLM-provider bypass(es) detected (must route via SwarmSync "
        f"or be added to _KNOWN_DIRECT_LLM_FILES with justification): {offenders}"
    )


def test_known_direct_llm_exceptions_still_exist():
    """Guard the allowlist against silent drift: if a known direct-provider file
    is removed or migrated to the router, update _KNOWN_DIRECT_LLM_FILES so the
    'no bypass' claim stays accurate."""
    # Resolve against the SAME surface the bypass scan walks (repo root + tools/), not tools/
    # alone — otherwise an allowlisted root-level file like agent_runtime.py reads as "missing"
    # and the guard fails for the wrong reason.
    for name in _KNOWN_DIRECT_LLM_FILES:
        candidates = [_TOOLS_DIR / name, _REPO_ROOT / name]
        path = next((p for p in candidates if p.exists()), candidates[0])
        assert path.exists(), f"allowlisted bypass file missing: {name} — update _KNOWN_DIRECT_LLM_FILES"
        text = path.read_text(encoding="utf-8")
        assert any(host in text for host in _DIRECT_LLM_HOSTS), (
            f"{name} no longer calls a direct provider — remove it from "
            f"_KNOWN_DIRECT_LLM_FILES so the bypass allowlist stays honest"
        )


@pytest.mark.asyncio
async def test_call_llm_router_targets_swarmsync_only(monkeypatch):
    import main

    captured: dict = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": "ok"}}],
                "swarmsync": {"routed_model": "anthropic/claude-haiku-4-5", "tier": "economy"},
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setenv("LLM_API_KEY", "sk-ss-test-key")
    monkeypatch.setenv("AGENT_GATEWAY_SECRET", "gateway-secret-test")
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)

    result = await main.call_llm_router("system", "user")

    assert "generativelanguage.googleapis.com" not in captured["url"]
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer sk-ss-test-key"
    assert captured["headers"]["X-Title"] == "SwarmSync Agent Gateway"
    assert captured["headers"]["X-Agent-Gateway-Secret"] == "gateway-secret-test"
    assert result["swarmsync"]["routed_model"]


@pytest.mark.asyncio
@pytest.mark.parametrize("request_slug", [
    "genesis_builder_x402", "genesis_deploy_x402",
    "genesis_qa_x402", "genesis_research_x402",
])
async def test_retired_conduit_aliases_are_explicitly_deprecated(request_slug):
    with pytest.raises(HTTPException) as exc:
        await run_agent(
            request_slug,
            RunRequest(prompt="must not execute", task={"scope": "smoke"}),
            principal=_test_principal(),
        )
    assert exc.value.status_code == 410
    assert exc.value.detail["error"] == "legacy_agent_deprecated"


@pytest.mark.asyncio
async def test_hr_alias_is_explicitly_deprecated():
    with pytest.raises(HTTPException) as exc:
        await run_agent(
            "genesis_hr_x402", RunRequest(prompt="must not execute", mode="live_test"),
            principal=_test_principal(),
        )
    assert exc.value.status_code == 410


@pytest.mark.asyncio
async def test_onboarding_alias_is_explicitly_deprecated():
    with pytest.raises(HTTPException) as exc:
        await run_agent(
            "onboarding_agent", RunRequest(prompt="must not execute", mode="live_test"),
            principal=_test_principal(),
        )
    assert exc.value.status_code == 410
