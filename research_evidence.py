"""Evidence for research jobs: gather real sources before writing, require citations, decline without them.

A marketplace research job used to be one LLM call with no tools (negotiation fc83b663, 2026-10-29): the
agent could only offer unverified hypotheses, cited nothing and labelled its own work partial, and the buyer
paid for an opinion. A job that needs evidence now runs a read-only web search and fetch first, the model
writes only from the numbered sources it was given, and delivery is withheld unless the output cites at least
MIN_SOURCES of them. An offer the agent cannot evidence is declined at negotiation, before any money is held.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

MIN_SOURCES = 2
MAX_SOURCES = 4
SEARCH_RESULTS = 6
SOURCE_CHARS = 2500
MIN_SOURCE_TEXT = 400
QUERY_CHARS = 200

EVIDENCE_SLUG_WORDS = ("research", "evidence", "knowledge")
# Narrow on purpose: "source code", "open source" and "data source" are ordinary jobs, not research.
_EVIDENCE_JOB = re.compile(
    r"\bresearch\b|\bcitations?\b|\bcite (?:your |the |its )?sources\b|\bwith sources\b|\bliterature (?:review|search)\b",
    re.I,
)

DECLINE_NOTE = (
    "I couldn't find published sources to back this research right now, so I won't take payment for "
    "unverified findings."
)

SearchFn = Callable[..., Awaitable[dict[str, Any]]]
FetchFn = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Source:
    url: str
    title: str
    text: str


def needs_evidence(slug: str, job_text: str) -> bool:
    """Research and evidence agents always cite; any agent cites when the buyer asks for research or sources."""
    lowered = slug.lower()
    return any(word in lowered for word in EVIDENCE_SLUG_WORDS) or bool(_EVIDENCE_JOB.search(job_text or ""))


def search_query(service: str, job_text: str = "") -> str:
    """A web query from what the buyer asked for: the service line, else the start of the job text."""
    base = (service or "").strip() or (job_text or "").strip()
    base = re.sub(r"^service requested:\s*", "", base, flags=re.I)
    base = re.sub(r"\s+", " ", base)
    return base[:QUERY_CHARS]


def _http_url(url: str) -> bool:
    return isinstance(url, str) and url.startswith(("https://", "http://")) and " " not in url


async def _default_search(**kwargs: Any) -> dict[str, Any]:
    from tools.web_tool import web_search

    return await web_search(**kwargs)


async def _default_fetch(**kwargs: Any) -> dict[str, Any]:
    from tools.web_tool import web_fetch

    return await web_fetch(**kwargs)


async def search_hits(query: str, search: Optional[SearchFn] = None) -> list[dict[str, str]]:
    """Distinct http(s) results for the query; [] on any search failure."""
    if not query:
        return []
    try:
        found = await (search or _default_search)(query=query, num_results=SEARCH_RESULTS)
    except Exception as exc:  # a search outage is "no evidence", never a crash
        logger.warning("evidence search failed: %s", exc)
        return []
    if not isinstance(found, dict) or not found.get("ok"):
        return []
    hits: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in found.get("results") or []:
        url = str((item or {}).get("url") or "")
        if _http_url(url) and url not in seen:
            seen.add(url)
            hits.append({"url": url, "title": str(item.get("title") or "")[:200]})
    return hits


async def gather_sources(
    query: str, search: Optional[SearchFn] = None, fetch: Optional[FetchFn] = None
) -> list[Source]:
    """Search, then fetch pages until MAX_SOURCES have readable text. Read-only; fetch is SSRF-guarded."""
    sources: list[Source] = []
    for hit in await search_hits(query, search):
        if len(sources) >= MAX_SOURCES:
            break
        try:
            page = await (fetch or _default_fetch)(url=hit["url"], extract_text=True)
        except Exception as exc:
            logger.info("evidence fetch failed for %s: %s", hit["url"], exc)
            continue
        if not isinstance(page, dict) or not page.get("ok"):
            continue
        status = page.get("status_code")
        text = re.sub(r"\s+", " ", str(page.get("text") or "")).strip()
        if (isinstance(status, int) and status >= 400) or len(text) < MIN_SOURCE_TEXT:
            continue
        sources.append(Source(url=hit["url"], title=hit["title"], text=text[:SOURCE_CHARS]))
    return sources


def evidence_prompt(job_text: str, sources: list[Source]) -> str:
    """The buyer's job plus numbered sources, with the rule that every claim is cited from them."""
    blocks = "\n\n".join(
        f"[{i}] {s.title or s.url}\nURL: {s.url}\nExcerpt: {s.text}" for i, s in enumerate(sources, start=1)
    )
    return (
        f"{job_text}\n\n"
        "SOURCES (retrieved for this job; use only these):\n\n"
        f"{blocks}\n\n"
        "Write the deliverable from these sources only. Mark every factual claim with its source number, "
        "like [1]. Do not state anything the sources do not support; if they do not cover part of the "
        "request, say so plainly. End with a 'Sources' section listing each source you cited as "
        "[n] Title - URL."
    )


CITATION_CORRECTION = (
    "\n\nCORRECTION REQUIRED: the previous draft did not cite its sources. Rewrite the complete deliverable "
    "using only the SOURCES above, mark each claim with [n], and end with the 'Sources' section listing at "
    "least {minimum} of them with their URLs."
)


_SOURCES_HEADING = re.compile(r"^\W*(?:sources|references)\W*$", re.I | re.M)
_URL_END = r"(?=$|[\s)\]>\"',.;])"


def cited_sources(output: str, sources: list[Source]) -> list[str]:
    """URLs of the given sources that the body marks inline with [n] and the Sources section lists by exact URL.

    A bare "[n] URL" line in the Sources section is not a citation: the marker has to appear in the body.
    """
    text = output or ""
    headings = list(_SOURCES_HEADING.finditer(text))
    if not headings:
        return []
    body, listing = text[: headings[-1].start()], text[headings[-1].end():]
    cited: list[str] = []
    for i, source in enumerate(sources, start=1):
        marked = re.search(rf"\[{i}\]", body)
        listed = re.search(rf"(?:^|[\s(<\[]){re.escape(source.url)}{_URL_END}", listing, re.M)
        if marked and listed:
            cited.append(source.url)
    return cited
