"""Sandbox tool - execute Python only behind the fail-closed bwrap boundary."""
from __future__ import annotations
import asyncio
from pathlib import Path
import shutil
import uuid
from typing import Any

from . import register_tool
from runtime.sandbox_manager import run_in_sandbox, sandbox_readiness

_MAX_CODE_LEN = 10_000
_MAX_TIMEOUT = 30


def run_code_readiness() -> dict[str, Any]:
    """Expose the real sandbox capability without attempting code execution."""
    return sandbox_readiness()


def code_format_readiness() -> dict[str, Any]:
    """Report whether the canonical Python formatter dependency is installed."""
    available = shutil.which("black") is not None
    return {
        "capability": "code_format",
        "required_provider": "black",
        "status": "supported" if available else "external_gated",
        "available": available,
        "release_blocker_for_claimed_support": not available,
        "evidence": {
            "provider_present": available,
            "reason": None if available else "black_not_installed",
        },
    }


def platform_tool_readiness() -> dict[str, dict[str, Any]]:
    """Return readiness for both environment-dependent Platform tools."""
    return {
        "code_format": code_format_readiness(),
        "run_code": run_code_readiness(),
    }

async def run_code(
    *,
    code: str,
    language: str = "python",
    timeout: int = 20,
    _job_dir: Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if language != "python":
        return {
            "ok": False,
            "error": "unsupported_language",
            "message": f"Language '{language}' is not supported. Only 'python' is currently available.",
        }

    if len(code) > _MAX_CODE_LEN:
        return {
            "ok": False,
            "error": "code_too_long",
            "message": f"Code exceeds maximum length of {_MAX_CODE_LEN} characters ({len(code)} given).",
        }

    if _job_dir is None or not Path(_job_dir).is_dir():
        return {"ok": False, "error": "no_job_dir"}

    clamped_timeout = min(max(1, int(timeout)), _MAX_TIMEOUT)
    workspace = Path(_job_dir) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    script_name = f"genesis_validation_{uuid.uuid4().hex}.py"
    script_path = workspace / script_name
    script_path.write_text(code, encoding="utf-8")
    try:
        # The command is runtime-generated, never model-controlled. The sandbox
        # manager refuses execution unless a functional bwrap namespace exists.
        result = await asyncio.to_thread(
            run_in_sandbox,
            Path(_job_dir).name,
            f"python3 {script_name}",
            timeout_s=clamped_timeout,
            job_dir=Path(_job_dir),
            allow_network=False,
        )
        result["sandbox_required"] = "bwrap"
        return result
    finally:
        try:
            script_path.unlink()
        except OSError:
            pass


RUN_CODE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_code",
        "description": (
            "Execute Python code in a fail-closed bwrap sandbox with no network or host filesystem access and a "
            f"maximum timeout of {_MAX_TIMEOUT}s. Max code length: {_MAX_CODE_LEN} characters. "
            "Returns stdout, stderr, and exit code."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python source code to execute.",
                },
                "language": {
                    "type": "string",
                    "description": "Programming language. Only 'python' is supported.",
                    "enum": ["python"],
                    "default": "python",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Execution timeout in seconds (max {_MAX_TIMEOUT}, default 20).",
                    "default": 20,
                },
            },
            "required": ["code"],
        },
    },
}


def register() -> None:
    register_tool("run_code", run_code, RUN_CODE_SCHEMA)
