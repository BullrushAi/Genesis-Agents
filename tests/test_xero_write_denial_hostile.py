"""GENESIS NEVER WRITES XERO -- hostile/adversarial proof.

Ben's canonical rule (2026-08-27): no Genesis runtime/profile/provider path
may invoke a Xero write operation, regardless of environment variables,
tenant, demo/prod mode, prompt, profile, agent slug, tool alias, caller, or
configuration drift. Genesis may READ approved Xero data where authorized and
may PROPOSE bills/invoices/journals as data for FinanceOS to evaluate; only
FinanceOS's approved, deterministic Worker may execute an actual Xero write.

This file tries each of five concrete bypass vectors Ben named and proves the
write is refused in every case, at the lowest possible level (the bare
`xero_scoped_invoke` coroutine and the bare `operation_allowed` function --
not the FastAPI app, not agent_runtime, not the tool registry), so that no
dispatch-layer plumbing gets credit for a block it did not perform.

Every hostile test that reaches the point where a real Xero write *could* be
attempted also asserts the HTTP layer was never touched, via a POST-call
counter on a fake aiohttp.ClientSession/response pair -- so "returned ok:
False" cannot be mistaken for "the block happened after the network call".
"""
from __future__ import annotations

import pytest

from accounting.xero_scope import GENESIS_WRITE_OPERATIONS, operation_allowed
from tools.xero_scoped_tool import GENESIS_XERO_READ_ONLY_OPERATIONS, xero_scoped_invoke

WRITE_OPERATIONS = [
    "create_draft_bill",
    "create_draft_invoice",
    "create_draft_manual_journal",
    "create_bank_transaction",
    "attach_file_to_bill",
    "attach_file_to_invoice",
]


class _CountingResponse:
    """A Xero-bridge response that would look like a real successful write if
    it were ever reached."""

    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self):
        return '{"receipt": {"id": "INV-LIVE-0001", "status": "AUTHORISED"}}'


class _CountingSession:
    """Records every POST attempt. If call_count is ever > 0 for a write
    operation, the structural gate failed -- the assertion on call_count is
    the load-bearing check, not the ok:False return value alone."""

    call_count = 0
    last_url: str | None = None
    last_body: dict | None = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def post(self, url, json=None, headers=None, **_kwargs):
        type(self).call_count += 1
        type(self).last_url = url
        type(self).last_body = json
        return _CountingResponse()

    @classmethod
    def reset(cls) -> None:
        cls.call_count = 0
        cls.last_url = None
        cls.last_body = None


@pytest.fixture(autouse=True)
def _reset_counting_session():
    _CountingSession.reset()
    yield
    _CountingSession.reset()


# ---------------------------------------------------------------------------
# Vector (a): demo env flag changed to point at a live-looking URL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", WRITE_OPERATIONS)
async def test_vector_a_live_looking_bridge_url_does_not_unlock_write(monkeypatch, operation):
    """Flip XERO_MCP_BRIDGE_URL from the demo value to something that looks
    exactly like a live production tenant endpoint, and swap in a fake
    aiohttp client that would happily return a successful receipt if it were
    ever called. The write must still be refused, and the fake client must
    never see a single POST."""
    import tools.xero_scoped_tool as xero

    monkeypatch.setattr(
        xero, "XERO_MCP_BRIDGE_URL", "https://live.xero-mcp.energy4life-production.internal/v1/write"
    )
    monkeypatch.setattr(xero, "XERO_MCP_BRIDGE_TOKEN", "prod-live-token-not-demo")
    monkeypatch.setattr(xero.aiohttp, "ClientSession", _CountingSession)

    result = await xero_scoped_invoke(
        operation=operation,
        verify_api_run_id="verify-vector-a-0001",
        _parent_agent_slug="genesis-e4l-ap",
        entity_key="energy4life-live",
    )

    assert result["ok"] is False
    assert result["error"] == "genesis_write_disabled"
    assert _CountingSession.call_count == 0, "a POST was attempted despite the structural gate"


# ---------------------------------------------------------------------------
# Vector (b): a production tenant ID supplied in the request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", WRITE_OPERATIONS)
async def test_vector_b_production_tenant_in_payload_does_not_unlock_write(monkeypatch, operation):
    """Supply a live-looking tenant/entity_key and execution_realm inside the
    request itself -- the values an attacker fully controls -- and prove they
    have zero effect on the outcome."""
    import tools.xero_scoped_tool as xero

    monkeypatch.setattr(xero, "XERO_MCP_BRIDGE_URL", "https://xero-mcp-bridge.example.com/v1")
    monkeypatch.setattr(xero.aiohttp, "ClientSession", _CountingSession)

    result = await xero_scoped_invoke(
        operation=operation,
        verify_api_run_id="verify-vector-b-0001",
        _parent_agent_slug="genesis-e4l-treasury",
        entity_key="00000000-0000-4000-8000-live-tenant-guid",
        payload={"tenant_id": "00000000-0000-4000-8000-live-tenant-guid"},
        _execution_realm="live_production",
        execution_realm="live_production",
    )

    assert result["ok"] is False
    assert result["error"] == "genesis_write_disabled"
    assert _CountingSession.call_count == 0


# ---------------------------------------------------------------------------
# Vector (c): the write tool name aliased/renamed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alias",
    [
        "create_draft_bill_v2",
        "CREATE_DRAFT_BILL",
        "Create_Draft_Bill",
        "create-draft-bill",
        "post_bill",
        "xero_write_bill",
        "create_bill_final",
        "draft_bill_create",
    ],
)
async def test_vector_c_aliased_or_renamed_write_operation_is_still_denied(monkeypatch, alias):
    """The gate is a default-deny allowlist of known reads, not a blocklist of
    known write names -- so an attacker cannot sidestep it by renaming,
    recasing, or inventing a plausible-looking new operation string. Any
    operation not on GENESIS_XERO_READ_ONLY_OPERATIONS is refused, including
    names this module has never seen before."""
    import tools.xero_scoped_tool as xero

    monkeypatch.setattr(xero, "XERO_MCP_BRIDGE_URL", "https://xero-mcp-bridge.example.com/v1")
    monkeypatch.setattr(xero.aiohttp, "ClientSession", _CountingSession)

    result = await xero_scoped_invoke(
        operation=alias,
        verify_api_run_id="verify-vector-c-0001",
        _parent_agent_slug="genesis-e4l-ap",
    )

    assert result["ok"] is False
    assert result["error"] == "genesis_write_disabled", alias
    assert _CountingSession.call_count == 0, alias
    assert alias not in GENESIS_XERO_READ_ONLY_OPERATIONS


def test_vector_c_scope_module_also_denies_aliases_independently():
    """Second, independent layer: accounting.xero_scope.operation_allowed on
    its own (bypassing tools/xero_scoped_tool.py entirely) also refuses any
    operation outside the known set for every e4l agent."""
    for alias in ("create_draft_bill_v2", "post_bill", "xero_write_bill"):
        ok, reason = operation_allowed("genesis-e4l-ap", alias)
        assert ok is False
        assert reason == "unknown_operation"


# ---------------------------------------------------------------------------
# Vector (d): an old legacy genesis-e4l-* slug invoked directly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slug",
    [
        "genesis-e4l-ap",
        "genesis-e4l-ar",
        "genesis-e4l-cash",
        "genesis-e4l-treasury",
        "genesis-e4l-journals",
        "genesis-e4l-intercompany",
        "genesis-e4l-close",
        "genesis-e4l-revenue",
        "genesis-e4l-shopify",
        "genesis-e4l-stripe",
    ],
)
async def test_vector_d_every_legacy_e4l_slug_is_denied_for_every_write_it_used_to_own(monkeypatch, slug):
    """Before this fix, each of these slugs was the *designated primary
    writer* for at least one Xero write operation per
    XERO_SCOPE_TO_AGENT_MAP.yaml. Prove every one of them is now denied for
    every write operation, not just the one it did not own."""
    import tools.xero_scoped_tool as xero

    monkeypatch.setattr(xero, "XERO_MCP_BRIDGE_URL", "https://xero-mcp-bridge.example.com/v1")
    monkeypatch.setattr(xero.aiohttp, "ClientSession", _CountingSession)

    for operation in WRITE_OPERATIONS:
        result = await xero_scoped_invoke(
            operation=operation,
            verify_api_run_id=f"verify-vector-d-{slug}-{operation}",
            _parent_agent_slug=slug,
        )
        assert result["ok"] is False, (slug, operation)
        assert result["error"] == "genesis_write_disabled", (slug, operation)
    assert _CountingSession.call_count == 0


def test_vector_d_scope_map_still_names_these_slugs_as_owners_but_code_overrides_it():
    """The YAML still records legacy domain ownership for documentation/read
    purposes (it is not this task's job to rewrite that history) -- but the
    Python-level structural gate overrides it unconditionally. This is the
    proof that a YAML edit alone (re-adding primary_write entries, flipping a
    specialist_override) cannot reactivate a write."""
    from accounting.xero_scope import load_scope_map

    scope_map = load_scope_map()
    ap_writers = scope_map["scopes"]["accounting.contacts"]["primary_write"]
    assert "genesis-e4l-ap" in ap_writers, (
        "expected the YAML to still (historically) name genesis-e4l-ap as AP "
        "owner -- if this assertion fails the fixture assumption changed, not "
        "the thing under test"
    )
    ok, reason = operation_allowed("genesis-e4l-ap", "create_draft_bill")
    assert ok is False
    assert reason == "genesis_write_disabled_structural"


def test_vector_d_no_write_operation_survives_in_either_allowlist():
    """Cross-check the two independent modules agree: nothing in
    GENESIS_WRITE_OPERATIONS is present in GENESIS_XERO_READ_ONLY_OPERATIONS,
    and every literal write name is covered by both gates."""
    assert GENESIS_WRITE_OPERATIONS.isdisjoint(GENESIS_XERO_READ_ONLY_OPERATIONS)
    for operation in WRITE_OPERATIONS:
        assert operation in GENESIS_WRITE_OPERATIONS
        assert operation not in GENESIS_XERO_READ_ONLY_OPERATIONS


# ---------------------------------------------------------------------------
# Vector (e): a direct/raw invocation bypassing the normal dispatch path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vector_e_raw_function_call_bypassing_tool_registry_and_http_layer(monkeypatch):
    """Skip the tool registry, agent_runtime, and the FastAPI app entirely --
    call the bare coroutine directly with hand-built, fully well-formed
    inputs (valid verify_api_run_id shape, a trusted slug injected the way
    the runtime would, a live-looking bridge). This is the strongest form of
    'raw endpoint' bypass available in-process: even a caller with total
    control over every argument the function accepts cannot make it write."""
    import tools.xero_scoped_tool as xero

    monkeypatch.setattr(xero, "XERO_MCP_BRIDGE_URL", "https://xero-mcp-bridge.example.com/v1")
    monkeypatch.setattr(xero, "XERO_MCP_BRIDGE_TOKEN", "whatever-token-an-attacker-controls")
    monkeypatch.setattr(xero.aiohttp, "ClientSession", _CountingSession)

    result = await xero.xero_scoped_invoke(
        operation="create_draft_manual_journal",
        payload={"narration": "attacker-controlled journal", "amount": "999999.99"},
        entity_key="live-tenant",
        verify_api_run_id="verify-vector-e-0001",
        agent_slug="genesis-e4l-journals",
        _parent_agent_slug="genesis-e4l-journals",
        _agent_slug="genesis-e4l-journals",
        _execution_realm="live_production",
        _scope_map_version="attacker-supplied",
    )

    assert result["ok"] is False
    assert result["error"] == "genesis_write_disabled"
    assert _CountingSession.call_count == 0


@pytest.mark.asyncio
async def test_vector_e_registered_tool_dict_also_refuses(monkeypatch):
    """Same proof, but through tools.get_tool("xero_scoped_invoke") -- the
    actual dispatch path an agent runtime would use -- so the block is not
    something that only exists when the private function is imported
    directly."""
    import tools
    import tools.xero_scoped_tool as xero

    tools.register_default_tools()
    monkeypatch.setattr(xero, "XERO_MCP_BRIDGE_URL", "https://xero-mcp-bridge.example.com/v1")
    monkeypatch.setattr(xero.aiohttp, "ClientSession", _CountingSession)

    registered = tools.get_tool("xero_scoped_invoke")
    assert registered is not None

    result = await registered(
        operation="create_draft_invoice",
        verify_api_run_id="verify-vector-e-0002",
        _parent_agent_slug="genesis-e4l-ar",
    )

    assert result["ok"] is False
    assert result["error"] == "genesis_write_disabled"
    assert _CountingSession.call_count == 0


# ---------------------------------------------------------------------------
# Read path must survive all of the above unharmed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_operation_still_reaches_the_bridge_when_configured(monkeypatch):
    """Negative control on the whole test file: prove the structural gate is
    write-specific, not a blanket kill switch. A legitimate read, with a
    bridge configured and a fake client that returns a real receipt, must
    actually complete and actually call the bridge exactly once."""
    import tools.xero_scoped_tool as xero

    monkeypatch.setattr(xero, "XERO_MCP_BRIDGE_URL", "https://xero-mcp-bridge.example.com/v1")
    monkeypatch.setattr(xero.aiohttp, "ClientSession", _CountingSession)

    result = await xero_scoped_invoke(
        operation="get_trial_balance",
        verify_api_run_id="verify-read-control-0001",
        _parent_agent_slug="genesis-e4l-close",
    )

    assert result["ok"] is True
    assert result["operation"] == "get_trial_balance"
    assert _CountingSession.call_count == 1
