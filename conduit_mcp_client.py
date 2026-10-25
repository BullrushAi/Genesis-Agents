"""
Conduit MCP stdio client for the SwarmSync agents-gateway.

Spawns the vendored Conduit MCP server (`vendor/conduit/conduit_mcp_server.py`)
as a stdio subprocess and exposes a small typed `call()` surface over it.

This replaces the hand-rolled Patchright reimplementation that previously lived
inside `conduit_verifier.run_verification`: every browser action now flows
through real Conduit tools, so the exported proof bundle carries a genuine
Conduit AIVS audit chain (hash chain + Ed25519 signature) instead of a
locally-assembled JSON blob.

Usage::

    async with ConduitMcpSession() as session:
        await session.call("conduit_navigate", url="https://example.com")
        proof = await session.call("conduit_export_proof", output_path=str(out_dir))

Configuration (env):
  CONDUIT_MCP_SERVER_PATH     path to conduit_mcp_server.py
                              (default <repo>/vendor/conduit/conduit_mcp_server.py)
  CONDUIT_MCP_TIMEOUT_S       whole-session timeout in seconds (default 120)
  CONDUIT_MCP_CALL_TIMEOUT_S  per-tool-call timeout in seconds (default 60)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_GATEWAY_ROOT = Path(__file__).resolve().parent

DEFAULT_CONDUIT_MCP_SERVER_PATH = _GATEWAY_ROOT / "vendor" / "conduit" / "conduit_mcp_server.py"

#: Whole-session budget (spawn + initialize + every call).
DEFAULT_SESSION_TIMEOUT_S = 120.0
#: Per-tool-call budget.
DEFAULT_CALL_TIMEOUT_S = 60.0


class ConduitMcpError(RuntimeError):
    """A Conduit MCP tool returned an error payload or failed to run."""

    def __init__(self, tool: str, message: str, payload: Any = None) -> None:
        super().__init__(f"{tool}: {message}")
        self.tool = tool
        self.message = message
        self.payload = payload


class ConduitMcpUnavailable(ConduitMcpError):
    """The MCP engine could not be started at all.

    Raised for a missing `mcp` dependency, a missing server file, or an MCP
    initialize failure.  This is the ONLY condition under which the verifier is
    allowed to fall back to the legacy Patchright engine -- a session that
    starts and then fails a verification is a real verification failure, not an
    engine problem.
    """


def server_path() -> Path:
    """Resolve the vendored Conduit MCP server path (env-overridable)."""
    raw = os.getenv("CONDUIT_MCP_SERVER_PATH", "").strip()
    return Path(raw) if raw else DEFAULT_CONDUIT_MCP_SERVER_PATH


def session_timeout_s() -> float:
    try:
        return float(os.getenv("CONDUIT_MCP_TIMEOUT_S", "") or DEFAULT_SESSION_TIMEOUT_S)
    except ValueError:
        return DEFAULT_SESSION_TIMEOUT_S


def call_timeout_s() -> float:
    try:
        return float(os.getenv("CONDUIT_MCP_CALL_TIMEOUT_S", "") or DEFAULT_CALL_TIMEOUT_S)
    except ValueError:
        return DEFAULT_CALL_TIMEOUT_S


def _parse_tool_payload(tool: str, result: Any) -> dict[str, Any]:
    """Turn an MCP CallToolResult into a dict, raising ConduitMcpError on errors."""
    chunks: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            chunks.append(text)
    raw = "\n".join(chunks).strip()

    if getattr(result, "isError", False):
        raise ConduitMcpError(tool, raw or "tool reported isError", raw)

    if not raw:
        return {}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # Conduit truncates / serialises some outputs as plain strings.
        return {"text": raw}

    if isinstance(payload, dict):
        if payload.get("error"):
            raise ConduitMcpError(tool, str(payload["error"]), payload)
        return payload
    return {"result": payload}


class ConduitMcpSession:
    """Async context manager owning one Conduit MCP stdio subprocess.

    The subprocess is always terminated on exit -- including on exception and
    timeout paths -- because `stdio_client` closes the transport (and kills the
    child) when its context manager unwinds, and `AsyncExitStack.aclose()` runs
    in every exit path.
    """

    def __init__(
        self,
        *,
        server: str | Path | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
        call_timeout: float | None = None,
    ) -> None:
        self._server = Path(server) if server else server_path()
        self._env_override = env
        self._timeout_s = timeout_s if timeout_s is not None else session_timeout_s()
        self._call_timeout = call_timeout if call_timeout is not None else call_timeout_s()
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self.tool_names: list[str] = []

    @property
    def session_timeout(self) -> float:
        return self._timeout_s

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["CONDUIT_ROOT"] = str(self._server.parent)
        # Keep the child's stdout clean: MCP framing lives on stdout.
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        if self._env_override:
            env.update(self._env_override)
        return env

    async def __aenter__(self) -> "ConduitMcpSession":
        if not self._server.exists():
            raise ConduitMcpUnavailable(
                "spawn",
                f"Conduit MCP server not found at {self._server}",
            )
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
            raise ConduitMcpUnavailable("spawn", f"mcp package unavailable: {exc}") from exc

        params = StdioServerParameters(
            command=sys.executable,
            args=[str(self._server)],
            cwd=str(self._server.parent),
            env=self._build_env(),
        )

        stack = AsyncExitStack()
        self._stack = stack
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=self._call_timeout)
            listing = await asyncio.wait_for(session.list_tools(), timeout=self._call_timeout)
            self.tool_names = [t.name for t in getattr(listing, "tools", []) or []]
            self._session = session
        except BaseException as exc:
            await self._close()
            if isinstance(exc, ConduitMcpUnavailable):
                raise
            raise ConduitMcpUnavailable("spawn", f"MCP initialize failed: {exc}") from exc
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self._close()
        return False

    async def _close(self) -> None:
        stack, self._stack = self._stack, None
        self._session = None
        if stack is None:
            return
        try:
            await stack.aclose()
        except BaseException as exc:  # teardown must never mask the real error
            logger.warning("Conduit MCP session teardown raised: %s", exc)

    # ------------------------------------------------------------------
    # tool calls
    # ------------------------------------------------------------------
    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        """Call a Conduit MCP tool and return its parsed JSON payload.

        Raises ConduitMcpError when the tool reports an error or times out.
        """
        if self._session is None:
            raise ConduitMcpError(tool, "session is not open")

        clean = {k: v for k, v in args.items() if v is not None}
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool, clean),
                timeout=self._call_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise ConduitMcpError(tool, f"timed out after {self._call_timeout}s") from exc
        except ConduitMcpError:
            raise
        except Exception as exc:
            raise ConduitMcpError(tool, f"transport error: {exc}") from exc
        return _parse_tool_payload(tool, result)


__all__ = [
    "ConduitMcpError",
    "ConduitMcpSession",
    "ConduitMcpUnavailable",
    "DEFAULT_CONDUIT_MCP_SERVER_PATH",
    "call_timeout_s",
    "server_path",
    "session_timeout_s",
]
