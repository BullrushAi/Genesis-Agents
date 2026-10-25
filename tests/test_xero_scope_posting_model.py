"""Genesis-side scope map and xero_scoped_invoke tests."""

from __future__ import annotations

import pytest

from accounting.xero_scope import augment_tools_advertised, load_scope_map, operation_allowed, writes_forbidden, validate_scope_map_structure
from tools.xero_scoped_tool import xero_scoped_invoke


def test_scope_map_yaml_structure_valid() -> None:
    validate_scope_map_structure(load_scope_map())


def test_ap_scope_denies_bill_structurally() -> None:
    """GENESIS NEVER WRITES XERO. This used to assert ok is True (genesis-e4l-ap
    was the designated AP writer) -- that was exactly the capability Ben's
    2026-08-27 directive removed. The scope map still names genesis-e4l-ap as
    the AP domain owner for read/propose purposes; it can no longer post."""
    ok, reason = operation_allowed("genesis-e4l-ap", "create_draft_bill")
    assert ok is False
    assert reason == "genesis_write_disabled_structural"


def test_ar_scope_denies_bill() -> None:
    ok, reason = operation_allowed("genesis-e4l-ar", "create_draft_bill")
    assert ok is False
    assert reason == "genesis_write_disabled_structural"


def test_fs_integrity_writes_forbidden() -> None:
    assert writes_forbidden("genesis-e4l-fs-integrity") is True


def test_augment_tools_adds_xero_invoke() -> None:
    tools = augment_tools_advertised("genesis-e4l-ap", ["file_write"])
    assert "xero_scoped_invoke" in tools


@pytest.mark.asyncio
async def test_xero_invoke_requires_verify_api() -> None:
    result = await xero_scoped_invoke(
        operation="create_draft_bill",
        agent_slug="genesis-e4l-ap",
        verify_api_run_id="",
    )
    assert result["ok"] is False
    assert result["error"] == "verify_api_required"


@pytest.mark.asyncio
async def test_xero_invoke_write_op_is_structurally_disabled_regardless_of_provider() -> None:
    """Was test_xero_invoke_without_provider_is_explicitly_unavailable, asserting
    xero_bridge_not_configured. GENESIS NEVER WRITES XERO now denies this before
    the bridge is ever consulted -- see test_xero_write_denial_hostile.py for the
    proof that this holds even when a bridge URL IS configured."""
    result = await xero_scoped_invoke(
        operation="create_draft_bill",
        agent_slug="genesis-e4l-ap",
        verify_api_run_id="verify-test-001",
        entity_key="demo",
        _parent_agent_slug="genesis-e4l-ap",
    )
    assert result["ok"] is False
    assert result["error"] == "genesis_write_disabled"
    assert "receipt" not in result
    assert result.get("simulated") is not True


@pytest.mark.asyncio
async def test_xero_invoke_scope_denied_for_ar_bill() -> None:
    result = await xero_scoped_invoke(
        operation="create_draft_bill",
        agent_slug="genesis-e4l-ar",
        verify_api_run_id="verify-test-002",
        _parent_agent_slug="genesis-e4l-ar",
    )
    assert result["ok"] is False
    assert result["error"] == "genesis_write_disabled"


@pytest.mark.asyncio
async def test_xero_invoke_rejects_agent_slug_escalation() -> None:
    result = await xero_scoped_invoke(
        operation="create_draft_bill",
        agent_slug="genesis-e4l-ap",
        verify_api_run_id="verify-test-escalation",
        _parent_agent_slug="genesis-e4l-ar",
    )
    assert result["ok"] is False
    assert result["error"] == "agent_slug_mismatch"


@pytest.mark.asyncio
async def test_xero_invoke_enforces_dispatch_allowlist() -> None:
    result = await xero_scoped_invoke(
        operation="create_draft_invoice",
        verify_api_run_id="verify-test-allowlist",
        _parent_agent_slug="genesis-e4l-ar",
        _allowed_xero_operations=["get_trial_balance"],
    )
    assert result["ok"] is False
    assert result["error"] == "dispatch_scope_denied"


@pytest.mark.asyncio
async def test_xero_invoke_rejects_short_verify_id() -> None:
    result = await xero_scoped_invoke(
        operation="create_draft_bill",
        verify_api_run_id="short",
        _parent_agent_slug="genesis-e4l-ap",
    )
    assert result["ok"] is False
    assert result["error"] == "verify_api_required"


@pytest.mark.asyncio
async def test_xero_invoke_read_operation_still_reaches_scope_check() -> None:
    """Reads must keep working -- GENESIS NEVER WRITES XERO is a write-only
    denial. get_trial_balance is on the structural read allowlist, so it
    passes the new gate and reaches the normal scope/provider path (which
    fails closed here only because no bridge is configured in this test
    process, not because of the write gate)."""
    result = await xero_scoped_invoke(
        operation="get_trial_balance",
        verify_api_run_id="verify-test-read-001",
        _parent_agent_slug="genesis-e4l-close",
    )
    assert result["error"] != "genesis_write_disabled"
    assert result["error"] in {"xero_bridge_not_configured", "xero_bridge_unavailable"}


@pytest.mark.asyncio
async def test_xero_invoke_denies_renamed_or_unknown_operation() -> None:
    """Default-deny: an operation this module has never heard of -- including
    a plausible alias of a known write op -- is refused by the same
    structural gate, not merely by the (name-keyed) scope map."""
    for alias in ("create_draft_bill_v2", "CREATE_DRAFT_BILL", "post_bill", "create-draft-bill"):
        result = await xero_scoped_invoke(
            operation=alias,
            verify_api_run_id="verify-test-alias-001",
            _parent_agent_slug="genesis-e4l-ap",
        )
        assert result["ok"] is False
        assert result["error"] == "genesis_write_disabled", alias
