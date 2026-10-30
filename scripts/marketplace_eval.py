#!/usr/bin/env python3
"""Offline marketplace evals for the 19 Genesis agents (marketplace/evals/<slug>.json).

Each case is a realistic buyer request. The harness runs the same code the gateway runs for a paid job
(apply_offer_policy, then marketplace_agent.run_job) and grades the result:

* decision: declines must be declined, deliver and injection cases accepted;
* must_cover / must_not_contain: literal, case-insensitive;
* length within the case bounds; sections and machine checks clean (marketplace_agent.check_delivery);
* citations: [n] markers plus URLs when the case expects them;
* optional --judge: a reviewer model marks each quality_bar item true/false.

A case scores passed checks / total checks; an agent scores the mean of its cases. Ben's bar: no paid SkillProof
run for an agent until it scores in the upper 90s here.

Models: ``--llm router`` uses the gateway's own router (main.call_llm_router, so LLM_API_URL / LLM_API_KEY /
GENESIS_LLM_MODEL from the gateway environment; this spends model credits). ``--replay outputs.json`` grades
recorded outputs ({"<case id>": "<delivery text>"}) and spends nothing. Without either, only the decision checks
for decline cases run.

    python scripts/marketplace_eval.py --slug genesis-fin-ap --llm router --judge --out eval-out/
    python scripts/marketplace_eval.py --all --replay recorded.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import marketplace_agent as ma  # noqa: E402

EVAL_DIR = ROOT / "marketplace" / "evals"
LLM = Callable[[str, str], Awaitable[str]]
UPPER_90S = 0.97


def load_cases(slug: str) -> list[dict[str, Any]]:
    return json.loads((EVAL_DIR / f"{slug}.json").read_text(encoding="utf-8"))["cases"]


def job_text(case: dict[str, Any]) -> str:
    """The job text exactly as ap2_provider.job_input builds it for a real negotiation."""
    import ap2_provider

    return ap2_provider.job_input({"requestedService": case["service"], "requirements": case["requirements"]})


def grade_output(spec: dict[str, Any], case: dict[str, Any], output: str, job: str) -> list[dict[str, Any]]:
    expect = case.get("expect") or {}
    low = (output or "").lower()
    checks: list[dict[str, Any]] = []
    for phrase in expect.get("must_cover") or []:
        checks.append({"check": f"covers '{phrase}'", "pass": phrase.lower() in low})
    for phrase in expect.get("must_not_contain") or []:
        checks.append({"check": f"avoids '{phrase}'", "pass": phrase.lower() not in low})
    words = ma.word_count(output)
    lo, hi = int(expect.get("min_words") or 0), int(expect.get("max_words") or 0)
    if lo or hi:
        ok = (not lo or words >= lo * (1 - ma.WORD_TOLERANCE)) and (not hi or words <= hi * (1 + ma.WORD_TOLERANCE))
        checks.append({"check": f"length {lo}-{hi} words (got {words})", "pass": ok})
    issues = ma.check_delivery(spec, output, job)
    checks.append({"check": "sections and machine checks", "pass": not issues, "detail": issues[:5]})
    if expect.get("citations"):
        cited = bool(re.search(r"\[\d+\]", output or "")) and bool(re.search(r"https?://", output or ""))
        checks.append({"check": "cites sources", "pass": cited})
    return checks


async def judge_output(spec: dict[str, Any], job: str, output: str, llm: LLM) -> list[dict[str, Any]]:
    system, user = ma._review_prompt(spec, job, output)
    raw = await llm(system, user)
    bar = spec.get("quality_bar") or []
    failed = set()
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    answered = set()
    for c in data.get("checks") or []:
        if isinstance(c, dict) and isinstance(c.get("item"), int):
            answered.add(c["item"])
            if c.get("pass") is False:
                failed.add(c["item"])
    return [
        {"check": f"quality bar {i}: {b[:70]}", "pass": i in answered and i not in failed}
        for i, b in enumerate(bar, start=1)
    ]


async def negotiation_decision(slug: str, case: dict[str, Any], llm: Optional[LLM]) -> dict[str, Any]:
    spec = ma.load_spec(slug)
    text = " ".join([case["service"], json.dumps(case["requirements"])])
    if llm is None:
        model = {"status": "ACCEPTED", "price": case["budget"], "notes": "offline"}
    else:
        prompt = (
            f"Service requested: {case['service']}\nBudget offered: ${case['budget']}\nRequirements: {case['requirements']}\n\n"
            'Respond with ONLY JSON: {"status": "ACCEPTED" or "REJECTED" or "COUNTERED", "price": <number>, "notes": "<why>"}'
        )
        raw = await llm(ma.negotiation_system_prompt(spec), prompt)
        m = re.search(r"\{.*\}", raw or "", re.S)
        try:
            model = json.loads(m.group(0)) if m else {}
        except ValueError:
            model = {}
        if model.get("status") not in ("ACCEPTED", "REJECTED", "COUNTERED"):
            model = {"status": "REJECTED", "price": case["budget"], "notes": "unparseable"}
    return ma.apply_offer_policy(slug, case["budget"], text, model)


async def eval_case(
    slug: str, case: dict[str, Any], llm: Optional[LLM], replay: dict[str, str], judge: Optional[LLM]
) -> dict[str, Any]:
    spec = ma.load_spec(slug)
    expect = case.get("expect") or {}
    decision = await negotiation_decision(slug, case, llm)
    want = expect.get("decision", "ACCEPTED")
    got = "REJECTED" if decision.get("status") == "REJECTED" else "ACCEPTED"
    checks = [{"check": f"decision {want}", "pass": got == want, "detail": decision.get("notes")}]
    output = None
    if want == "ACCEPTED" and got == "ACCEPTED":
        job = job_text(case)
        if case["id"] in replay:
            output = replay[case["id"]]
        elif llm is not None:
            output = await ma.run_job(slug, job, llm)
        if output is not None:
            checks += grade_output(spec, case, output, job)
            if judge is not None:
                checks += await judge_output(spec, job, output, judge)
        else:
            checks.append({"check": "delivery (no model or replay; not graded)", "pass": None})
    graded = [c for c in checks if c["pass"] is not None]
    score = sum(1 for c in graded if c["pass"]) / len(graded) if graded else 0.0
    return {"id": case["id"], "kind": case["kind"], "score": round(score, 4), "checks": checks, "output": output}


async def eval_agent(slug: str, llm: Optional[LLM], replay: dict[str, str], judge: Optional[LLM]) -> dict[str, Any]:
    results = [await eval_case(slug, c, llm, replay, judge) for c in load_cases(slug)]
    score = sum(r["score"] for r in results) / len(results) if results else 0.0
    # Ben's bar is about real deliverables: a run that graded only the accept/decline decisions never meets it.
    ungraded = any(ch["pass"] is None for r in results for ch in r["checks"])
    return {
        "slug": slug,
        "score": round(score, 4),
        "graded": "decisions only" if ungraded else "full",
        "meets_bar": (not ungraded) and score >= UPPER_90S,
        "cases": results,
    }


def _router_llm() -> LLM:
    import main

    async def call(system: str, user: str) -> str:
        text, _ = main._router_result_payload(await main.call_llm_router(system, user))
        return text

    return call


def main_cli(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--slug", action="append")
    who.add_argument("--all", action="store_true")
    ap.add_argument("--llm", choices=["router"], help="run the agents on the gateway's model router (spends credits)")
    ap.add_argument("--judge", action="store_true", help="also grade each quality_bar item with the router model")
    ap.add_argument("--replay", type=Path, help="JSON {case_id: output} of recorded deliveries to grade")
    ap.add_argument("--out", type=Path, help="directory for <slug>.json results and summary.md")
    args = ap.parse_args(argv)

    slugs = sorted(ma.all_specs()) if args.all else args.slug
    llm = _router_llm() if args.llm == "router" else None
    judge = (llm or _router_llm()) if args.judge else None
    replay = json.loads(args.replay.read_text()) if args.replay else {}

    started = time.time()
    reports = [asyncio.run(eval_agent(s, llm, replay, judge)) for s in slugs]
    lines = ["| agent | score | graded | upper 90s |", "|---|---|---|---|"]
    for r in reports:
        lines.append(f"| {r['slug']} | {r['score']:.1%} | {r['graded']} | {'yes' if r['meets_bar'] else 'no'} |")
    summary = "\n".join(lines) + f"\n\n{len(reports)} agents, {time.time() - started:.0f}s\n"
    print(summary)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        for r in reports:
            (args.out / f"{r['slug']}.json").write_text(json.dumps(r, indent=2))
        (args.out / "summary.md").write_text(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
