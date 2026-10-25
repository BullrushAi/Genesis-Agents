"""Regression proof for auth, provider truth, ownership, and sandbox claims."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient


def test_openapi_declares_actual_auth_alternatives(monkeypatch):
    import main

    main.app.openapi_schema = None
    schema = main.app.openapi()
    schemes = schema["components"]["securitySchemes"]
    assert set(schemes) >= {
        "AgentApiKey", "GatewaySecret", "AP2Version", "AP2Pubkey", "PrincipalToken"
    }
    assert schema["paths"]["/agents/{slug}/run"]["post"]["security"] == [
        {"AgentApiKey": []}, {"GatewaySecret": []},
        {"AP2Version": [], "AP2Pubkey": []},
    ]
    assert schema["paths"]["/auth/whoami"]["get"]["security"] == [
        {"AgentApiKey": []}, {"GatewaySecret": []},
    ]
    assert "security" not in schema["paths"]["/agents/{slug}/capabilities"]["get"]
    assert schema["paths"]["/agents/jobs/{job_id}/sandbox"]["post"]["security"] == [
        {"AgentApiKey": []}, {"GatewaySecret": []},
    ]


def test_controlled_positive_and_negative_gateway_auth(monkeypatch):
    import main

    monkeypatch.setenv("GATEWAY_API_KEY", "controlled-test-key")
    monkeypatch.setenv("AGENT_GATEWAY_SECRET", "controlled-internal-secret")
    client = TestClient(main.app)

    allowed = client.get("/auth/whoami", headers={"X-Agent-Api-Key": "controlled-test-key"})
    assert allowed.status_code == 200
    assert allowed.json()["principal_id"] == "legacy:gateway"
    assert allowed.json()["auth_method"] == "legacy_gateway"
    internal = client.get(
        "/auth/whoami",
        headers={"X-Agent-Gateway-Secret": "controlled-internal-secret"},
    )
    assert internal.status_code == 200
    assert internal.json()["principal_id"] == "legacy:gateway"
    assert client.get("/auth/whoami").status_code == 401
    refused = client.get(
        "/auth/whoami", headers={"X-Agent-Gateway-Secret": "wrong-secret"}
    )
    assert refused.status_code == 401
    assert refused.json()["detail"] == "Invalid or missing gateway credential"


def test_async_ap2_binding_covers_enqueued_params():
    from main import assert_envelope_binds_request
    from runtime.request_auth import AuthenticationError

    body = {
        "prompt": "bounded",
        "params": {"scope": "vendor-renewals"},
        "task": {"scope": "different"},
        "payload": {
            "agent": "genesis-research",
            "task": "bounded",
            "params": {"scope": "vendor-renewals"},
        },
    }
    assert_envelope_binds_request(body, "genesis-research", params_field="params")
    body["params"]["scope"] = "tampered"
    with pytest.raises(AuthenticationError, match="ap2_params_mismatch"):
        assert_envelope_binds_request(body, "genesis-research", params_field="params")


def test_shared_gateway_side_channel_enforces_ownership(monkeypatch):
    import main
    from fastapi import HTTPException
    from runtime.request_auth import legacy_gateway_principal

    monkeypatch.setattr(main, "_JOB_STORE_OK", True)
    monkeypatch.setattr(
        main, "get_job",
        lambda _job_id: {"tenantId": "e4l", "ownerPrincipalId": "service:cato"},
    )
    with pytest.raises(HTTPException) as exc:
        main._authorize_job_read(legacy_gateway_principal(), "ap2-owned")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_xero_missing_provider_is_unavailable_not_simulated_success(monkeypatch):
    """Uses get_trial_balance (a read, on the structural allowlist) so this
    keeps testing the bridge-not-configured path. create_draft_bill can no
    longer reach that path at all -- GENESIS NEVER WRITES XERO denies it
    before XERO_MCP_BRIDGE_URL is ever read; see
    test_xero_write_denial_hostile.py for that proof."""
    import tools.xero_scoped_tool as xero

    monkeypatch.setattr(xero, "XERO_MCP_BRIDGE_URL", "")
    result = await xero.xero_scoped_invoke(
        operation="get_trial_balance",
        verify_api_run_id="verify-test-001",
        _parent_agent_slug="genesis-e4l-close",
    )
    assert result["ok"] is False
    assert result["error"] == "xero_bridge_not_configured"
    assert "receipt" not in result
    assert result.get("simulated") is not True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "wire_body", "expected_error"),
    [(503, '{"error":"provider down"}', "xero_bridge_error"),
     (200, "{}", "xero_bridge_receipt_missing")],
)
async def test_xero_provider_failure_or_missing_receipt_fails_closed(
    monkeypatch, status, wire_body, expected_error
):
    """Uses get_trial_balance -- a read, on the structural allowlist -- so this
    keeps exercising the provider-error/missing-receipt handling. A write
    operation would never reach this code at all under GENESIS NEVER WRITES
    XERO (see test_xero_write_denial_hostile.py)."""
    import tools.xero_scoped_tool as xero

    class Response:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False
        async def text(self):
            return wire_body

    class Session:
        def __init__(self, **_kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False
        def post(self, *_args, **_kwargs):
            response = Response()
            response.status = status
            return response

    monkeypatch.setattr(xero, "XERO_MCP_BRIDGE_URL", "https://xero-bridge.invalid")
    monkeypatch.setattr(xero.aiohttp, "ClientSession", Session)
    result = await xero.xero_scoped_invoke(
        operation="get_trial_balance",
        verify_api_run_id="verify-test-provider",
        _parent_agent_slug="genesis-e4l-close",
    )
    assert result["ok"] is False
    assert result["error"] == expected_error


@pytest.mark.asyncio
async def test_financeos_exception_fails_closed(monkeypatch):
    import tools.financeos_proofs_tool as proofs

    async def unavailable(**_kwargs):
        raise asyncio.TimeoutError

    monkeypatch.setattr(proofs, "post_invoice_proof", unavailable)
    result = await proofs.financeos_invoice_proof(document_id="doc-1")
    assert result == {
        "ok": False, "error": "financeos_provider_unavailable", "reason": "TimeoutError"
    }


@pytest.mark.asyncio
async def test_financeos_success_shape_without_evidence_is_refused(monkeypatch):
    import tools.financeos_proofs_tool as proofs

    async def malformed(**_kwargs):
        return {"ok": True, "verdict": "PASS"}

    monkeypatch.setattr(proofs, "post_verify_api", malformed)
    result = await proofs.financeos_verify_api(
        entity_id="entity-1", operation="create_draft_bill",
        _parent_agent_slug="genesis-e4l-ap",
    )
    assert result == {"ok": False, "error": "financeos_provider_contract_invalid"}


def test_sandbox_health_and_cards_follow_readiness(monkeypatch):
    import main
    import runtime.sandbox_manager as sandbox
    import tools.sandbox_tool as sandbox_tool
    from capability_cards import card_for

    unavailable = {
        "capability": "run_code", "required_provider": "bwrap",
        "status": "external_gated", "available": False,
        "release_blocker_for_claimed_support": True,
        "evidence": {"provider_present": False, "host_supported": True,
                     "functional_probe_passed": False, "reason": "bwrap_not_installed"},
    }
    monkeypatch.setattr(sandbox, "sandbox_readiness", lambda: unavailable)
    monkeypatch.setattr(
        sandbox_tool, "platform_tool_readiness",
        lambda: {"run_code": unavailable, "code_format": unavailable},
    )
    health = TestClient(main.app).get("/health/sandbox")
    assert health.status_code == 200
    assert health.json()["ok"] is False
    assert health.json()["ready"] is False

    card = card_for("genesis-builder")
    assert card is not None
    assert card["status"] == "bundle_present"
    assert card["runtime_readiness"] == "not_live_verified"
    advertised = {item["tool"]: item for item in card["capabilities"]}
    assert advertised["run_code"]["dependency_ready"] is False
    assert "send_email" not in advertised
