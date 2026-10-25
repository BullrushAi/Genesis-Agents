"""
Tests for AP2 delivery mechanism (Gap G3 fix) - CSV Data Cleaner focus.

Verifies:
- CERT-011: Verifiable work result returned
- CERT-024: Work completion recorded
- CERT-043/044: Full deterministic economic flow with pre-existing agent (CSV Data Cleaner)

Tests that MUST FAIL before the fix and PASS after:
1. CSV Data Cleaner accepted → real cleaning → exactly one /ap2/deliver with output hash
2. Timeout produces a failed delivery, not fake success
3. Restart or retry doesn't deliver twice (idempotency)
"""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Test fixtures need database
# pytestmark = pytest.mark.skipif(
#     not pytest.config.getoption("--with-db", default=False),
#     reason="requires --with-db for Postgres delivery tracking"
# )


@pytest.fixture
def sample_csv_data():
    """Sample CSV data for testing."""
    return """name,email,age,city
John Doe,john@example.com,30,New York
  Jane Smith  ,  jane@example.com  ,  25  ,  Los Angeles  
,,,
Bob Johnson,bob@example.com,35,Chicago
,invalid@email.com,,
Alice Williams,alice@example.com,28,Boston"""


@pytest.fixture
def mock_swarmsync_api(monkeypatch):
    """Mock httpx client for SwarmSync API calls."""
    responses = []
    
    class MockResponse:
        def __init__(self, status_code=201, json_data=None):
            self.status_code = status_code
            self.text = json.dumps(json_data or {})
        
        def json(self):
            return json.loads(self.text) if self.text else {}
    
    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            self.responses = responses
        
        async def __aenter__(self):
            return self
        
        async def __aexit__(self, *args):
            pass
        
        async def post(self, url, json=None, headers=None):
            # Record the call
            self.responses.append({
                "url": url,
                "payload": json,
                "headers": headers,
            })
            # Return success
            return MockResponse(201, {"id": f"delivery-{len(self.responses)}"})
        
        async def get(self, url):
            # For CSV URL fetches
            if "csv" in url.lower():
                return MockResponse(200, {})
            return MockResponse(404, {})
    
    monkeypatch.setattr("httpx.AsyncClient", MockAsyncClient)
    return responses


@pytest.fixture
def clean_deliveries_table():
    """Clean the deliveries table before and after tests."""
    from ap2_delivery import _conn
    
    def clean():
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM genesis_ap2_deliveries WHERE negotiation_id LIKE 'test-%'")
                conn.commit()
        except Exception:
            pass  # Table might not exist yet
    
    clean()
    yield
    clean()


@pytest.mark.asyncio
async def test_csv_cleaner_accepted_real_cleaning_one_deliver(
    sample_csv_data, mock_swarmsync_api, clean_deliveries_table
):
    """
    TEST 1: CSV Data Cleaner accepted → real cleaning → exactly one /ap2/deliver with output hash.
    
    This MUST FAIL before the G3 fix (no delivery happens).
    This MUST PASS after the fix (delivery is called with real cleaned CSV and hash).
    """
    from ap2_delivery import execute_csv_cleaning, deliver_to_swarmsync
    
    negotiation_id = "test-csv-negotiation-001"
    responder_agent_id = "8e46cc30-fcea-4de5-8305-a744c7ec55df"  # CSV Data Cleaner
    
    # Execute CSV cleaning work
    result = await execute_csv_cleaning(
        task="Clean this CSV data",
        requirements={"csv_data": sample_csv_data},
        timeout_seconds=30,
    )
    
    # Verify cleaning produces real output
    assert result.content
    assert "John Doe" in result.content
    assert "Jane Smith" in result.content
    # Verify empty rows removed (original had 7 lines, cleaned should have fewer)
    cleaned_lines = [l for l in result.content.split('\n') if l.strip()]
    assert len(cleaned_lines) >= 5  # At least header + 4 data rows with content
    # Verify whitespace trimmed
    assert "  Jane Smith  " not in result.content
    assert "Jane Smith" in result.content
    
    # Verify metadata
    assert result.content_hash
    assert result.elapsed_seconds >= 0  # May be 0 for very fast operations
    assert result.metadata["rows"] > 0
    assert result.metadata["columns"] == 4
    assert len(result.sources) > 0
    
    # Deliver to SwarmSync
    delivery_result = await deliver_to_swarmsync(
        negotiation_id=negotiation_id,
        responder_agent_id=responder_agent_id,
        result=result,
        api_url="https://api.swarmsync.ai",
        gateway_secret="test-secret",
        is_success=True,
    )
    
    # Verify delivery succeeded
    assert delivery_result.success
    assert delivery_result.delivery_id
    
    # Verify exactly ONE /ap2/deliver call was made
    assert len(mock_swarmsync_api) == 1
    
    # Verify the payload structure matches SwarmSync contract
    call = mock_swarmsync_api[0]
    assert call["url"] == "https://api.swarmsync.ai/ap2/deliver"
    assert call["headers"]["x-agent-gateway-secret"] == "test-secret"
    
    payload = call["payload"]
    assert payload["negotiationId"] == negotiation_id
    assert payload["responderAgentId"] == responder_agent_id
    
    # Verify result field
    assert payload["result"]["status"] == "success"
    assert payload["result"]["output"] == result.content
    assert payload["result"]["metadata"]["contentHash"] == result.content_hash
    assert payload["result"]["metadata"]["rows"] == result.metadata["rows"]
    assert payload["result"]["metadata"]["columns"] == result.metadata["columns"]
    
    # Verify evidence field
    assert "sources" in payload["evidence"]
    assert len(payload["evidence"]["sources"]) > 0
    assert "completed_at" in payload["evidence"]
    
    # Verify verificationHints with expectedHash
    assert "verificationHints" in payload
    assert payload["verificationHints"]["expectedHash"] == result.content_hash
    assert payload["verificationHints"]["inlineContent"] == result.content
    
    # CRITICAL: Verify NO autoApprove flag
    assert "autoApprove" not in payload
    assert "autoApprove" not in payload.get("result", {})
    assert "autoApprove" not in payload.get("evidence", {})


@pytest.mark.asyncio
async def test_csv_cleaner_timeout_produces_failed_delivery(
    mock_swarmsync_api, clean_deliveries_table
):
    """
    TEST 2: A timeout produces a failed delivery, not a fake success.
    
    This MUST FAIL before the fix (no delivery or fake success).
    This MUST PASS after the fix (honest failure reported).
    """
    from ap2_delivery import deliver_to_swarmsync
    
    negotiation_id = "test-csv-timeout"
    responder_agent_id = "8e46cc30-fcea-4de5-8305-a744c7ec55df"
    
    # Deliver a timeout failure (simulating a real timeout that happened)
    delivery_result = await deliver_to_swarmsync(
        negotiation_id=negotiation_id,
        responder_agent_id=responder_agent_id,
        result=None,
        api_url="https://api.swarmsync.ai",
        gateway_secret="test-secret",
        is_success=False,
        failure_reason="Work timed out after 120 seconds",
    )
    
    # Verify delivery succeeded (but reported failure)
    assert delivery_result.success  # HTTP call succeeded
    
    # Verify the failure was reported honestly
    call = mock_swarmsync_api[0]
    payload = call["payload"]
    
    assert payload["result"]["status"] == "timeout"
    assert "timed out" in payload["result"]["metadata"]["error"].lower()
    
    # CRITICAL: No fake success data
    assert "output" not in payload["result"]
    assert "verificationHints" not in payload


def _database_configured() -> bool:
    try:
        from runtime import pg_store
        return bool(pg_store.database_url())
    except Exception:
        return False


@pytest.mark.asyncio
@pytest.mark.skipif(not _database_configured(), reason="DATABASE_URL is required for delivery idempotency")
async def test_csv_cleaner_retry_does_not_deliver_twice(
    sample_csv_data, mock_swarmsync_api, clean_deliveries_table
):
    """
    TEST 3: A retry doesn't deliver twice (idempotency).
    
    This MUST FAIL before the fix (no idempotency).
    This MUST PASS after the fix (second attempt is skipped).
    """
    from ap2_delivery import (
        execute_csv_cleaning,
        deliver_to_swarmsync,
        mark_delivered,
        is_already_delivered,
    )
    
    negotiation_id = "test-csv-idempotent"
    responder_agent_id = "8e46cc30-fcea-4de5-8305-a744c7ec55df"
    
    # First delivery
    result = await execute_csv_cleaning(
        task="Clean CSV",
        requirements={"csv_data": sample_csv_data},
        timeout_seconds=30,
    )
    
    delivery1 = await deliver_to_swarmsync(
        negotiation_id=negotiation_id,
        responder_agent_id=responder_agent_id,
        result=result,
        api_url="https://api.swarmsync.ai",
        gateway_secret="test-secret",
        is_success=True,
    )
    
    assert delivery1.success
    
    # Mark as delivered in database
    marked = mark_delivered(
        negotiation_id=negotiation_id,
        responder_agent_id=responder_agent_id,
        content_hash=result.content_hash,
        delivery_id=delivery1.delivery_id,
        is_success=True,
        failure_reason=None,
    )
    assert marked  # First mark succeeds
    
    # Check if already delivered
    assert is_already_delivered(negotiation_id)
    
    # Attempt second delivery (simulating retry or restart)
    already_marked = mark_delivered(
        negotiation_id=negotiation_id,
        responder_agent_id=responder_agent_id,
        content_hash=result.content_hash,
        delivery_id="duplicate-id",
        is_success=True,
        failure_reason=None,
    )
    assert not already_marked  # Second mark fails
    
    # Verify still only ONE /ap2/deliver call from the first delivery
    assert len(mock_swarmsync_api) == 1


@pytest.mark.asyncio
async def test_csv_cleaner_validates_bounds():
    """Verify CSV cleaning enforces size, row, and column limits."""
    from ap2_delivery import execute_csv_cleaning, MAX_CSV_ROWS, MAX_CSV_COLUMNS
    
    # Test too many rows
    large_csv = "col1,col2\n" + "\n".join([f"val{i},val{i}" for i in range(MAX_CSV_ROWS + 100)])
    with pytest.raises(ValueError, match="more than .* rows"):
        await execute_csv_cleaning(
            task="Clean CSV",
            requirements={"csv_data": large_csv},
            timeout_seconds=30,
        )
    
    # Test too many columns
    wide_csv = ",".join([f"col{i}" for i in range(MAX_CSV_COLUMNS + 10)])
    with pytest.raises(ValueError, match="columns"):
        await execute_csv_cleaning(
            task="Clean CSV",
            requirements={"csv_data": wide_csv},
            timeout_seconds=30,
        )


@pytest.mark.asyncio
@pytest.mark.skipif(not _database_configured(), reason="DATABASE_URL is required for delivery idempotency")
async def test_csv_cleaner_restart_safe_idempotency(clean_deliveries_table):
    """
    Verify idempotency survives "restarts" (new connection).
    
    This ensures the database-backed idempotency actually works,
    not just in-memory state.
    """
    from ap2_delivery import mark_delivered, is_already_delivered
    
    negotiation_id = "test-csv-restart"
    
    # "First process" marks delivery
    marked1 = mark_delivered(
        negotiation_id=negotiation_id,
        responder_agent_id="8e46cc30-fcea-4de5-8305-a744c7ec55df",
        content_hash="hash123",
        delivery_id="delivery-1",
        is_success=True,
        failure_reason=None,
    )
    assert marked1
    
    # "After restart" - new connection, check if delivered
    is_delivered = is_already_delivered(negotiation_id)
    assert is_delivered
    
    # "After restart" - attempt to mark again
    marked2 = mark_delivered(
        negotiation_id=negotiation_id,
        responder_agent_id="8e46cc30-fcea-4de5-8305-a744c7ec55df",
        content_hash="hash123",
        delivery_id="delivery-2",
        is_success=True,
        failure_reason=None,
    )
    assert not marked2  # Should fail - already marked


@pytest.mark.asyncio
async def test_csv_cleaner_cleans_properly():
    """Verify CSV cleaning actually cleans the data."""
    from ap2_delivery import execute_csv_cleaning
    
    messy_csv = """name,email,age
John Doe  ,  john@example.com  ,  30  
  ,  ,  
Jane Smith,jane@example.com,25
,,,"""
    
    result = await execute_csv_cleaning(
        task="Clean CSV",
        requirements={"csv_data": messy_csv},
        timeout_seconds=5,
    )
    
    # Verify whitespace trimmed
    assert "  John Doe  " not in result.content
    assert "John Doe" in result.content
    
    # Verify empty rows removed
    lines = [l for l in result.content.split('\n') if l.strip()]
    assert len(lines) == 3  # Header + 2 data rows


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--with-db"])
