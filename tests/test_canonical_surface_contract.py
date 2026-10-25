"""Contract tests for one generated public Genesis surface."""
from __future__ import annotations

from fastapi.testclient import TestClient

import main
from canonical_profiles import CANONICAL_SLUGS
from docs.validation.canonical_surface_inventory import build_inventory


def test_offline_inventory_matches_all_generated_surfaces() -> None:
    result = build_inventory()
    assert result["ok"] is True
    assert result["count"] == 19
    assert result["mismatches"] == {}
    assert result["openapi_enum_mismatches"] == {}
    assert result["missing_openapi_enums"] == []
    assert result["private_schema_paths"] == []


def test_every_canonical_metadata_route_resolves_and_legacy_is_explicit() -> None:
    client = TestClient(main.app)
    for slug in sorted(CANONICAL_SLUGS):
        assert client.get(f"/agents/{slug}/card").status_code == 200
        assert client.get(f"/agents/{slug}/capabilities").status_code == 200
        assert client.get(f"/agents/{slug}/reputation").status_code == 200
    for slug in ("genesis-meta", "genesis_meta_agent", "genesis_meta_x402"):
        response = client.get(f"/agents/{slug}/card")
        assert response.status_code == 410
        assert response.json()["detail"]["error"] == "legacy_agent_deprecated"
    assert client.get("/agents/not-a-real-agent/card").status_code == 404


def test_invocation_acceptance_uses_only_the_generated_roster() -> None:
    for slug in CANONICAL_SLUGS:
        assert main._require_canonical_agent(slug) == slug
    for slug in ("builder_agent", "genesis-research", "genesis_meta_agent"):
        try:
            main._require_canonical_agent(slug)
        except main.HTTPException as exc:
            assert exc.status_code == 410
            assert exc.detail["error"] == "legacy_agent_deprecated"
        else:  # pragma: no cover - makes silent legacy acceptance impossible
            raise AssertionError(f"legacy slug accepted: {slug}")


def test_legacy_route_migrations_are_method_preserving_redirects() -> None:
    async def allow_dependency():
        return object()

    main.app.dependency_overrides[main.verify_gateway_key] = allow_dependency
    main.app.dependency_overrides[main.require_admin] = allow_dependency
    main.app.dependency_overrides[main.verify_continuation_principal] = allow_dependency
    try:
        client = TestClient(main.app, follow_redirects=False)
        admin = client.get("/admin/")
        assert admin.status_code == 308
        assert admin.headers["location"] == "/admin"
        artifacts = client.get("/jobs/job-1/artifacts")
        assert artifacts.status_code == 308
        assert artifacts.headers["location"] == "/agents/jobs/job-1/artifacts"
    finally:
        main.app.dependency_overrides.clear()


def test_route_method_pairs_are_unique() -> None:
    seen: set[tuple[str, str]] = set()
    duplicates: list[tuple[str, str]] = []
    for route in main.app.routes:
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", set()):
            pair = (path, method)
            if pair in seen:
                duplicates.append(pair)
            seen.add(pair)
    assert duplicates == []


def test_public_openapi_preserves_t7_auth_contract() -> None:
    main.app.openapi_schema = None
    schema = main.app.openapi()
    assert set(schema["components"]["securitySchemes"]) == {
        "AgentApiKey", "GatewaySecret", "AP2Version", "AP2Pubkey", "PrincipalToken"
    }
    assert schema["paths"]["/agents/{slug}/run"]["post"]["security"] == [
        {"AgentApiKey": []}, {"GatewaySecret": []},
        {"AP2Version": [], "AP2Pubkey": []},
    ]
