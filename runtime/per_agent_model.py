"""Per-agent model override for the Genesis gateway (design + code only, not deployed).

Problem: GENESIS_LLM_MODEL is global. When it is set (live: anthropic/claude-sonnet-4-5)
it overrides every bundle's model_hint in AgentRuntime and is the primary model in the
legacy persona router. A per-agent switch (e.g. genesis-knowledge-evidence to M3.1-Flash)
therefore cannot be made by changing a bundle hint alone, and changing the global env
would switch all 19 agents at once.

Solution: one env var per agent, named GENESIS_MODEL_OVERRIDE_<SLUG>, where <SLUG> is the
agent slug upper-cased with hyphens (and any other non-alphanumeric) replaced by underscores.
Example: genesis-knowledge-evidence -> GENESIS_MODEL_OVERRIDE_GENESIS_KNOWLEDGE_EVIDENCE.
The value is a router model id, passed through verbatim. This module does NOT hardcode a
provider prefix for any model: the exact router model id for a named model (e.g. M3.1-Flash)
must be confirmed by Platform before deploy and set in the env var at deploy time.

Precedence (router paths only — see scope note below):
  1. per-agent override, if non-empty and not "auto"
  2. global GENESIS_LLM_MODEL, if non-empty and not "auto"
  3. bundle model_hint, if non-empty and not "auto"
  4. caller-supplied default
An empty override or an override of "auto" falls through silently to the next level, so a
mistyped or placeholder override cannot silently pin an agent to a bogus model id; it simply
does not override. A non-empty, non-"auto" override that the router does not recognise
surfaces as the router's own error and then the existing fallback list applies, exactly as
a bad global model would today. Callers must record which model actually ran (see the
observability fields added in agent_runtime.py) so a fallback is never mistaken for the
override having run.

Scope — what this does NOT change:
  * Canonical V5 candidate/judge: still Haiku-pinned in code (runtime/semantic_safety_judge.py
    CANDIDATE_MODEL / JUDGE_MODEL, runtime/router_canonical.py ROUTER_HAIKU_MODEL, startup
    require_exact_model_role_binding, and _normalize_haiku_model). This override is ignored
    there. A canonical-path switch to another model is a separate design (Haiku gate change,
    judge recalibration, full offline gate re-run) and is NOT delivered by this change.
  * Direct Anthropic path: in agent_runtime.py, _call_anthropic is reached only for a reserved
    canonical-result call or when the configured endpoint is an Anthropic URL. The override
    is applied only in the non-Anthropic (router) branch, because an override naming a
    router model must never be posted to api.anthropic.com. If the endpoint is Anthropic,
    the override is skipped and the existing global/bundle selection applies.
  * Global env, bundle hints, fallback lists, token caps, validators, and the V5 contract:
    all unchanged. All other agents fall back to the global env exactly as before.
"""

from __future__ import annotations

import os
import re
from typing import Mapping, Optional

__all__ = [
    "override_env_name",
    "per_agent_override",
    "resolve_agent_model",
    "slug_to_env_name",
]

_AUTO = "auto"


def slug_to_env_name(slug: Optional[str]) -> str:
    """Normalise an agent slug to the env-var name fragment for it."""
    if not slug:
        return ""
    return re.sub(r"[^A-Z0-9]", "_", str(slug).strip().upper())


def override_env_name(slug: Optional[str]) -> str:
    """Env var name for one agent's model override, or "" when no slug is given."""
    fragment = slug_to_env_name(slug)
    return f"GENESIS_MODEL_OVERRIDE_{fragment}" if fragment else ""


def per_agent_override(slug: Optional[str], env: Optional[Mapping[str, str]] = None) -> str:
    """Return the per-agent override model id, or "" when unset, empty, or "auto".

    "auto" falls through deliberately: it is not a model id, and treating it as an
    override would pin the agent to a router request for a model that does not exist.
    """
    name = override_env_name(slug)
    if not name:
        return ""
    source = env if env is not None else os.environ
    value = (source.get(name) or "").strip()
    if not value or value.lower() == _AUTO:
        return ""
    return value


def resolve_agent_model(
    slug: Optional[str],
    bundle_model: Optional[str],
    global_model: Optional[str],
    default: str,
    env: Optional[Mapping[str, str]] = None,
) -> tuple[str, str]:
    """Resolve the primary router model for one agent, in precedence order.

    Returns (model_id, source) where source is one of "per_agent_override",
    "global", "bundle", or "default". Callers record source in observability so
    a fallback is never mistaken for the override having run.
    """
    override = per_agent_override(slug, env=env)
    if override:
        return override, "per_agent_override"
    global_value = (global_model or "").strip()
    if global_value and global_value.lower() != _AUTO:
        return global_value, "global"
    bundle_value = (bundle_model or "").strip()
    if bundle_value and bundle_value.lower() != _AUTO:
        return bundle_value, "bundle"
    return default, "default"
