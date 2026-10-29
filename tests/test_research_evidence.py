"""Research jobs gather real sources, cite them, and decline when they can't (live defect, negotiation fc83b663).

The search and fetch tools are replaced at their call boundary; the provider loop, prompt, citation check and
withholding decision under test are real code.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import research_evidence as rev  # noqa: E402
from tests.test_ap2_provider import FakeMarketplace, cred, env, key, neg  # noqa: E402,F401
import ap2_provider as ap  # noqa: E402

PAGE = "Automated bookkeeping adoption among small firms depends on data quality and review effort. " * 10
RESEARCH_SLUG = "genesis-knowledge-evidence"


def run(coro):
    return asyncio.run(coro)


def fake_search(urls: list[str]):
    async def search(**kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "results": [{"title": f"Source {i}", "url": u, "snippet": ""} for i, u in enumerate(urls)]}

    return search


async def ok_fetch(**kwargs: Any) -> dict[str, Any]:
    return {"ok": True, "url": kwargs["url"], "status_code": 200, "text": PAGE}


def sources(n: int) -> list[rev.Source]:
    return [rev.Source(url=f"https://example.org/report-{i}", title=f"Report {i}", text=PAGE) for i in range(1, n + 1)]


class TestNeedsEvidence:
    def test_research_and_evidence_agents_always_cite(self):
        assert rev.needs_evidence(RESEARCH_SLUG, "Summarise this text")
        assert rev.needs_evidence("research-buyer", "anything")

    def test_any_agent_cites_when_the_buyer_asks_for_research_or_sources(self):
        assert rev.needs_evidence("genesis-fin-ap", "Short research summary of AP automation")
        assert rev.needs_evidence("genesis-fin-ap", "Please cite your sources")

    def test_ordinary_jobs_do_not(self):
        assert not rev.needs_evidence("genesis-fin-ap", "Summarise: SwarmSync is an open agent marketplace.")

    def test_source_code_and_data_source_jobs_are_not_research(self):
        for job in ("Review this source code", "Audit our open source licence", "Map the data source fields",
                    "Add a reference to the config", "Cite the line numbers you changed"):
            assert not rev.needs_evidence("genesis-code-reviewer", job), job


class TestGatherSources:
    def test_keeps_readable_pages_up_to_the_cap(self):
        urls = [f"https://example.org/{i}" for i in range(8)]
        got = run(rev.gather_sources("bookkeeping adoption", fake_search(urls), ok_fetch))
        assert [s.url for s in got] == urls[: rev.MAX_SOURCES]
        assert all(len(s.text) <= rev.SOURCE_CHARS for s in got)

    def test_drops_failed_short_and_error_pages(self):
        async def fetch(**kwargs: Any) -> dict[str, Any]:
            url = kwargs["url"]
            if url.endswith("/0"):
                return {"ok": False, "error": "ssrf_blocked"}
            if url.endswith("/1"):
                return {"ok": True, "status_code": 404, "text": PAGE}
            if url.endswith("/2"):
                return {"ok": True, "status_code": 200, "text": "too short"}
            return {"ok": True, "status_code": 200, "text": PAGE}

        urls = [f"https://example.org/{i}" for i in range(4)]
        assert [s.url for s in run(rev.gather_sources("q", fake_search(urls), fetch))] == ["https://example.org/3"]

    def test_a_search_outage_is_no_evidence_not_a_crash(self):
        async def broken(**kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("search down")

        assert run(rev.gather_sources("q", broken, ok_fetch)) == []
        assert run(rev.search_hits("", fake_search(["https://example.org/a"]))) == []

    def test_non_http_results_are_ignored(self):
        hits = run(rev.search_hits("q", fake_search(["javascript:alert(1)", "/relative", "https://example.org/ok"])))
        assert [h["url"] for h in hits] == ["https://example.org/ok"]


class TestCitations:
    def test_counts_a_source_only_when_marked_and_listed(self):
        srcs = sources(3)
        out = (
            "Adoption depends on data quality [1]. Review effort matters [2].\n\nSources\n"
            f"[1] Report 1 - {srcs[0].url}\n[2] Report 2 - {srcs[1].url}"
        )
        assert rev.cited_sources(out, srcs) == [srcs[0].url, srcs[1].url]
        assert rev.cited_sources("Claims with [1] [2] but no URLs.", srcs) == []

    def test_a_bare_sources_list_is_not_a_citation(self):
        srcs = sources(2)
        out = f"Findings with no inline markers.\n\nSources\n[1] {srcs[0].url}\n[2] {srcs[1].url}"
        assert rev.cited_sources(out, srcs) == []

    def test_a_url_prefix_does_not_count_as_the_shorter_source(self):
        srcs = [rev.Source(url="https://example.org/a", title="A", text=PAGE),
                rev.Source(url="https://example.org/ab", title="AB", text=PAGE)]
        out = "Claim one [1]. Claim two [2].\n\nSources\n[2] https://example.org/ab"
        assert rev.cited_sources(out, srcs) == ["https://example.org/ab"]

    def test_prompt_carries_numbered_sources_and_the_citation_rule(self):
        prompt = rev.evidence_prompt("Service requested: research summary", sources(2))
        assert "[1] Report 1" in prompt and "URL: https://example.org/report-2" in prompt
        assert "Mark every factual claim with its source number" in prompt


def research_job() -> dict[str, Any]:
    return neg(
        responderAgent={"id": "11111111-1111-4111-8111-111111111111"},
        requestedService="short research summary of automated bookkeeping adoption",
        requirements={"text": "Summarise adoption barriers for small businesses."},
    )


def research_provider(market: FakeMarketplace, executor, gatherer) -> ap.Ap2Provider:
    seller = cred(slug=RESEARCH_SLUG)
    return ap.Ap2Provider([seller], executor, key(), "runtime-sha", market.client, source_gatherer=gatherer)


class TestResearchDelivery:
    def test_writes_from_sources_and_delivers_with_citations(self):
        srcs = sources(3)
        prompts: list[str] = []

        async def executor(slug: str, prompt: str) -> str:
            prompts.append(prompt)
            return f"Barriers are data quality [1] and review effort [2].\n\nSources\n[1] {srcs[0].url}\n[2] {srcs[1].url}"

        async def gatherer(query: str) -> list[rev.Source]:
            assert "automated bookkeeping adoption" in query
            return srcs

        market = FakeMarketplace()
        market.negotiations = [research_job()]
        p = research_provider(market, executor, gatherer)
        run(p.tick())
        assert len(market.delivered) == 1
        assert "SOURCES (retrieved for this job" in prompts[0]
        assert srcs[0].url in market.delivered[0]["result"]["output"]
        assert p.ledger["neg-1"]["cited_sources"] == 2

    def test_no_sources_means_no_delivery_and_no_retry_loop(self):
        calls: list[str] = []

        async def executor(slug: str, prompt: str) -> str:
            calls.append(prompt)
            return "unverified opinion"

        async def gatherer(query: str) -> list[rev.Source]:
            return sources(1)

        market = FakeMarketplace()
        market.negotiations = [research_job()]
        p = research_provider(market, executor, gatherer)
        run(p.tick())
        run(p.tick())
        assert market.delivered == [] and calls == []
        assert p.ledger["neg-1"]["evidence_blocked"] == "no usable sources found"

    def test_an_uncited_draft_is_rewritten_once_then_withheld(self):
        srcs = sources(3)
        calls: list[str] = []

        async def executor(slug: str, prompt: str) -> str:
            calls.append(prompt)
            return "Findings with no citations at all."

        async def gatherer(query: str) -> list[rev.Source]:
            return srcs

        market = FakeMarketplace()
        market.negotiations = [research_job()]
        p = research_provider(market, executor, gatherer)
        run(p.tick())
        assert market.delivered == []
        assert len(calls) == 2 and "CORRECTION REQUIRED" in calls[1]
        assert p.ledger["neg-1"]["evidence_blocked"] == "output did not cite its sources"

    def test_a_rewritten_draft_that_cites_is_delivered(self):
        srcs = sources(2)
        drafts = iter([
            "No citations.",
            f"Point one [1]. Point two [2].\n\nSources\n[1] {srcs[0].url}\n[2] {srcs[1].url}",
        ])

        async def executor(slug: str, prompt: str) -> str:
            return next(drafts)

        async def gatherer(query: str) -> list[rev.Source]:
            return srcs

        market = FakeMarketplace()
        market.negotiations = [research_job()]
        p = research_provider(market, executor, gatherer)
        run(p.tick())
        assert len(market.delivered) == 1
        assert p.ledger["neg-1"]["citation_regenerated"] is True

    def test_ordinary_jobs_skip_the_evidence_step(self):
        async def executor(slug: str, prompt: str) -> str:
            return f"{slug} did it"

        async def gatherer(query: str) -> list[rev.Source]:
            raise AssertionError("an ordinary job must not search")

        market = FakeMarketplace()
        market.negotiations = [neg()]
        p = ap.Ap2Provider([cred()], executor, key(), "runtime-sha", market.client, source_gatherer=gatherer)
        run(p.tick())
        assert len(market.delivered) == 1


class TestNegotiationDecline:
    """An offer the agent can't evidence is declined before any money is held."""

    def _request(self, service: str):
        import main

        return main.NegotiateRequest(
            event="negotiation.created", negotiation_id="neg-1", requester_agent_id="r-1",
            requester_agent_name="Research Buyer", responder_agent_id="s-1", requested_service=service,
            budget=0.2, callback_url="https://api.swarmsync.ai/ap2/gateway/respond",
        )

    def test_declines_research_with_nothing_to_cite_without_calling_the_llm(self, monkeypatch):
        import main

        async def no_hits(query, search=None, fetch=None):
            return []

        async def llm(*args, **kwargs):
            raise AssertionError("the LLM must not be asked to accept unevidenced research")

        monkeypatch.setattr(rev, "gather_sources", no_hits)
        monkeypatch.setattr(main, "call_llm_router", llm)
        decision = run(main.evaluate_negotiation(RESEARCH_SLUG, self._request("short research summary")))
        assert decision["status"] == "REJECTED"
        assert decision["notes"] == rev.DECLINE_NOTE

    def test_search_hits_that_cannot_be_read_still_decline(self, monkeypatch):
        """Delivery needs readable pages, so the negotiation probe does too; bare hits would strand escrow."""
        import main

        async def llm(*args, **kwargs):
            raise AssertionError("must decline before the LLM")

        async def unreadable(**kwargs):
            return {"ok": True, "status_code": 403, "text": PAGE}

        monkeypatch.setattr(rev, "_default_search", fake_search(["https://example.org/a", "https://example.org/b"]))
        monkeypatch.setattr(rev, "_default_fetch", unreadable)
        monkeypatch.setattr(main, "call_llm_router", llm)
        decision = run(main.evaluate_negotiation(RESEARCH_SLUG, self._request("short research summary")))
        assert decision["status"] == "REJECTED"

    def test_research_with_sources_goes_to_the_llm(self, monkeypatch):
        import main

        async def hits(query, search=None, fetch=None):
            return sources(2)

        async def llm(system, prompt):
            return '{"status": "ACCEPTED", "price": 0.2, "notes": "ok"}'

        monkeypatch.setattr(rev, "gather_sources", hits)
        monkeypatch.setattr(main, "call_llm_router", llm)
        monkeypatch.setattr(main, "_router_result_payload", lambda raw: (raw, {}), raising=False)
        decision = run(main.evaluate_negotiation(RESEARCH_SLUG, self._request("short research summary")))
        assert decision["status"] == "ACCEPTED"


def test_a_url_followed_by_more_path_is_not_the_shorter_source():
    srcs = [rev.Source(url="https://example.org/a", title="A", text=PAGE)]
    assert rev.cited_sources("Claim [1].\n\nSources\n[1] https://example.org/a.html", srcs) == []
    assert rev.cited_sources("Claim [1].\n\nSources\n[1] https://example.org/a.", srcs) == ["https://example.org/a"]
