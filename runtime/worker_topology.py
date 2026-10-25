"""Pure worker-topology decision logic used by health and release certification.

The legacy standalone Render worker is not automatically authoritative. Production
can process jobs in-process on the web service or through Trigger.dev. This module
turns environment configuration into one explicit, fail-closed ownership verdict.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


_TRUE = frozenset({"1", "true", "yes"})


def worker_topology(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    source = os.environ if env is None else env
    in_process_enabled = str(source.get("GENESIS_WORKER_ENABLED", "")).lower() in _TRUE
    configured_mode = str(source.get("WORKER_MODE", "trigger_dev")).strip() or "trigger_dev"
    trigger_dispatch_configured = bool(str(source.get("TRIGGER_SECRET_KEY", "")).strip())

    if in_process_enabled:
        effective_mode = "in_process"
        separate_worker_required = False
        worker_intent = "web_service_owns_queue_processing"
    elif configured_mode == "trigger_dev" and trigger_dispatch_configured:
        effective_mode = "trigger_dev"
        separate_worker_required = False
        worker_intent = "trigger_dev_owns_job_dispatch"
    else:
        effective_mode = configured_mode
        separate_worker_required = True
        worker_intent = (
            "trigger_dev_missing_secret"
            if configured_mode == "trigger_dev"
            else "separate_worker_required"
        )

    return {
        "worker_mode": effective_mode,
        "configured_worker_mode": configured_mode,
        "in_process_worker_enabled": in_process_enabled,
        "trigger_dispatch_configured": trigger_dispatch_configured,
        "separate_worker_required": separate_worker_required,
        "worker_intent": worker_intent,
    }
