"""Tests for the .claude library loader (library_agents.py).

The definitions are frozen: these tests pin the loader contract — frontmatter is metadata,
the body is the persona and a byte-exact suffix of the file, and only registry slugs are
hosted. They run against the real shipped definition files.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import library_agents

BASE = Path(__file__).resolve().parent.parent
LIB_DIR = BASE / "library_agents"

PILOT_FILES = ["CFO-Prime.md", "Alex.md", "Luna.md", "Archaeologist.md", "Kraken.md", "HF_supply_scout.md"]


def _raw(file_name: str) -> bytes:
    return (LIB_DIR / file_name).read_bytes()


def test_every_definition_file_is_present_and_parses():
    definitions = library_agents.all_definitions()
    files = [p.name for p in LIB_DIR.glob("*.md") if p.name != "INDEX.md"]
    assert len(files) >= 170
    assert set(definitions) == set(files)
    for name, definition in definitions.items():
        assert definition["name"], name
        assert definition["sha256"] == hashlib.sha256(_raw(name)).hexdigest(), name


def test_body_is_byte_exact_suffix():
    for file_name in PILOT_FILES:
        text = _raw(file_name).decode("utf-8")
        _, body = library_agents._split_frontmatter(text)
        assert text.endswith(body), file_name
        assert body.strip(), file_name


def test_frontmatter_metadata_extracted():
    definitions = library_agents.all_definitions()
    assert definitions["CFO-Prime.md"]["name"] == "CFO-Prime"
    assert "Fortune-50-grade CFO decision" in definitions["CFO-Prime.md"]["description"]
    assert definitions["Luna.md"]["category"] == "analysis"
    assert definitions["HF_supply_scout.md"]["name"] == "HF supply scout"


def test_files_without_frontmatter_pass_through():
    meta, body = library_agents._split_frontmatter("plain text\nno frontmatter\n")
    assert meta == {}
    assert body == "plain text\nno frontmatter\n"


def test_registry_slugs_resolve_to_personas():
    library_agents.reload()
    for slug in library_agents.library_slugs():
        entry_file = library_agents.definition_file(slug)
        assert entry_file and entry_file in library_agents.all_definitions(), slug
        prompt = library_agents.system_prompt(slug)
        assert prompt == library_agents.all_definitions()[entry_file]["body"], slug
        persona = library_agents.persona(slug)
        assert persona is not None and persona[1] == prompt, slug
        assert library_agents.display_name(slug) == library_agents.all_definitions()[entry_file]["name"], slug


def test_unknown_slug_is_not_hosted():
    assert not library_agents.is_library_slug("no-such-agent")
    assert library_agents.persona("no-such-agent") is None
    try:
        library_agents.system_prompt("no-such-agent")
    except LookupError:
        pass
    else:  # pragma: no cover
        raise AssertionError("system_prompt must raise LookupError for unregistered slugs")
