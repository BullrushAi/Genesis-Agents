"""
AP2 Delivery Module
===================
Handles actual work execution and delivery for accepted AP2 negotiations.

Gap G3: No pre-existing agent performs or delivers work after acceptance.
This module implements a GENERIC delivery mechanism for any gateway-hosted agent.

Idempotency: Delivered negotiations are persisted to genesis_ap2_deliveries
in Postgres, so restarts cannot cause double delivery.

Contract: Matches SwarmSync API /ap2/deliver exactly per
apps/api/src/modules/ap2/dto/service-delivery.dto.ts

Supported Agents:
- csv-data-cleaner: Cleans CSV data with bounded row/size limits
- genesis_research_x402: Research tasks (if naturally supported)
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import ipaddress
import logging
import os
from datetime import datetime, timezone
from typing import Optional, Any, Callable, Awaitable
from urllib.parse import urlsplit

import httpx
import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# Bounded limits for all work types
MAX_TIMEOUT_SECONDS = 120  # 2 minutes max per task
MAX_BUDGET_USD = 10.0  # Max cost cap for a single task
MAX_OUTPUT_TOKENS = 4096  # Aligned with canonical MasterOS limit

# CSV Data Cleaner specific limits
MAX_CSV_SIZE_BYTES = 1_000_000  # 1 MB max CSV input
MAX_CSV_ROWS = 10_000  # 10K rows max
MAX_CSV_COLUMNS = 100  # 100 columns max


def _assert_public_https_url(url: str) -> str:
    """Reject csv_url values that are not public https addresses."""
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https" or not host or parts.username or parts.password:
        raise ValueError("csv_url must be a public https URL")
    if host in {"localhost"} or host.endswith(".local") or host.endswith(".internal"):
        raise ValueError("csv_url must be a public https URL")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url.strip()
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise ValueError("csv_url must be a public https URL")
    return url.strip()


def _conn():
    """Get database connection for delivery tracking."""
    from runtime import pg_store
    db_url = pg_store.database_url()
    if not db_url:
        raise RuntimeError("DATABASE_URL not configured for delivery tracking")
    return psycopg.connect(
        db_url,
        row_factory=dict_row,
        prepare_threshold=None,
        connect_timeout=int(os.getenv("GENESIS_DB_CONNECT_TIMEOUT_S", "10")),
    )


class DeliveryResult(BaseModel):
    """Result of a delivery attempt."""
    success: bool
    delivery_id: Optional[str] = None
    error: Optional[str] = None
    response_code: Optional[int] = None


class WorkResult(BaseModel):
    """Generic result of bounded work execution."""
    content: str
    sources: list[str]
    content_hash: str
    elapsed_seconds: float
    metadata: dict[str, Any]  # Agent-specific metadata


async def execute_csv_cleaning(
    task: str,
    requirements: dict | None,
    timeout_seconds: float = MAX_TIMEOUT_SECONDS,
) -> WorkResult:
    """
    Execute bounded CSV data cleaning with row/size/column limits.
    
    Args:
        task: The cleaning task description (may contain inline CSV or URL)
        requirements: Optional structured requirements with 'csv_data' or 'csv_url'
        timeout_seconds: Max execution time
    
    Returns:
        WorkResult with cleaned CSV, sources, and evidence
    
    Raises:
        asyncio.TimeoutError: If work exceeds timeout
        ValueError: If CSV is invalid or exceeds bounds
    """
    start_time = datetime.now(timezone.utc)
    
    # Extract CSV data from task or requirements
    csv_input = None
    source = "inline CSV data"
    
    if requirements and 'csv_data' in requirements:
        csv_input = requirements['csv_data']
        source = "inline CSV data"
    elif requirements and 'csv_url' in requirements:
        # Fetch CSV from a public https URL only. A buyer-supplied address must not
        # be able to point this gateway at localhost or a private network.
        url = _assert_public_https_url(str(requirements['csv_url']))
        source = f"CSV from {url}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    raise ValueError(f"Failed to fetch CSV from {url}: {resp.status_code}")
                csv_input = resp.text
                # Check size before processing
                if len(csv_input.encode('utf-8')) > MAX_CSV_SIZE_BYTES:
                    raise ValueError(
                        f"CSV exceeds maximum size of {MAX_CSV_SIZE_BYTES} bytes"
                    )
        except httpx.TimeoutException:
            raise ValueError(f"Timeout fetching CSV from {url}")
    elif "csv" in task.lower():
        # Try to extract inline CSV from task description
        lines = task.split('\n')
        csv_lines = [l for l in lines if ',' in l or '\t' in l]
        if csv_lines:
            csv_input = '\n'.join(csv_lines)
            source = "inline CSV data from task"
    
    if not csv_input:
        raise ValueError("No CSV data provided. Include csv_data or csv_url in requirements.")
    
    # Validate size
    csv_bytes = csv_input.encode('utf-8')
    if len(csv_bytes) > MAX_CSV_SIZE_BYTES:
        raise ValueError(f"CSV exceeds maximum size of {MAX_CSV_SIZE_BYTES} bytes")
    
    # Parse and clean CSV with timeout
    try:
        cleaned_csv = await asyncio.wait_for(
            _clean_csv_data(csv_input),
            timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        logger.error("CSV cleaning timed out after %s seconds", timeout_seconds)
        raise
    
    # Calculate content hash
    content_hash = hashlib.sha256(cleaned_csv.encode('utf-8')).hexdigest()
    
    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    
    # Count rows and columns
    reader = csv.reader(io.StringIO(cleaned_csv))
    rows = list(reader)
    row_count = len(rows)
    col_count = len(rows[0]) if rows else 0
    
    return WorkResult(
        content=cleaned_csv,
        sources=[source],
        content_hash=content_hash,
        elapsed_seconds=round(elapsed, 2),
        metadata={
            "rows": row_count,
            "columns": col_count,
            "original_size_bytes": len(csv_bytes),
            "cleaned_size_bytes": len(cleaned_csv.encode('utf-8')),
        },
    )


async def _clean_csv_data(csv_input: str) -> str:
    """
    Clean CSV data: remove empty rows, trim whitespace, validate structure.
    
    Bounded by MAX_CSV_ROWS and MAX_CSV_COLUMNS.
    """
    # Parse CSV
    try:
        reader = csv.reader(io.StringIO(csv_input))
        rows = []
        header = None
        
        for i, row in enumerate(reader):
            if i == 0:
                # Header row
                header = [col.strip() for col in row if col.strip()]
                if len(header) > MAX_CSV_COLUMNS:
                    raise ValueError(
                        f"CSV has {len(header)} columns, max allowed is {MAX_CSV_COLUMNS}"
                    )
                rows.append(header)
            else:
                # Data row - skip empty rows
                if any(cell.strip() for cell in row):
                    # Trim whitespace from each cell
                    cleaned_row = [cell.strip() for cell in row]
                    # Pad or truncate to match header length
                    if len(cleaned_row) < len(header):
                        cleaned_row.extend([''] * (len(header) - len(cleaned_row)))
                    elif len(cleaned_row) > len(header):
                        cleaned_row = cleaned_row[:len(header)]
                    rows.append(cleaned_row)
            
            if len(rows) > MAX_CSV_ROWS:
                raise ValueError(
                    f"CSV has more than {MAX_CSV_ROWS} rows (limit exceeded)"
                )
        
        # Write cleaned CSV
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerows(rows)
        return output.getvalue()
    
    except csv.Error as e:
        raise ValueError(f"Invalid CSV format: {e}")


async def execute_bounded_research(
    task: str,
    requirements: dict | None,
    llm_caller: Callable[[str, str], Awaitable[str]],
    timeout_seconds: float = MAX_TIMEOUT_SECONDS,
) -> WorkResult:
    """
    Execute bounded research work with timeout and token limits.
    
    Args:
        task: The research task description
        requirements: Optional structured requirements
        llm_caller: Async callable that takes (system_prompt, user_prompt) and returns text
        timeout_seconds: Max execution time
    
    Returns:
        WorkResult with content, sources, and evidence
    
    Raises:
        asyncio.TimeoutError: If work exceeds timeout
        ValueError: If task is invalid
    """
    if not task or not task.strip():
        raise ValueError("Research task cannot be empty")
    
    # Build research prompt
    requirements_str = ""
    if requirements:
        requirements_str = f"\n\nAdditional requirements:\n{requirements}"
    
    system_prompt = (
        "You are a research assistant. Provide factual, well-sourced research results. "
        "Always cite your sources. Be concise and accurate. "
        f"Limit your response to {MAX_OUTPUT_TOKENS} tokens."
    )
    
    user_prompt = (
        f"Research task: {task}{requirements_str}\n\n"
        "Provide:\n"
        "1. Summary of findings (max 3 paragraphs)\n"
        "2. Key sources consulted (list URLs or references)\n"
        "3. Any caveats or limitations\n"
        "\n"
        "Format your response as:\n"
        "FINDINGS:\n[your findings]\n\n"
        "SOURCES:\n- [source 1]\n- [source 2]\n...\n\n"
        "CAVEATS:\n[any limitations]"
    )
    
    start_time = datetime.now(timezone.utc)
    
    try:
        # Execute with timeout
        result_text = await asyncio.wait_for(
            llm_caller(system_prompt, user_prompt),
            timeout=timeout_seconds
        )
        
        # Parse sources from result
        sources = _extract_sources(result_text)
        
        # Calculate content hash for verification
        content_bytes = result_text.encode('utf-8')
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        
        # Estimate token count (rough approximation: 1 token ≈ 4 chars)
        token_count = len(result_text) // 4
        
        return WorkResult(
            content=result_text,
            sources=sources,
            content_hash=content_hash,
            elapsed_seconds=round(elapsed, 2),
            metadata={"tokenCount": token_count},
        )
    
    except asyncio.TimeoutError:
        logger.error("Research task timed out after %s seconds", timeout_seconds)
        raise
    except Exception as exc:
        logger.error("Research execution failed: %s", exc)
        raise


def _extract_sources(text: str) -> list[str]:
    """Extract source citations from research result text."""
    sources = []
    lines = text.split('\n')
    in_sources = False
    
    for line in lines:
        line_stripped = line.strip()
        if line_stripped.upper().startswith('SOURCES:'):
            in_sources = True
            continue
        if line_stripped.upper().startswith('CAVEATS:') or line_stripped.upper().startswith('FINDINGS:'):
            in_sources = False
            continue
        
        if in_sources and line_stripped.startswith('-'):
            source = line_stripped.lstrip('- ').strip()
            if source:
                sources.append(source)
    
    # If no sources found in structured format, return generic
    if not sources:
        sources = ["LLM knowledge base (no external sources cited)"]
    
    return sources


async def execute_agent_work(
    slug: str,
    task: str,
    requirements: dict | None,
    llm_caller: Optional[Callable[[str, str], Awaitable[str]]] = None,
    timeout_seconds: float = MAX_TIMEOUT_SECONDS,
) -> WorkResult:
    """
    Generic work executor - dispatches to the appropriate agent implementation.
    
    Args:
        slug: Agent slug (e.g., 'csv-data-cleaner', 'genesis_research_x402')
        task: The work task description
        requirements: Optional structured requirements
        llm_caller: Optional LLM caller for research agents
        timeout_seconds: Max execution time
    
    Returns:
        WorkResult with content, sources, and evidence
    """
    if slug == "csv-data-cleaner":
        return await execute_csv_cleaning(task, requirements, timeout_seconds)
    elif "research" in slug.lower():
        if not llm_caller:
            raise ValueError(f"Research agent {slug} requires llm_caller")
        return await execute_bounded_research(task, requirements, llm_caller, timeout_seconds)
    else:
        raise ValueError(f"Unknown agent slug: {slug}")



async def deliver_to_swarmsync(
    negotiation_id: str,
    responder_agent_id: str,
    result: WorkResult | None,
    api_url: str,
    gateway_secret: str,
    is_success: bool = True,
    failure_reason: Optional[str] = None,
) -> DeliveryResult:
    """
    POST delivery result to SwarmSync API /ap2/deliver endpoint.
    
    Contract matches apps/api/src/modules/ap2/dto/service-delivery.dto.ts exactly:
    - negotiationId (uuid, required)
    - responderAgentId (uuid, required)  
    - result (optional): { status: "success" | "timeout" | "error", output?, metadata? }
    - evidence (optional): { sources: string[], completed_at: ISO }
    - notes (optional)
    - verificationHints (optional): { url?, expectedHash?, ... }
    
    NEVER sends autoApprove or self-approval flags.
    
    Args:
        negotiation_id: The AP2 negotiation ID
        responder_agent_id: The delivering agent's ID
        result: Work result (or None if failed)
        api_url: Base SwarmSync API URL
        gateway_secret: Authentication secret
        is_success: Whether work succeeded
        failure_reason: Error message if failed
    
    Returns:
        DeliveryResult indicating success or failure
    """
    delivery_url = f"{api_url}/ap2/deliver"
    
    # Build delivery payload per SwarmSync contract
    # NEVER include autoApprove or self-approval flags
    failure_lower = (failure_reason or "").lower()
    status_value = "success" if is_success else ("timeout" if ("timeout" in failure_lower or "timed out" in failure_lower) else "error")
    
    payload: dict[str, Any] = {
        "negotiationId": negotiation_id,
        "responderAgentId": responder_agent_id,
    }
    
    # Add result if we have output
    if result:
        payload["result"] = {
            "status": status_value,
            "output": result.content,
            "metadata": {
                **result.metadata,
                "contentHash": result.content_hash,
                "elapsedSeconds": result.elapsed_seconds,
            },
        }
    else:
        payload["result"] = {
            "status": status_value,
        }
        if failure_reason:
            payload["result"]["metadata"] = {"error": failure_reason}
    
    # Add evidence with sources and completion time
    if result:
        payload["evidence"] = {
            "sources": result.sources,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
    
    # Add verification hints with expectedHash for buyer verification
    if is_success and result:
        payload["verificationHints"] = {
            "expectedHash": result.content_hash,
            "inlineContent": result.content,
        }
    
    headers = {
        "Content-Type": "application/json",
        "x-agent-gateway-secret": gateway_secret,
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                delivery_url,
                json=payload,
                headers=headers,
            )
            
            if 200 <= resp.status_code < 300:
                resp_data = resp.json() if resp.text else {}
                logger.info(
                    "Delivery successful for negotiation %s: %d",
                    negotiation_id,
                    resp.status_code,
                )
                return DeliveryResult(
                    success=True,
                    delivery_id=resp_data.get("id"),
                    response_code=resp.status_code,
                )
            else:
                logger.error(
                    "Delivery failed for negotiation %s: %d %s",
                    negotiation_id,
                    resp.status_code,
                    resp.text[:500],
                )
                return DeliveryResult(
                    success=False,
                    error=f"API returned {resp.status_code}",
                    response_code=resp.status_code,
                )
    
    except Exception as exc:
        logger.error(
            "Delivery request failed for negotiation %s: %s",
            negotiation_id,
            exc,
        )
        return DeliveryResult(
            success=False,
            error=str(exc),
        )


def is_already_delivered(negotiation_id: str) -> bool:
    """
    Check if negotiation has already been delivered (restart-safe).
    
    Queries genesis_ap2_deliveries table in Postgres.
    """
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM genesis_ap2_deliveries WHERE negotiation_id = %s",
                (negotiation_id,)
            )
            return cur.fetchone() is not None
    except Exception as exc:
        logger.error("Failed to check delivery status for %s: %s", negotiation_id, exc)
        # Fail safe: if we can't check, assume not delivered
        return False


def mark_delivered(
    negotiation_id: str,
    responder_agent_id: str,
    content_hash: Optional[str],
    delivery_id: Optional[str],
    is_success: bool,
    failure_reason: Optional[str],
) -> bool:
    """
    Mark a negotiation as delivered in database (restart-safe).
    
    Persists to genesis_ap2_deliveries table so restarts/redeploys
    cannot cause double delivery.
    
    Returns:
        True if this is the first delivery, False if already delivered
    """
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO genesis_ap2_deliveries
                (negotiation_id, responder_agent_id, content_hash, delivery_id,
                 is_success, failure_reason, delivered_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (negotiation_id) DO NOTHING
                """,
                (negotiation_id, responder_agent_id, content_hash, delivery_id,
                 is_success, failure_reason)
            )
            inserted = cur.rowcount == 1
            conn.commit()
            if not inserted:
                logger.warning("Negotiation %s already marked as delivered", negotiation_id)
                return False
            logger.info("Marked negotiation %s as delivered", negotiation_id)
            return True
    
    except Exception as exc:
        logger.error("Failed to mark negotiation %s as delivered: %s", negotiation_id, exc)
        # Fail safe: if we can't mark it, assume it wasn't marked
        return False
