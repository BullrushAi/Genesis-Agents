"""Offline canonical-surface comparison for the Genesis FastAPI app.

This utility creates no jobs and makes no network calls.  Exit status zero means
the source roster, bundle identities, discovery payloads, marketplace results,
and OpenAPI invocation enums are the same exact set.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main
from canonical_profiles import canonical_agent_inventory, check_canonical_inventory


AGENT_PARAMETER_PATHS = (
    "/agents/{slug}/capabilities",
    "/agents/{slug}/card",
    "/agents/{slug}/reputation",
    "/agents/{slug}/run",
    "/agents/{slug}/jobs",
    "/agents/{slug}/negotiate",
)


def build_inventory() -> dict[str, Any]:
    main.app.openapi_schema = None
    client = TestClient(main.app)
    source_check = check_canonical_inventory()
    expected = [entry["slug"] for entry in canonical_agent_inventory()]
    agents = sorted(item["slug"] for item in client.get("/agents").json()["agents"])
    cards = sorted(item["slug"] for item in client.get("/.well-known/agents.json").json()["agents"])
    marketplace = sorted(
        item["slug"]
        for item in client.get("/marketplace/search", params={"limit": 100}).json()["results"]
    )
    schema = client.get("/openapi.json").json()
    openapi_enums: dict[str, list[str]] = {}
    for path in AGENT_PARAMETER_PATHS:
        if path not in schema["paths"]:
            continue
        for operation in schema["paths"][path].values():
            for parameter in operation.get("parameters", []):
                if parameter.get("name") == "slug" and parameter.get("in") == "path":
                    openapi_enums[path] = sorted(parameter["schema"].get("enum", []))

    sets = {
        "source": expected,
        "bundles": source_check["slugs"],
        "agents": agents,
        "cards": cards,
        "marketplace": marketplace,
    }
    mismatches = {name: values for name, values in sets.items() if values != expected}
    enum_mismatches = {
        path: values for path, values in openapi_enums.items() if values != expected
    }
    private_schema_paths = sorted(
        path for path in schema["paths"]
        if path == "/admin" or path.startswith("/admin/") or path.startswith("/internal/")
    )
    missing_enums = sorted(set(AGENT_PARAMETER_PATHS) - openapi_enums.keys())
    return {
        "ok": source_check["ok"] and not mismatches and not enum_mismatches
        and not private_schema_paths and not missing_enums,
        "authority": source_check["authority"],
        "count": len(expected),
        "surfaces": sets,
        "openapi_slug_enums": openapi_enums,
        "mismatches": mismatches,
        "openapi_enum_mismatches": enum_mismatches,
        "missing_openapi_enums": missing_enums,
        "private_schema_paths": private_schema_paths,
    }


if __name__ == "__main__":
    result = build_inventory()
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["ok"] else 1)
