"""
Tests for conduit_mcp_client.ConduitMcpSession.

These spawn the REAL vendored Conduit MCP server over stdio -- no mocks --
so they prove the subprocess lifecycle end to end.  They are skipped (not
failed) when the `mcp` package or the vendored server is unavailable, so the
suite still runs on a machine that has not synced vendor/conduit.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

import conduit_mcp_client
from conduit_mcp_client import (
    ConduitMcpError,
    ConduitMcpSession,
    ConduitMcpUnavailable,
    _parse_tool_payload,
    server_path,
)

pytestmark = pytest.mark.skipif(
    not server_path().exists(),
    reason="vendored Conduit MCP server not present",
)


def _mcp_available() -> bool:
    try:
        import mcp  # noqa: F401
        from mcp.client.stdio import stdio_client  # noqa: F401
    except ImportError:
        return False
    return True


requires_mcp = pytest.mark.skipif(
    not _mcp_available(), reason="mcp python package not installed"
)


def _count_server_processes() -> int | None:
    """Best-effort count of live conduit_mcp_server.py processes.

    Returns None when the platform tooling is unavailable, in which case the
    caller skips the assertion rather than failing.
    """
    needle = "conduit_mcp_server.py"
    try:
        if sys.platform.startswith("win"):
            out = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\""
                    " | Select-Object -ExpandProperty CommandLine",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
        else:
            out = subprocess.run(
                ["ps", "-eo", "args"], capture_output=True, text=True, timeout=30
            ).stdout
    except Exception:
        return None
    if not out:
        return None
    return sum(1 for line in out.splitlines() if needle in line)


# ---------------------------------------------------------------------------
# payload parsing (pure, no subprocess)
# ---------------------------------------------------------------------------


class _Chunk:
    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    def __init__(self, text: str, is_error: bool = False) -> None:
        self.content = [_Chunk(text)]
        self.isError = is_error


def test_parse_tool_payload_returns_dict():
    assert _parse_tool_payload("t", _Result('{"a": 1}')) == {"a": 1}


def test_parse_tool_payload_raises_on_error_key():
    with pytest.raises(ConduitMcpError) as exc:
        _parse_tool_payload("conduit_navigate", _Result('{"error": "boom"}'))
    assert exc.value.tool == "conduit_navigate"
    assert "boom" in str(exc.value)


def test_parse_tool_payload_raises_on_is_error_flag():
    with pytest.raises(ConduitMcpError):
        _parse_tool_payload("t", _Result("kaboom", is_error=True))


def test_parse_tool_payload_wraps_plain_text():
    assert _parse_tool_payload("t", _Result("not json")) == {"text": "not json"}


def test_parse_tool_payload_empty_content():
    assert _parse_tool_payload("t", _Result("")) == {}


# ---------------------------------------------------------------------------
# spawn failure -> ConduitMcpUnavailable (not a generic error)
# ---------------------------------------------------------------------------


def test_missing_server_raises_unavailable(tmp_path):
    async def run():
        async with ConduitMcpSession(server=tmp_path / "nope.py"):
            pass

    with pytest.raises(ConduitMcpUnavailable):
        asyncio.run(run())


def test_unavailable_is_an_mcp_error():
    assert issubclass(ConduitMcpUnavailable, ConduitMcpError)


# ---------------------------------------------------------------------------
# real stdio subprocess lifecycle
# ---------------------------------------------------------------------------


@requires_mcp
def test_real_session_initializes_and_lists_verify_tools():
    async def run():
        async with ConduitMcpSession(call_timeout=90) as session:
            return list(session.tool_names)

    names = asyncio.run(run())
    assert "conduit_verify_deliverable" in names
    assert "conduit_verify_rubric" in names
    assert "conduit_navigate" in names
    assert "conduit_export_proof" in names


@requires_mcp
def test_real_session_unknown_tool_raises_conduit_mcp_error():
    async def run():
        async with ConduitMcpSession(call_timeout=90) as session:
            await session.call("conduit_definitely_not_a_tool")

    with pytest.raises(ConduitMcpError):
        asyncio.run(run())


@requires_mcp
def test_call_after_exit_raises():
    async def run():
        session = ConduitMcpSession(call_timeout=90)
        async with session:
            pass
        assert session._session is None
        assert session._stack is None
        await session.call("conduit_navigate", url="https://example.com")

    with pytest.raises(ConduitMcpError):
        asyncio.run(run())


@requires_mcp
def test_subprocess_is_gone_after_exit():
    before = _count_server_processes()
    if before is None:
        pytest.skip("cannot enumerate processes on this platform")

    async def run():
        async with ConduitMcpSession(call_timeout=90) as session:
            assert session.tool_names

    asyncio.run(run())
    after = _count_server_processes()
    assert after is not None
    assert after <= before


@requires_mcp
def test_session_survives_an_exception_inside_the_block():
    """An exception in the body must still tear the subprocess down."""
    before = _count_server_processes()

    async def run():
        async with ConduitMcpSession(call_timeout=90):
            raise RuntimeError("caller blew up")

    with pytest.raises(RuntimeError):
        asyncio.run(run())

    if before is not None:
        after = _count_server_processes()
        assert after is not None and after <= before


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_env_overrides_server_path(monkeypatch):
    monkeypatch.setenv("CONDUIT_MCP_SERVER_PATH", r"/tmp/custom_server.py")
    assert conduit_mcp_client.server_path().name == "custom_server.py"


def test_timeout_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("CONDUIT_MCP_TIMEOUT_S", raising=False)
    monkeypatch.delenv("CONDUIT_MCP_CALL_TIMEOUT_S", raising=False)
    assert conduit_mcp_client.session_timeout_s() == 120.0
    assert conduit_mcp_client.call_timeout_s() == 60.0

    monkeypatch.setenv("CONDUIT_MCP_TIMEOUT_S", "45")
    monkeypatch.setenv("CONDUIT_MCP_CALL_TIMEOUT_S", "9")
    assert conduit_mcp_client.session_timeout_s() == 45.0
    assert conduit_mcp_client.call_timeout_s() == 9.0

    monkeypatch.setenv("CONDUIT_MCP_TIMEOUT_S", "not-a-number")
    assert conduit_mcp_client.session_timeout_s() == 120.0
