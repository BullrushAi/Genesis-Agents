"""Loader for Ben's .claude agent library, hosted by this gateway alongside the Genesis agents.

The definition files under ``library_agents/`` are VERBATIM copies of the ``.claude/agents``
definitions. They are frozen: this module never writes to them, and registration/hosting code
must never edit their content. The split applied here is the same one Claude Code itself
applies to these files: the YAML frontmatter is configuration/metadata (used only as
platform registration data — name, description, category), and the markdown body is the
agent's system prompt, returned byte-exact.

Which library definitions are live on the marketplace is platform registration data, kept in
``library_registry.json`` next to this module::

    {"agents": {"<platform slug>": {"definition_file": "Kraken.md", "agent_id": "<uuid>"}}}

A slug present in that registry is a hosted library agent: the AP2 provider may hold its
gateway-managed credential and fulfil its sold jobs, the negotiate endpoint evaluates offers
with its persona, and ``/agents/{slug}/run`` executes bounded text jobs with its persona —
exactly the rails the canonical Genesis agents use, with the definition body as the persona
instead of a reviewed skill bundle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger("library_agents")

_BASE = Path(__file__).resolve().parent
_LIBRARY_DIR = _BASE / "library_agents"
_REGISTRY_PATH = _BASE / "library_registry.json"
_INDEX_FILE = "INDEX.md"

_KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$")

_definitions: Optional[dict[str, dict]] = None
_registry: Optional[dict[str, dict]] = None


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a definition into (frontmatter metadata, body).

    The body is a byte-exact suffix of the file text: splitting and rejoining on "\\n" is
    lossless, and the body is everything after the closing ``---`` delimiter line. Files
    without a frontmatter block return empty metadata and the whole text as the body.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        match = _KEY_LINE.match(line)
        if match and match.group(1).lower() not in meta:
            value = match.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            meta[match.group(1).lower()] = value
    return meta, "\n".join(lines[end + 1 :])


def _load_definitions() -> dict[str, dict]:
    """All library definitions keyed by file name (INDEX.md is an index, not a definition)."""
    global _definitions
    if _definitions is not None:
        return _definitions
    out: dict[str, dict] = {}
    if _LIBRARY_DIR.is_dir():
        for path in sorted(_LIBRARY_DIR.glob("*.md")):
            if path.name == _INDEX_FILE:
                continue
            raw = path.read_bytes()
            text = raw.decode("utf-8")
            meta, body = _split_frontmatter(text)
            out[path.name] = {
                "file": path.name,
                "name": meta.get("name") or path.stem,
                "description": meta.get("description", ""),
                "category": meta.get("category", ""),
                "model": meta.get("model", ""),
                "body": body,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
    _definitions = out
    return out


def _load_registry() -> dict[str, dict]:
    """Platform slug -> registration entry for each hosted library agent."""
    global _registry
    if _registry is not None:
        return _registry
    try:
        data = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
        agents = data.get("agents") if isinstance(data, dict) else None
        _registry = agents if isinstance(agents, dict) else {}
    except FileNotFoundError:
        _registry = {}
    except (ValueError, OSError) as exc:
        logger.error("library_registry.json unreadable: %s", exc)
        _registry = {}
    return _registry


def reload() -> None:
    """Drop cached definitions/registry (tests and registration tooling)."""
    global _definitions, _registry
    _definitions = None
    _registry = None


def is_library_slug(slug: str) -> bool:
    """True when this platform slug is a hosted library agent."""
    return slug in _load_registry()


def library_slugs() -> list[str]:
    return sorted(_load_registry())


def definition_file(slug: str) -> Optional[str]:
    entry = _load_registry().get(slug)
    return str(entry.get("definition_file")) if entry else None


def _definition(slug: str) -> Optional[dict]:
    entry = _load_registry().get(slug)
    if not entry:
        return None
    return _load_definitions().get(str(entry.get("definition_file") or ""))


def system_prompt(slug: str) -> str:
    """The agent persona: the definition body, byte-exact. Raises LookupError if not hosted."""
    definition = _definition(slug)
    if definition is None:
        raise LookupError(f"no library definition registered for slug {slug!r}")
    return str(definition["body"])


def display_name(slug: str) -> str:
    definition = _definition(slug)
    return str(definition["name"]) if definition else slug


def persona(slug: str) -> Optional[tuple[str, str]]:
    """(display name, system prompt) for negotiation evaluation, or None if not hosted."""
    definition = _definition(slug)
    if definition is None:
        return None
    return str(definition["name"]), str(definition["body"])


def all_definitions() -> dict[str, dict]:
    """Every parsed definition (metadata + body), keyed by file name."""
    return dict(_load_definitions())


def definition_sha256(file_name: str) -> Optional[str]:
    definition = _load_definitions().get(file_name)
    return str(definition["sha256"]) if definition else None
