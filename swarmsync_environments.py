"""Trusted SwarmSync environments for the Genesis provider gateway.

Genesis is an independent provider gateway. More than one SwarmSync deployment (production, certification
staging, ...) may transact with the same Genesis process. Everything a transaction needs from "its"
SwarmSync is therefore derived per request instead of read from process-global settings:

* The caller is identified by the credential it presents. Each environment owns its own gateway secret
  (inbound negotiate / execute calls and the secret echoed on negotiate callbacks) and its own internal
  secret (inbound /verify jobs and the secret sent on verification callbacks). A presented secret that
  matches an environment *is* that environment's identity; secrets must therefore be unique.
* Callbacks go only to the authenticated environment's own origin, and carry only that environment's secret.
  A production secret is never sent to another environment's origin and vice versa.
* Genesis's own LLM provider is not part of this: it stays Genesis-owned configuration and is never derived
  from the caller.

Configuration
-------------
The environment described by the legacy variables is always present and is named ``default``:
``SWARMSYNC_API_URL`` (default ``https://api.swarmsync.ai``), ``AGENT_GATEWAY_SECRET``, ``INTERNAL_SECRET``.
Additional environments are added with ``SWARMSYNC_ENVIRONMENTS``, a JSON list::

    [{"name": "cert-staging", "api_url": "https://swarmsync-api-cert-staging.onrender.com",
      "gateway_secret": "...", "internal_secret": "..."}]

An invalid additional entry is skipped and logged (names only, never secrets); it can never weaken or remove
the ``default`` environment.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("swarmsync_environments")

DEFAULT_API_URL = "https://api.swarmsync.ai"
DEFAULT_NAME = "default"
MIN_SECRET_LENGTH = 16
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class SwarmSyncEnvironment:
    name: str
    api_url: str  # origin only, no trailing slash
    gateway_secret: str = ""
    internal_secret: str = ""

    @property
    def origin(self) -> str:
        return self.api_url


def origin_of(url: str) -> Optional[str]:
    """Return ``scheme://host[:port]`` for an http(s) URL, or None when it is unusable.

    https is required; plain http is accepted only for loopback hosts (local development). URLs that carry
    credentials are rejected so an allow-list comparison can never be fooled by ``user@host`` tricks.
    """
    try:
        parts = urlsplit((url or "").strip())
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if not host or parts.username is not None or parts.password is not None:
        return None
    if scheme == "http" and host not in _LOOPBACK_HOSTS:
        return None
    if scheme not in ("https", "http"):
        return None
    default_port = 443 if scheme == "https" else 80
    shown_host = f"[{host}]" if ":" in host else host
    if port is None or port == default_port:
        return f"{scheme}://{shown_host}"
    return f"{scheme}://{shown_host}:{port}"


def _normalize_api_url(url: str) -> Optional[str]:
    """An environment's API base must be a bare origin (no path, query or fragment)."""
    parts = urlsplit((url or "").strip())
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    return origin_of(url)


def _default_environment() -> Optional[SwarmSyncEnvironment]:
    api_url = _normalize_api_url(os.getenv("SWARMSYNC_API_URL", DEFAULT_API_URL))
    if api_url is None:
        logger.error("SWARMSYNC_API_URL is not a usable origin; the default SwarmSync environment is disabled")
        return None
    return SwarmSyncEnvironment(
        name=DEFAULT_NAME,
        api_url=api_url,
        gateway_secret=os.getenv("AGENT_GATEWAY_SECRET", "").strip(),
        internal_secret=os.getenv("INTERNAL_SECRET", "").strip(),
    )


def _extra_environments(taken: Iterable[SwarmSyncEnvironment]) -> list[SwarmSyncEnvironment]:
    raw = (os.getenv("SWARMSYNC_ENVIRONMENTS") or "").strip()
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except ValueError:
        logger.error("SWARMSYNC_ENVIRONMENTS is not valid JSON; no additional environments are trusted")
        return []
    if not isinstance(entries, list):
        logger.error("SWARMSYNC_ENVIRONMENTS must be a JSON list; no additional environments are trusted")
        return []

    accepted: list[SwarmSyncEnvironment] = []
    seen = list(taken)
    for index, entry in enumerate(entries):
        label = f"entry {index}"
        if not isinstance(entry, dict):
            logger.error("SWARMSYNC_ENVIRONMENTS %s skipped: not an object", label)
            continue
        name = str(entry.get("name") or "").strip()
        label = f"entry {index} ({name or 'unnamed'})"
        api_url = _normalize_api_url(str(entry.get("api_url") or ""))
        gateway_secret = str(entry.get("gateway_secret") or "").strip()
        internal_secret = str(entry.get("internal_secret") or "").strip()
        problem = None
        if not name or name == DEFAULT_NAME or any(e.name == name for e in seen):
            problem = "name missing, reserved or duplicated"
        elif api_url is None:
            problem = "api_url is not a bare https origin"
        elif any(e.api_url == api_url for e in seen):
            problem = "origin already registered"
        elif len(gateway_secret) < MIN_SECRET_LENGTH or len(internal_secret) < MIN_SECRET_LENGTH:
            problem = f"secrets must be at least {MIN_SECRET_LENGTH} characters"
        elif any(
            (e.gateway_secret and e.gateway_secret == gateway_secret)
            or (e.internal_secret and e.internal_secret == internal_secret)
            for e in seen
        ):
            problem = "secret not unique across environments (identity would be ambiguous)"
        if problem:
            logger.error("SWARMSYNC_ENVIRONMENTS %s skipped: %s", label, problem)
            continue
        env = SwarmSyncEnvironment(name, api_url, gateway_secret, internal_secret)  # type: ignore[arg-type]
        accepted.append(env)
        seen.append(env)
    return accepted


def environments() -> list[SwarmSyncEnvironment]:
    """All trusted environments, read from the process environment at call time (secret rotation friendly)."""
    default = _default_environment()
    base = [default] if default else []
    return base + _extra_environments(base)


def default_environment() -> Optional[SwarmSyncEnvironment]:
    return _default_environment()


def _match(presented: Optional[str], secrets: list[tuple[SwarmSyncEnvironment, str]]) -> Optional[SwarmSyncEnvironment]:
    candidate = (presented or "").encode("utf-8")
    found: Optional[SwarmSyncEnvironment] = None
    for env, secret in secrets:  # no early exit: timing does not reveal which environment was nearest
        if secret and hmac.compare_digest(candidate, secret.encode("utf-8")):
            found = env
    return found


def resolve_by_gateway_secret(presented: Optional[str]) -> Optional[SwarmSyncEnvironment]:
    return _match(presented, [(e, e.gateway_secret) for e in environments()])


def resolve_by_internal_secret(presented: Optional[str]) -> Optional[SwarmSyncEnvironment]:
    return _match(presented, [(e, e.internal_secret) for e in environments()])


def resolve_by_origin(url: str) -> Optional[SwarmSyncEnvironment]:
    origin = origin_of(url)
    if origin is None:
        return None
    for env in environments():
        if env.origin == origin:
            return env
    return None


def any_gateway_secret_configured() -> bool:
    return any(e.gateway_secret for e in environments())


def any_internal_secret_configured() -> bool:
    return any(e.internal_secret for e in environments())


def assert_callback_allowed(callback_url: str, env: Optional[SwarmSyncEnvironment] = None) -> SwarmSyncEnvironment:
    """Return the environment a callback may be sent to, or raise ValueError.

    With ``env`` (the caller authenticated as that environment) the callback must target that environment's
    origin. Without it (caller authenticated by Genesis's own API key) the origin must belong to a registered
    environment, and that environment is the one whose secret will be attached.
    """
    incoming = origin_of(callback_url)
    if incoming is None:
        raise ValueError("callback_url is not a usable https origin")
    if env is not None:
        if incoming != env.origin:
            raise ValueError(f"callback origin '{incoming}' does not belong to the authenticated environment")
        return env
    resolved = resolve_by_origin(callback_url)
    if resolved is None:
        raise ValueError(f"callback origin '{incoming}' is not a registered SwarmSync environment")
    return resolved


def internal_secret_for_callback(callback_url: str) -> str:
    """Secret to attach to a job-completion callback: only the registered environment's own, else none."""
    env = resolve_by_origin(callback_url)
    return env.internal_secret if env else ""
