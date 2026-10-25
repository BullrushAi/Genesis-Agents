"""A transient LLM-router outage (upstream 5xx, refused/timed-out connection) is retried; it is not a decision.

Regression for the SSCERT-30 finding: a few seconds of api.swarmsync.ai downtime turned into a permanent
"LLM evaluation unavailable - auto-rejected" of a buyer's offer on the first attempt.
"""
import httpx
import pytest
from fastapi import HTTPException

import main


class _Script:
    """Plays a fixed list of outcomes through httpx.MockTransport and counts calls."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else self.outcomes_default
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    outcomes_default = httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})


@pytest.fixture
def router(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_API_URL", "https://router.invalid/v1/chat/completions")

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)

    def install(script: _Script):
        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(script.handler)
            return real(*args, **kwargs)

        monkeypatch.setattr(main.httpx, "AsyncClient", factory)

    return install


OK = httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}], "model": "m"})


@pytest.mark.asyncio
async def test_upstream_502_then_recovery_returns_the_answer(router):
    script = _Script([httpx.Response(502, text="<html>Bad Gateway</html>"), httpx.Response(503, text="x"), OK])
    router(script)
    out = await main.call_llm_router("sys", "user")
    assert out["text"] == "hello"
    assert script.calls == 3
    assert out["_genesis_observability"]["retry_count"] == 2


@pytest.mark.asyncio
async def test_connection_refused_then_recovery_returns_the_answer(router):
    script = _Script([httpx.ConnectError("refused"), OK])
    router(script)
    out = await main.call_llm_router("sys", "user")
    assert out["text"] == "hello"
    assert script.calls == 2


@pytest.mark.asyncio
async def test_persistent_outage_fails_after_bounded_attempts_on_one_model(router):
    script = _Script([httpx.Response(502, text="down")] * 20)
    router(script)
    with pytest.raises(HTTPException) as err:
        await main.call_llm_router("sys", "user")
    assert err.value.status_code == 502
    assert script.calls == 4  # 1 + three backoffs, and the fallback models are not walked (same router)


@pytest.mark.asyncio
async def test_non_transient_error_is_not_retried(router):
    script = _Script([httpx.Response(401, text="bad key")] * 5)
    router(script)
    with pytest.raises(HTTPException):
        await main.call_llm_router("sys", "user")
    assert script.calls == 1


@pytest.mark.asyncio
async def test_negotiation_survives_a_router_blip(router, monkeypatch):
    decision = '{"status": "COUNTERED", "price": 0.05, "notes": "bounded job", "counter_budget": 0.05}'
    script = _Script([httpx.Response(502, text="down"), httpx.Response(200, json={"choices": [{"message": {"content": decision}}]})])
    router(script)
    req = main.NegotiateRequest(
        event="negotiate", negotiation_id="n-1", requester_agent_id="r", requester_agent_name="Buyer", responder_agent_id="s",
        requested_service="Vendor invoice approval controls", budget=0.05, requirements={"text": "x"}, callback_url="https://swarmsync-api-cert-staging.onrender.com/ap2/gateway/respond",
    )
    out = await main.evaluate_negotiation("genesis-fin-ap", req)
    assert out["status"] != "REJECTED" or "unavailable" not in str(out.get("notes", "")).lower()
