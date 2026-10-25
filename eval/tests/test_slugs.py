"""Slug-form handling: bundles use hyphens, the LIVE gateway uses underscores
with an ``_x402`` suffix. Sending the bundle form 404s.
"""

from __future__ import annotations

import asyncio

import pytest

from eval.genesis_client import (
    LIVE_SLUG_COUNT,
    LIVE_SLUGS,
    GenesisClient,
    MoneyDomainBlocked,
    Outcome,
    UnknownSlug,
    is_money_domain,
    resolve_live_slug,
)
from eval.tests.fakes import FakeTransport, ok_response


@pytest.mark.parametrize(
    "requested,expected",
    [
        # bundle (hyphen) form -> live form
        ("genesis-knowledge-evidence", "genesis-knowledge-evidence"),
        ("genesis-platform-engineer", "genesis-platform-engineer"),
        ("genesis-platform-engineer", "genesis-platform-engineer"),
        ("genesis-commercial-growth", "genesis-commercial-growth"),
        ("genesis-commercial-growth", "genesis-commercial-growth"),
        ("genesis-security-identity", "genesis-security-identity"),
        # already live -> unchanged
        ("genesis-knowledge-evidence", "genesis-knowledge-evidence"),
        ("genesis-execution-control", "genesis-execution-control"),
        ("genesis-legal-risk", "genesis-legal-risk"),
        # underscore-without-suffix -> suffixed live form
        ("genesis-knowledge-evidence", "genesis-knowledge-evidence"),
        # aliases that do NOT follow the pattern
        ("genesis-execution-control", "genesis-execution-control"),
        ("genesis-legal-risk", "genesis-legal-risk"),
        ("genesis-people-operations", "genesis-people-operations"),
        ("genesis-commercial-growth", "genesis-commercial-growth"),
        ("genesis-platform-engineer", "genesis-platform-engineer"),
        ("genesis-data-intelligence", "genesis-data-intelligence"),
        ("genesis-data-intelligence", "genesis-data-intelligence"),
        # live slugs that keep hyphens
        ("genesis-automation-architect", "genesis-automation-architect"),
    ],
)
def test_resolves_to_the_live_form(requested, expected):
    resolved, _ = resolve_live_slug(requested)
    assert resolved == expected
    assert resolved in LIVE_SLUGS, f"{resolved} is not a live slug"


def test_whitespace_is_stripped():
    assert resolve_live_slug("  genesis-knowledge-evidence  ")[0] == "genesis-knowledge-evidence"


def test_unknown_slug_is_best_effort_and_marked_unverified():
    resolved, resolution = resolve_live_slug("totally-made-up-agent")
    assert resolved == "totally-made-up-agent"
    assert resolution == "unverified"


def test_the_snapshot_pins_the_live_count():
    assert len(LIVE_SLUGS) == LIVE_SLUG_COUNT == 19


# ---------------------------------------------------------------------------
# Strict slugs — the silent-success failure mode
#
# An unrecognised slug does NOT 404. main.py falls back to a generic persona
# built from slug.title() and returns HTTP 200 with a bland answer. Scoring
# that would be scoring a stub, so an unverified slug is refused up front.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "typo",
    [
        "genesis-reserch",          # transposed letters
        "genesis_research_x420",    # wrong suffix
        "genesis-research-agent",   # plausible but not served
        "totally-made-up-agent",
    ],
)
def test_unknown_slug_is_refused_before_any_request(typo):
    transport = FakeTransport([ok_response("bland generic persona answer")])
    client = GenesisClient(transport=transport, api_key="k", jitter=False)
    with pytest.raises(UnknownSlug):
        asyncio.run(client.run_agent(typo, "task"))
    assert transport.run_call_count == 0, "a typo'd slug reached the gateway"


def test_the_strict_check_is_what_catches_it_not_a_404():
    """Proof the guard matters: with strict off, the gateway returns 200."""
    transport = FakeTransport([ok_response("bland generic persona answer")])
    client = GenesisClient(
        transport=transport, api_key="k", jitter=False, strict_slugs=False
    )
    result = asyncio.run(client.run_agent("genesis-reserch", "task"))
    assert result.outcome is Outcome.SUCCESS       # <- silently "fine"
    assert result.http_status == 200
    assert result.slug_resolution == "unverified"  # <- the only warning sign


@pytest.mark.parametrize("slug", ["genesis-knowledge-evidence", "genesis-platform-engineer", "genesis-platform-engineer"])
def test_known_slugs_pass_the_strict_check(slug):
    transport = FakeTransport([ok_response()])
    client = GenesisClient(transport=transport, api_key="k", jitter=False)
    result = asyncio.run(client.run_agent(slug, "task"))
    assert result.outcome is Outcome.SUCCESS
    assert result.slug_resolution in ("verified", "aliased")


def test_target_reports_an_unknown_slug_instead_of_raising():
    from eval.target import make_target

    transport = FakeTransport([ok_response()])
    target = make_target(GenesisClient(transport=transport, api_key="k", jitter=False))
    outputs = target({"slug": "genesis-reserch", "task": "t"})
    assert outputs["error_kind"] == "unknown_slug"
    assert outputs["ok"] is False
    assert transport.run_call_count == 0


def test_known_slug_is_marked_verified_or_aliased():
    assert resolve_live_slug("genesis-knowledge-evidence")[1] == "verified"
    assert resolve_live_slug("genesis-legal-risk")[1] == "verified"


def test_empty_slug_is_rejected():
    with pytest.raises(ValueError):
        resolve_live_slug("")


def test_the_request_url_uses_the_live_form():
    transport = FakeTransport([ok_response()])
    client = GenesisClient(transport=transport, api_key="k", jitter=False)
    result = asyncio.run(client.run_agent("genesis-knowledge-evidence", "task"))
    assert transport.calls[0]["url"].endswith("/agents/genesis-knowledge-evidence/run")
    assert result.slug == "genesis-knowledge-evidence"
    assert result.requested_slug == "genesis-knowledge-evidence"


# ---------------------------------------------------------------------------
# Money-domain containment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "slug",
    [
        "genesis-fin-controller", "genesis-fin-controller", "genesis-fin-controller",
        "genesis-billing", "genesis-fin-ar", "billing_agent",
        "genesis-commerce", "genesis-fin-revenue", "commerce_agent",
        "genesis-pricing", "pricing_agent",
    ],
)
def test_money_domain_agents_are_refused_before_any_request(slug):
    transport = FakeTransport([ok_response()])
    client = GenesisClient(transport=transport, api_key="k", jitter=False)
    with pytest.raises(MoneyDomainBlocked):
        asyncio.run(client.run_agent(slug, "task"))
    assert transport.run_call_count == 0, "a money-domain request reached the transport"
    assert transport.health_calls == 0


def test_non_money_agents_are_not_blocked():
    for slug in ("genesis-knowledge-evidence", "genesis-platform-engineer", "genesis-commercial-growth", "genesis-commercial-growth"):
        assert is_money_domain(resolve_live_slug(slug)[0]) is False


def test_the_block_is_overridable_explicitly():
    transport = FakeTransport([ok_response()])
    client = GenesisClient(
        transport=transport, api_key="k", jitter=False, allow_money_domain=True
    )
    result = asyncio.run(client.run_agent("genesis-fin-controller", "task"))
    assert result.slug == "genesis-fin-controller"


# ---------------------------------------------------------------------------
# Mode handling
# ---------------------------------------------------------------------------


def test_live_test_mode_sets_both_bypass_fields():
    transport = FakeTransport([ok_response()])
    client = GenesisClient(transport=transport, api_key="k", jitter=False)
    asyncio.run(client.run_agent("genesis-knowledge-evidence", "task", mode="live_test"))
    body = transport.calls[0]["json"]
    assert body["mode"] == "live_test"
    assert body["testContext"] is True
    assert body["prompt"] == "task"


def test_full_mode_sends_no_bypass_fields():
    transport = FakeTransport([ok_response()])
    client = GenesisClient(transport=transport, api_key="k", jitter=False)
    asyncio.run(client.run_agent("genesis-knowledge-evidence", "task", mode="full"))
    body = transport.calls[0]["json"]
    assert "mode" not in body
    assert "testContext" not in body


def test_structured_task_goes_to_input_not_prompt():
    transport = FakeTransport([ok_response()])
    client = GenesisClient(transport=transport, api_key="k", jitter=False)
    asyncio.run(client.run_agent("genesis-knowledge-evidence", {"topic": "agents"}))
    body = transport.calls[0]["json"]
    assert body["input"] == {"topic": "agents"}
    assert "prompt" not in body


def test_invalid_mode_is_rejected():
    client = GenesisClient(transport=FakeTransport([ok_response()]), api_key="k")
    with pytest.raises(ValueError):
        asyncio.run(client.run_agent("genesis-knowledge-evidence", "task", mode="turbo"))


def test_auth_headers_match_the_gateway_contract():
    """main.py::verify_gateway_key accepts X-Agent-Api-Key (GATEWAY_API_KEY) or
    X-Agent-Gateway-Secret (AGENT_GATEWAY_SECRET)."""
    transport = FakeTransport([ok_response()])
    client = GenesisClient(
        transport=transport, api_key="a", gateway_secret="b", jitter=False
    )
    asyncio.run(client.run_agent("genesis-knowledge-evidence", "task"))
    headers = transport.calls[0]["headers"]
    assert headers["X-Agent-Api-Key"] == "a"
    assert headers["X-Agent-Gateway-Secret"] == "b"


def test_no_credential_configured_sends_no_auth_headers(monkeypatch):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_GATEWAY_SECRET", raising=False)
    transport = FakeTransport([ok_response()])
    client = GenesisClient(transport=transport, jitter=False)
    assert client.has_credential() is False
    asyncio.run(client.run_agent("genesis-knowledge-evidence", "task"))
    headers = transport.calls[0]["headers"]
    assert "X-Agent-Api-Key" not in headers
    assert "X-Agent-Gateway-Secret" not in headers
