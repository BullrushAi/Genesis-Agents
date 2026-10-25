"""Sanitized exact-model catalogue parsing and local evaluation preflight."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx


ANTHROPIC_ROUTE_SCHEMA_VERSION = 1
ANTHROPIC_LOGICAL_ROUTES: dict[str, str] = {
    "anthropic/claude-sonnet-4.5": "claude-sonnet-4-5-20250929",
    "anthropic/claude-haiku-4.5": "claude-haiku-4-5-20251001",
}
CANDIDATE_LOGICAL_MODEL = "anthropic/claude-haiku-4.5"
JUDGE_LOGICAL_MODEL = "anthropic/claude-haiku-4.5"
CANDIDATE_NATIVE_MODEL = ANTHROPIC_LOGICAL_ROUTES[CANDIDATE_LOGICAL_MODEL]
JUDGE_NATIVE_MODEL = ANTHROPIC_LOGICAL_ROUTES[JUDGE_LOGICAL_MODEL]


def anthropic_route_fingerprint() -> str:
    from eval.canonical.contracts import canonical_role_bindings

    canonical = json.dumps(
        {
            "schema_version": ANTHROPIC_ROUTE_SCHEMA_VERSION,
            "routes": ANTHROPIC_LOGICAL_ROUTES,
            "role_bindings": canonical_role_bindings(),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class ExperimentPreflightError(RuntimeError):
    """A safe run-level failure raised before any completion request."""


@dataclass(frozen=True)
class ModelCapability:
    model_id: str
    supports_tools: bool


@dataclass(frozen=True)
class ModelCatalogue:
    models: tuple[ModelCapability, ...]
    sha256: str
    fetched_at: str

    def by_id(self) -> dict[str, ModelCapability]:
        return {model.model_id: model for model in self.models}

    def public_dict(self, requested_ids: tuple[str, ...] = ()) -> dict[str, Any]:
        index = self.by_id()
        return {
            "catalogue_sha256": self.sha256,
            "fetched_at": self.fetched_at,
            "model_count": len(self.models),
            "requested": [
                {
                    "model_id": model_id,
                    "present": model_id in index,
                    "supports_tools": index[model_id].supports_tools if model_id in index else False,
                }
                for model_id in requested_ids
            ],
        }


def parse_model_catalogue(payload: dict[str, Any], *, fetched_at: str | None = None) -> ModelCatalogue:
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ExperimentPreflightError("model_catalogue_invalid")
    parsed: list[ModelCapability] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise ExperimentPreflightError("model_catalogue_invalid")
        model_id = row["id"]
        if model_id in seen:
            raise ExperimentPreflightError("model_catalogue_duplicate_id")
        seen.add(model_id)
        supports_tools = row.get("supports_tools")
        if not isinstance(supports_tools, bool):
            capabilities = row.get("capabilities")
            supports_tools = capabilities.get("supports_tools") if isinstance(capabilities, dict) else None
        parsed.append(ModelCapability(model_id=model_id, supports_tools=supports_tools is True))
    parsed.sort(key=lambda model: model.model_id)
    canonical = json.dumps(
        [{"id": model.model_id, "supports_tools": model.supports_tools} for model in parsed],
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return ModelCatalogue(
        models=tuple(parsed),
        sha256=hashlib.sha256(canonical).hexdigest(),
        fetched_at=fetched_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def preflight_models(catalogue: ModelCatalogue, candidate_model: str, judge_model: str) -> dict[str, Any]:
    if not candidate_model or candidate_model == "auto" or not judge_model or judge_model == "auto":
        raise ExperimentPreflightError("exact_models_required")
    if candidate_model == judge_model:
        raise ExperimentPreflightError("candidate_and_judge_models_must_differ")
    index = catalogue.by_id()
    for role, model_id in (("candidate", candidate_model), ("judge", judge_model)):
        capability = index.get(model_id)
        if capability is None:
            raise ExperimentPreflightError(f"{role}_model_absent")
        if not capability.supports_tools:
            raise ExperimentPreflightError(f"{role}_model_tools_unsupported")
    return catalogue.public_dict((candidate_model, judge_model))


def preflight_anthropic_models(
    catalogue: ModelCatalogue,
    candidate_model: str,
    judge_model: str,
) -> dict[str, Any]:
    """Freeze exact dual-Haiku transport onto distinct immutable role contracts."""
    if not candidate_model or candidate_model == "auto" or not judge_model or judge_model == "auto":
        raise ExperimentPreflightError("exact_models_required")
    if candidate_model != CANDIDATE_LOGICAL_MODEL:
        raise ExperimentPreflightError("candidate_model_role_binding_mismatch")
    if judge_model != JUDGE_LOGICAL_MODEL:
        raise ExperimentPreflightError("judge_model_role_binding_mismatch")
    from canonical_profiles import CANONICAL_SLUGS
    from eval.canonical.contracts import (
        ContractViolation,
        SEMANTIC_JUDGE_CONTRACT,
        genesis_output_contract,
        canonical_role_bindings,
    )

    try:
        bindings_by_slug = {
            slug: canonical_role_bindings(genesis_output_contract(slug), SEMANTIC_JUDGE_CONTRACT)
            for slug in sorted(CANONICAL_SLUGS)
        }
    except ContractViolation as exc:
        raise ExperimentPreflightError(exc.code) from exc
    first = bindings_by_slug[sorted(bindings_by_slug)[0]]
    candidate_bindings = {slug: rows["candidate"] for slug, rows in bindings_by_slug.items()}
    candidate_aggregate = hashlib.sha256(
        json.dumps(candidate_bindings, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    role_bindings = {
        "candidate": {**first["candidate"], "role_binding_sha256": candidate_aggregate},
        "judge": first["judge"],
    }
    requested: list[dict[str, Any]] = []
    index = catalogue.by_id()
    for role, logical_id in (("candidate", candidate_model), ("judge", judge_model)):
        native_id = ANTHROPIC_LOGICAL_ROUTES.get(logical_id)
        if native_id is None:
            raise ExperimentPreflightError(f"{role}_logical_route_absent")
        capability = index.get(native_id)
        if capability is None:
            raise ExperimentPreflightError(f"{role}_model_absent")
        if not capability.supports_tools:
            raise ExperimentPreflightError(f"{role}_model_tools_unsupported")
        requested.append(
            {
                "role": role,
                "logical_model_id": logical_id,
                "native_model_id": native_id,
                "present": True,
                "supports_tools": True,
                **role_bindings[role],
            }
        )
    return {
        "catalogue_sha256": catalogue.sha256,
        "fetched_at": catalogue.fetched_at,
        "model_count": len(catalogue.models),
        "provider": "anthropic",
        "route_schema_version": ANTHROPIC_ROUTE_SCHEMA_VERSION,
        "route_fingerprint": anthropic_route_fingerprint(),
        "canonical_role_bindings": bindings_by_slug,
        "requested": requested,
    }


async def fetch_model_catalogue(endpoint: str, api_key: str, *, timeout_seconds: float = 10.0) -> ModelCatalogue:
    models_url = endpoint.rstrip("/")
    if models_url.endswith("/chat/completions"):
        models_url = models_url[: -len("/chat/completions")] + "/models"
    elif not models_url.endswith("/models"):
        models_url += "/models"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(models_url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.TimeoutException as exc:
        raise ExperimentPreflightError("model_catalogue_timeout") from exc
    except httpx.HTTPError as exc:
        raise ExperimentPreflightError("model_catalogue_transport_error") from exc
    if response.status_code != 200:
        raise ExperimentPreflightError(f"model_catalogue_http_{response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ExperimentPreflightError("model_catalogue_invalid_json") from exc
    if not isinstance(payload, dict):
        raise ExperimentPreflightError("model_catalogue_invalid")
    return parse_model_catalogue(payload)


async def fetch_anthropic_model_catalogue(
    endpoint: str,
    api_key: str,
    *,
    timeout_seconds: float = 10.0,
) -> ModelCatalogue:
    """Fetch Anthropic's authenticated model inventory without retaining payload prose."""
    models_url = endpoint.rstrip("/")
    if models_url.endswith("/v1/messages"):
        models_url = models_url[: -len("/v1/messages")] + "/v1/models"
    elif not models_url.endswith("/v1/models"):
        models_url += "/v1/models"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(
                models_url,
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
    except httpx.TimeoutException as exc:
        raise ExperimentPreflightError("model_catalogue_timeout") from exc
    except httpx.HTTPError as exc:
        raise ExperimentPreflightError("model_catalogue_transport_error") from exc
    if response.status_code != 200:
        raise ExperimentPreflightError(f"model_catalogue_http_{response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ExperimentPreflightError("model_catalogue_invalid_json") from exc
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ExperimentPreflightError("model_catalogue_invalid")
    tool_capable = frozenset(ANTHROPIC_LOGICAL_ROUTES.values())
    normalized = {
        "data": [
            {
                "id": row.get("id"),
                "supports_tools": row.get("id") in tool_capable,
            }
            for row in rows
            if isinstance(row, dict)
        ]
    }
    return parse_model_catalogue(normalized)
