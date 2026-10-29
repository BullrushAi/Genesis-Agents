"""
rubric.py — Rubric evaluation engine for generative AI output verification.

## Rubric Format

A rubric is a plain dict (matching RubricSchema) that declares one or more
predicates to check against a string of generated content.  Every predicate
that appears in the rubric is evaluated; `rubric_pass` is True only when ALL
predicates pass.

Supported predicates:

    min_word_count       int   content.split() count must be >= value
    max_word_count       int   content.split() count must be <= value
    must_contain         list  every string must appear (case-insensitive)
    must_not_contain     list  no string may appear (case-insensitive)
    min_length_chars     int   len(content) must be >= value
    max_length_chars     int   len(content) must be <= value
    language             str   ISO 639-1 code e.g. "en" (requires langdetect)
    content_type_hint    str   "text" | "json" | "html" | "markdown"
    custom_checks        list  sandboxed Python boolean expressions

## Pre-commitment with make_rubric_hash

Call `make_rubric_hash(rubric)` before generating content and store the hex
digest.  After generation, re-hash the same rubric dict and compare — this
proves the rubric was not altered between commitment and evaluation.

    hash_before = make_rubric_hash(rubric)
    content     = generate(...)
    assert make_rubric_hash(rubric) == hash_before  # rubric unchanged
    result      = evaluate_rubric(content, rubric)

## Custom Check Sandbox

Expressions in `custom_checks` run in a restricted namespace.  Available
names: content (str), len, str, int, float, bool, list, dict, re.
Dunder attribute access, imports, and dangerous builtins are blocked at the
AST level before any code executes.
"""

from __future__ import annotations

import ast
import concurrent.futures
import hashlib
import json
import re
import warnings
from typing import Any

try:
    from langdetect import DetectorFactory as _LangDetectorFactory
    from langdetect import detect as _langdetect_detect
    _LangDetectorFactory.seed = 0  # langdetect is random by default; a verdict must not change between runs
    _LANGDETECT_AVAILABLE = True
except ImportError:
    _LANGDETECT_AVAILABLE = False
    warnings.warn(
        "langdetect not installed — language rubric checks will be silently skipped. "
        "Install with: pip install langdetect",
        ImportWarning,
        stacklevel=2,
    )

try:
    from typing import TypedDict

    class RubricSchema(TypedDict, total=False):
        min_word_count: int
        max_word_count: int
        must_contain: list[str]
        must_not_contain: list[str]
        min_length_chars: int
        max_length_chars: int
        language: str
        content_type_hint: str
        custom_checks: list[str]

except ImportError:
    RubricSchema = dict  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_BLOCKED_CALL_NAMES = frozenset({
    "__import__", "eval", "exec", "open", "compile",
    "type", "getattr", "setattr", "delattr", "globals", "locals",
    "vars", "dir",
})

_SAFE_BUILTINS: dict[str, Any] = {
    "len": len, "range": range, "int": int, "float": float,
    "str": str, "bool": bool, "list": list, "dict": dict,
    "set": set, "sum": sum, "min": min, "max": max, "abs": abs,
    "round": round, "sorted": sorted, "enumerate": enumerate,
    "zip": zip, "map": map, "filter": filter, "any": any, "all": all,
    "isinstance": isinstance, "hasattr": hasattr,
}

_SAFE_GLOBALS: dict[str, Any] = {"__builtins__": _SAFE_BUILTINS}


def _ast_is_safe(expr: str) -> tuple[bool, str]:
    """Return (True, "") if the expression passes the AST security check,
    or (False, reason) if it should be rejected."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        return False, f"syntax error: {exc}"

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return False, "imports are not allowed"

        if isinstance(node, ast.Call):
            func = node.func
            name: str | None = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name and name in _BLOCKED_CALL_NAMES:
                return False, f"blocked function call: {name}"

        if isinstance(node, ast.Attribute):
            if (
                node.attr.startswith("__")
                or node.attr in ("__class__", "__subclasses__", "__bases__", "__mro__")
            ):
                return False, f"dunder attribute access blocked: {node.attr}"

        # Block walrus operator (:=) — NamedExpr allows rebinding names in
        # local_ns mid-expression (e.g. `(x := len)(content)`).  While the
        # sandbox is ephemeral, walrus assignments are confusing and unnecessary
        # for legitimate rubric checks.
        if isinstance(node, ast.NamedExpr):
            return False, "walrus operator (:=) is not allowed in custom_checks"

        # Block exponentiation. `10**10**10` needs no function call, no
        # import, no dunder access — just this operator — and it is enough
        # to hang the process and exhaust memory (CPython bigints have no
        # size cap). The 0.5s executor timeout in _eval_custom_check cannot
        # save us here: the computation holds the GIL continuously and never
        # returns control to the waiting thread. Rubric predicates never
        # legitimately need exponentiation.
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            return False, "exponentiation (**) is not allowed in custom_checks"

    return True, ""


def _eval_custom_check(expr: str, content: str) -> dict:
    """Evaluate a single custom_check expression. Returns a predicate result dict."""
    safe, reason = _ast_is_safe(expr)
    if not safe:
        return {
            "predicate": f"custom_check: {expr!r}",
            "passed": False,
            "reason": f"custom_check rejected: unsafe expression ({reason})",
        }

    try:
        compiled_code = compile(expr, "<rubric_check>", "eval")
        exec_namespace = {"__builtins__": _SAFE_BUILTINS, "content": content, "re": re}
        # NOTE: do NOT use `with ThreadPoolExecutor(...) as executor:` here.
        # ThreadPoolExecutor.__exit__ calls shutdown(wait=True), which blocks
        # the caller until the submitted work item finishes — even after
        # future.result(timeout=...) has already raised TimeoutError. Since
        # Python threads cannot be forcibly killed, a pathological expression
        # (e.g. "sum(range(10**9))") would make this function — and, because
        # verify_rubric() calls evaluate_rubric() synchronously with no
        # executor offload, the entire asyncio event loop / MCP server —
        # block for the full runaway computation instead of the intended
        # 0.5s cap. shutdown(wait=False) lets the timed-out call return
        # immediately; the abandoned thread finishes in the background.
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(eval, compiled_code, exec_namespace)
            try:
                result = future.result(timeout=0.5)
            except concurrent.futures.TimeoutError:
                return {
                    "predicate": f"custom_check: {expr!r}",
                    "passed": False,
                    "reason": "custom_check timed out (>0.5s)",
                }
            except Exception as exc:
                return {
                    "predicate": f"custom_check: {expr!r}",
                    "passed": False,
                    "reason": f"custom_check error: {exc}",
                }
        finally:
            executor.shutdown(wait=False)
        passed = bool(result)
    except Exception as exc:
        return {
            "predicate": f"custom_check: {expr!r}",
            "passed": False,
            "reason": f"custom_check error: {exc}",
        }

    status = "PASS" if passed else "FAIL"
    return {
        "predicate": f"custom_check: {expr!r}",
        "passed": passed,
        "reason": f"expression evaluated to {result!r}: {status}",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Deliverable text
# ---------------------------------------------------------------------------
# A marketplace delivery is usually a JSON envelope ({"service", "agent", "negotiation_id", "output": ...})
# whose output may itself be a JSON document. Size, language and must_contain predicates describe the work
# the buyer asked for, so they are scored on the readable text inside the envelope, not on its keys, ids,
# hashes and punctuation. must_not_contain, json and custom checks still see the full delivered content,
# so nothing can be hidden in metadata. The SwarmSync API applies the same rule (release-gate.ts
# deliverableText); keep the two in step.

_METADATA_KEYS = frozenset({
    "service", "agent", "job", "status", "version", "slug", "model", "provider", "kind", "type",
})
_METADATA_SUFFIX = re.compile(r"(?:^|_)(?:id|ids|sha256|hash|url|uri|at|b64|signature|fingerprint|timestamp)$")
_MAX_DEPTH = 6


def _is_metadata_key(key: str) -> bool:
    snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", key).lower()
    return snake in _METADATA_KEYS or bool(_METADATA_SUFFIX.search(snake))


def _collect_text(value: Any, pieces: list[str], depth: int) -> None:
    if depth > _MAX_DEPTH:
        return
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return
        if text[0] in "{[":
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                _collect_text(parsed, pieces, depth + 1)
                return
        pieces.append(text)
    elif isinstance(value, list):
        for item in value:
            _collect_text(item, pieces, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not _is_metadata_key(str(key)):
                _collect_text(item, pieces, depth + 1)


def deliverable_text(content: str) -> str:
    """The readable text of a delivery: plain content as-is; for JSON, its string values minus metadata."""
    stripped = content.strip()
    if not stripped or stripped[0] not in "{[":
        return content
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return content
    if not isinstance(parsed, (dict, list)):
        return content
    pieces: list[str] = []
    _collect_text(parsed, pieces, 0)
    return "\n".join(pieces)


def evaluate_rubric(content: str, rubric: dict) -> dict:
    """Evaluate *content* against *rubric* and return a results dict.

    Parameters
    ----------
    content:
        The generated text to evaluate.
    rubric:
        A dict matching RubricSchema.  Unknown keys are silently ignored.

    Returns
    -------
    dict with keys:
        rubric_pass       bool   True only when every predicate passes
        predicate_results list   one entry per evaluated predicate
        content_length    int    len(deliverable_text(content))
        word_count        int    len(deliverable_text(content).split())
    """
    predicates: list[dict] = []
    text = deliverable_text(content)
    word_count = len(text.split())
    content_length = len(text)

    # --- min_word_count ---
    if "min_word_count" in rubric:
        min_wc = int(rubric["min_word_count"])
        passed = word_count >= min_wc
        predicates.append({
            "predicate": "min_word_count",
            "passed": passed,
            "reason": f"word_count={word_count} >= min={min_wc}: {'PASS' if passed else 'FAIL'}",
        })

    # --- max_word_count ---
    if "max_word_count" in rubric:
        max_wc = int(rubric["max_word_count"])
        passed = word_count <= max_wc
        predicates.append({
            "predicate": "max_word_count",
            "passed": passed,
            "reason": f"word_count={word_count} <= max={max_wc}: {'PASS' if passed else 'FAIL'}",
        })

    # --- must_contain ---
    # NOTE: executor.shutdown(wait=False) instead of `with ... as executor:` —
    # see _eval_custom_check for why the `with` form defeats the timeout.
    if "must_contain" in rubric:
        content_lower = text.lower()
        for phrase in rubric["must_contain"]:
            pattern = re.compile(re.escape(phrase.lower()))
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                future = executor.submit(pattern.search, content_lower)
                try:
                    match = future.result(timeout=1.0)
                    found = match is not None
                    predicates.append({
                        "predicate": "must_contain",
                        "passed": found,
                        "reason": f"'{phrase}' {'found' if found else 'NOT found'}: {'PASS' if found else 'FAIL'}",
                    })
                except concurrent.futures.TimeoutError:
                    predicates.append({
                        "predicate": "must_contain",
                        "passed": False,
                        "reason": f"regex pattern timed out: {phrase}",
                    })
            finally:
                executor.shutdown(wait=False)

    # --- must_not_contain ---
    if "must_not_contain" in rubric:
        content_lower = content.lower()
        for phrase in rubric["must_not_contain"]:
            pattern = re.compile(re.escape(phrase.lower()))
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                future = executor.submit(pattern.search, content_lower)
                try:
                    match = future.result(timeout=1.0)
                    absent = match is None
                    predicates.append({
                        "predicate": "must_not_contain",
                        "passed": absent,
                        "reason": f"'{phrase}' {'absent' if absent else 'FOUND (not allowed)'}: {'PASS' if absent else 'FAIL'}",
                    })
                except concurrent.futures.TimeoutError:
                    predicates.append({
                        "predicate": "must_not_contain",
                        "passed": False,
                        "reason": f"regex pattern timed out: {phrase}",
                    })
            finally:
                executor.shutdown(wait=False)

    # --- min_length_chars ---
    if "min_length_chars" in rubric:
        min_ch = int(rubric["min_length_chars"])
        passed = content_length >= min_ch
        predicates.append({
            "predicate": "min_length_chars",
            "passed": passed,
            "reason": f"length={content_length} >= min={min_ch}: {'PASS' if passed else 'FAIL'}",
        })

    # --- max_length_chars ---
    if "max_length_chars" in rubric:
        max_ch = int(rubric["max_length_chars"])
        passed = content_length <= max_ch
        predicates.append({
            "predicate": "max_length_chars",
            "passed": passed,
            "reason": f"length={content_length} <= max={max_ch}: {'PASS' if passed else 'FAIL'}",
        })

    # --- language ---
    if "language" in rubric:
        expected_lang = rubric["language"]
        if not _LANGDETECT_AVAILABLE:
            predicates.append({
                "predicate": "language",
                "passed": False,
                "reason": (
                    "language check skipped — langdetect not installed. "
                    "Install with: pip install langdetect to enable language verification."
                ),
            })
        else:
            try:
                detected = _langdetect_detect(text)
                passed = detected == expected_lang
                predicates.append({
                    "predicate": "language",
                    "passed": passed,
                    "reason": (
                        f"detected='{detected}', expected='{expected_lang}': "
                        f"{'PASS' if passed else 'FAIL'}"
                    ),
                })
            except Exception as exc:
                predicates.append({
                    "predicate": "language",
                    "passed": True,
                    "reason": f"WARNING: langdetect detection failed ({exc}); check skipped",
                })

    # --- content_type_hint ---
    if "content_type_hint" in rubric:
        hint = rubric["content_type_hint"]
        if hint == "text":
            passed = True
            reason = "content_type_hint='text' always passes: PASS"
        elif hint == "json":
            try:
                json.loads(content)
                passed = True
                reason = "valid JSON: PASS"
            except json.JSONDecodeError as exc:
                passed = False
                reason = f"invalid JSON ({exc}): FAIL"
        elif hint == "html":
            cl = text.lower()
            passed = "<html" in cl or "<!doctype" in cl
            reason = (
                "found <html or <!doctype: PASS"
                if passed
                else "no <html or <!doctype found: FAIL"
            )
        elif hint == "markdown":
            pattern = r"(^#{1,6} |\*\*|__|```|\[.+\]\(.+\))"
            passed = bool(re.search(pattern, text, re.MULTILINE))
            reason = (
                "markdown pattern found: PASS"
                if passed
                else "no markdown pattern found (expected #heading, **bold**, __, ```, or [link](url)): FAIL"
            )
        else:
            passed = True
            reason = f"unknown content_type_hint='{hint}'; check skipped: PASS"

        predicates.append({
            "predicate": "content_type_hint",
            "passed": passed,
            "reason": reason,
        })

    # --- custom_checks ---
    if "custom_checks" in rubric:
        for expr in rubric["custom_checks"]:
            predicates.append(_eval_custom_check(str(expr), content))

    rubric_pass = all(p["passed"] for p in predicates)

    return {
        "rubric_pass": rubric_pass,
        "predicate_results": predicates,
        "content_length": content_length,
        "word_count": word_count,
    }


def make_rubric_hash(rubric: dict) -> str:
    """Return a SHA-256 hex digest of the rubric for pre-commitment.

    The rubric dict is serialised with sorted keys so the hash is stable
    regardless of insertion order.  Store this hash before generating
    content, then re-hash after generation to prove the rubric was not
    modified between commitment and evaluation.
    """
    return hashlib.sha256(json.dumps(rubric, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
