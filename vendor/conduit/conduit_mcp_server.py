"""Standalone stdio MCP server for Conduit headless browser + audit proofs.

Run with:  python conduit_mcp_server.py
or via:    python -m conduit_mcp_server / conduit-mcp

Marketplace_* tools are kept for compatibility; prefer a future
conduit-marketplace split if the tool list stays large.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Ensure Conduit's own directory is on the path regardless of where Python is invoked from.
_CONDUIT_ROOT = Path(__file__).resolve().parent
if str(_CONDUIT_ROOT) not in sys.path:
    sys.path.insert(0, str(_CONDUIT_ROOT))


def _bootstrap_cato() -> None:
    cato_pkg = types.ModuleType("cato")
    cato_pkg.__path__ = [str(_CONDUIT_ROOT)]
    cato_pkg.__package__ = "cato"
    sys.modules.setdefault("cato", cato_pkg)

    for _alias in ("cato.platform", "cato.conduit_platform"):
        if _alias not in sys.modules:
            import importlib.util as _ilu

            _spec = _ilu.spec_from_file_location(_alias, str(_CONDUIT_ROOT / "conduit_platform.py"))
            assert _spec and _spec.loader
            _pmod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_pmod)  # type: ignore[union-attr]
            sys.modules[_alias] = _pmod
            attr_name = "conduit_platform" if "conduit_platform" in _alias else "platform"
            setattr(sys.modules["cato"], attr_name, _pmod)

    tools_pkg = types.ModuleType("cato.tools")
    tools_pkg.__path__ = [str(_CONDUIT_ROOT / "tools")]
    tools_pkg.__package__ = "cato.tools"
    sys.modules.setdefault("cato.tools", tools_pkg)
    sys.modules["cato"].tools = sys.modules["cato.tools"]  # type: ignore[attr-defined]


_bootstrap_cato()

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    TextContent,
    TextResourceContents,
    Tool,
)

# ---------------------------------------------------------------------------
# Lazy bridge + session state for resources
# ---------------------------------------------------------------------------
_bridge: Any = None
_last_proof: dict[str, Any] | None = None
_MAX_TOOL_CHARS = 25_000


async def _get_bridge():
    global _bridge
    if _bridge is None:
        from tools.conduit_bridge import ConduitBridge

        _bridge = ConduitBridge()
        await _bridge.start()
    return _bridge


def _truncate_json(payload: Any) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) <= _MAX_TOOL_CHARS:
        return text
    return json.dumps(
        {
            "truncated": True,
            "hint": f"Output exceeded {_MAX_TOOL_CHARS} chars; request a narrower selector, lower limit, or export_proof.",
            "preview": text[: _MAX_TOOL_CHARS - 200],
        },
        ensure_ascii=False,
    )


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> Tool:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return Tool(name=name, description=description, inputSchema=schema)


# ---------------------------------------------------------------------------
# Tool definitions — browser core + compat marketplace
# ---------------------------------------------------------------------------
BROWSER_TOOLS = [
    _tool("conduit_navigate", "Navigate to a URL in the stealth headless browser.", {"url": {"type": "string"}}, ["url"]),
    _tool("conduit_screenshot", "Take a screenshot of the current browser page.", {"full_page": {"type": "boolean", "default": False}, "path": {"type": "string"}}),
    _tool("conduit_extract", "Extract text content from the current page.", {"selector": {"type": "string", "description": "CSS selector (optional, defaults to body)"}}),
    _tool("conduit_click", "Click an element (CSS selector or accessibility ref like e3).", {"selector": {"type": "string"}}, ["selector"]),
    _tool("conduit_fill", "Fill a form field with text.", {"selector": {"type": "string"}, "text": {"type": "string"}}, ["selector", "text"]),
    _tool("conduit_type", "Type text into an element (CSS or ref eN).", {"selector": {"type": "string"}, "text": {"type": "string"}}, ["selector", "text"]),
    _tool("conduit_hover", "Hover an element (CSS or ref eN).", {"selector": {"type": "string"}}, ["selector"]),
    _tool(
        "conduit_scroll",
        "Scroll the page or an element.",
        {
            "selector": {"type": "string"},
            "direction": {"type": "string", "description": "up|down|left|right"},
            "amount": {"type": "number"},
            "x": {"type": "number"},
            "y": {"type": "number"},
        },
    ),
    _tool("conduit_wait", "Wait a fixed duration (ms or seconds per bridge).", {"ms": {"type": "number"}, "seconds": {"type": "number"}, "timeout_ms": {"type": "number"}}),
    _tool("conduit_wait_for", "Wait until a selector appears.", {"selector": {"type": "string"}, "timeout_ms": {"type": "number"}}, ["selector"]),
    _tool("conduit_key_press", "Press a keyboard key.", {"key": {"type": "string"}}, ["key"]),
    _tool("conduit_select_option", "Select a dropdown option.", {"selector": {"type": "string"}, "value": {"type": "string"}, "label": {"type": "string"}}, ["selector"]),
    _tool("conduit_handle_dialog", "Accept or dismiss the next JS dialog.", {"action": {"type": "string", "description": "accept|dismiss"}, "prompt_text": {"type": "string"}}),
    _tool("conduit_navigate_back", "Navigate back in history.", {}),
    _tool("conduit_console_messages", "Return collected console messages.", {}),
    _tool(
        "conduit_extract_main",
        "Extract main readable content (strips nav/ads).",
        {"fmt": {"type": "string", "description": "md|text|html", "default": "md"}, "max_chars": {"type": "integer"}},
    ),
    _tool("conduit_extract_structured", "Schema-validated structured extraction.", {"schema": {"type": "object"}, "instruction": {"type": "string"}}),
    _tool(
        "conduit_accessibility_snapshot",
        "Accessibility tree with stable refs (e1, e2, …). Paginate large pages.",
        {"offset": {"type": "integer", "default": 0}, "limit": {"type": "integer", "default": 100}},
    ),
    _tool("conduit_network_requests", "Return captured network request log.", {}),
    _tool("conduit_map", "Discover site URLs (BFS map).", {"url": {"type": "string"}, "max_urls": {"type": "integer"}}),
    _tool("conduit_crawl", "Bulk page extraction crawl.", {"url": {"type": "string"}, "max_pages": {"type": "integer"}, "depth": {"type": "integer"}}),
    _tool("conduit_fingerprint", "SHA-256 fingerprint of current page content.", {}),
    _tool("conduit_check_changed", "Signed change detection vs prior fingerprint.", {"fingerprint": {"type": "string"}}),
    _tool("conduit_output_to_file", "Write extracted content to a workspace file.", {"path": {"type": "string"}, "content": {"type": "string"}, "fmt": {"type": "string"}}),
    _tool("conduit_search", "Search the web (DuckDuckGo, Brave, etc.).", {"query": {"type": "string"}, "engine": {"type": "string", "default": "ddg"}, "num_results": {"type": "integer", "default": 10}}, ["query"]),
    _tool(
        "conduit_eval",
        "Execute JavaScript on the page. Source is stored verbatim in the audit chain. IRREVERSIBLE — requires confirm:true. Use dry_run:true to preview.",
        {
            "script": {"type": "string", "description": "JavaScript to execute"},
            "js_code": {"type": "string", "description": "Alias for script"},
            "confirm": {"type": "boolean", "description": "Must be true to execute"},
            "dry_run": {"type": "boolean", "description": "If true, return plan without executing", "default": False},
        },
    ),
    _tool("conduit_export_proof", "Export a self-verifiable cryptographic proof bundle of all actions.", {"output_path": {"type": "string"}}),
    _tool(
        "conduit_verify_deliverable",
        "Fetch a delivered artifact URL, compute its SHA-256, compare to expected_hash, and write deliverable_verified to the audit chain (SwarmSync escrow verification).",
        {"url": {"type": "string"}, "expected_hash": {"type": "string"}, "request_id": {"type": "string"}},
        ["url"],
    ),
    _tool(
        "conduit_verify_rubric",
        "Evaluate a pre-committed rubric (hash must match rubric_hash) against a URL or inline_content and write the result to the audit chain (SwarmSync escrow verification).",
        {"rubric": {"type": "object"}, "rubric_hash": {"type": "string"}, "request_id": {"type": "string"}, "url": {"type": "string"}, "inline_content": {"type": "string"}},
        ["rubric", "rubric_hash"],
    ),
]

# Compat marketplace tools (legacy — do not expand)
MARKETPLACE_TOOLS = [
    _tool("conduit_marketplace_list", "List marketplace product metadata and supported marketplaces. (compat)", {}),
    _tool(
        "conduit_marketplace_create_job",
        "Create a queued marketplace extraction job. (compat)",
        {
            "marketplace": {"type": "string"},
            "target_type": {"type": "string"},
            "target_url": {"type": "string"},
            "account_id": {"type": "string"},
            "proxy_label": {"type": "string"},
            "request_payload": {"type": "object"},
        },
        ["marketplace", "target_type", "target_url"],
    ),
    _tool("conduit_marketplace_targets", "List supported target types for a marketplace. (compat)", {"marketplace": {"type": "string"}}, ["marketplace"]),
    _tool(
        "conduit_marketplace_plan",
        "Build a normalized marketplace extraction plan. (compat)",
        {"marketplace": {"type": "string"}, "target_type": {"type": "string"}, "target_url": {"type": "string"}, "account_id": {"type": "string"}, "proxy_label": {"type": "string"}},
        ["marketplace", "target_type", "target_url"],
    ),
    _tool(
        "conduit_marketplace_create_account",
        "Create a named marketplace account record. (compat)",
        {"marketplace": {"type": "string"}, "display_name": {"type": "string"}, "credential_key": {"type": "string"}, "proxy_label": {"type": "string"}, "metadata": {"type": "object"}},
        ["marketplace", "display_name"],
    ),
    _tool(
        "conduit_marketplace_create_proxy",
        "Create or update a named marketplace proxy route. (compat)",
        {"label": {"type": "string"}, "host": {"type": "string"}, "port": {"type": "integer"}, "protocol": {"type": "string", "default": "http"}, "username": {"type": "string"}, "password": {"type": "string"}, "kind": {"type": "string", "default": "http"}, "metadata": {"type": "object"}},
        ["label", "host", "port"],
    ),
    _tool("conduit_marketplace_list_proxies", "List named marketplace proxy routes. (compat)", {"state": {"type": "string"}}),
    _tool("conduit_marketplace_get_proxy", "Fetch one named marketplace proxy route. (compat)", {"label": {"type": "string"}}, ["label"]),
    _tool("conduit_marketplace_test_proxy", "Test a named proxy route. (compat)", {"label": {"type": "string"}, "test_url": {"type": "string", "default": "https://api.ipify.org/"}}, ["label"]),
    _tool(
        "conduit_marketplace_save_session",
        "Register a saved marketplace session cookie file. (compat)",
        {"account_id": {"type": "string"}, "label": {"type": "string"}, "cookie_path": {"type": "string"}, "state": {"type": "string"}, "metadata": {"type": "object"}},
        ["account_id", "label", "cookie_path"],
    ),
    _tool("conduit_marketplace_list_accounts", "List configured marketplace accounts. (compat)", {"marketplace": {"type": "string"}}),
    _tool("conduit_marketplace_list_sessions", "List saved marketplace sessions. (compat)", {"marketplace": {"type": "string"}, "account_id": {"type": "string"}}),
    _tool("conduit_marketplace_get_session", "Fetch one saved marketplace session. (compat)", {"session_id": {"type": "string"}}, ["session_id"]),
    _tool("conduit_marketplace_bootstrap_session", "Log into a marketplace account and persist session. (compat)", {"account_id": {"type": "string"}, "target_url": {"type": "string"}}, ["account_id"]),
    _tool("conduit_marketplace_run_job", "Execute a queued marketplace extraction job. (compat)", {"job_id": {"type": "string"}}, ["job_id"]),
    _tool("conduit_marketplace_enqueue_job", "Queue a marketplace job for background execution. (compat)", {"job_id": {"type": "string"}}, ["job_id"]),
    _tool("conduit_marketplace_queue_status", "Inspect marketplace job queue status. (compat)", {"job_id": {"type": "string"}}),
    _tool("conduit_marketplace_get_job", "Fetch one marketplace job. (compat)", {"job_id": {"type": "string"}}, ["job_id"]),
    _tool("conduit_marketplace_list_jobs", "List marketplace jobs. (compat)", {"marketplace": {"type": "string"}, "status": {"type": "string"}}),
    _tool("conduit_marketplace_list_results", "List marketplace results. (compat)", {"job_id": {"type": "string"}}),
    _tool("conduit_marketplace_get_result", "Fetch one marketplace result. (compat)", {"result_id": {"type": "string"}}, ["result_id"]),
    _tool("conduit_marketplace_export_result", "Export one marketplace result as JSONL or CSV. (compat)", {"result_id": {"type": "string"}, "fmt": {"type": "string", "default": "jsonl"}}, ["result_id"]),
]

TOOLS = BROWSER_TOOLS + MARKETPLACE_TOOLS

ACTION_MAP = {
    "conduit_navigate": "navigate",
    "conduit_screenshot": "screenshot",
    "conduit_extract": "extract",
    "conduit_click": "click",
    "conduit_fill": "fill",
    "conduit_type": "type",
    "conduit_hover": "hover",
    "conduit_scroll": "scroll",
    "conduit_wait": "wait",
    "conduit_wait_for": "wait_for",
    "conduit_key_press": "key_press",
    "conduit_select_option": "select_option",
    "conduit_handle_dialog": "handle_dialog",
    "conduit_navigate_back": "navigate_back",
    "conduit_console_messages": "console_messages",
    "conduit_extract_main": "extract_main",
    "conduit_extract_structured": "extract_structured",
    "conduit_accessibility_snapshot": "accessibility_snapshot",
    "conduit_network_requests": "network_requests",
    "conduit_map": "map",
    "conduit_crawl": "crawl",
    "conduit_fingerprint": "fingerprint",
    "conduit_check_changed": "check_changed",
    "conduit_output_to_file": "output_to_file",
    "conduit_search": "search",
    "conduit_eval": "eval",
    "conduit_export_proof": "export_proof",
    "conduit_verify_deliverable": "verify_deliverable",
    "conduit_verify_rubric": "verify_rubric",
    "conduit_marketplace_list": "marketplace_list",
    "conduit_marketplace_targets": "marketplace_targets",
    "conduit_marketplace_plan": "marketplace_plan",
    "conduit_marketplace_create_job": "marketplace_create_job",
    "conduit_marketplace_create_account": "marketplace_create_account",
    "conduit_marketplace_create_proxy": "marketplace_create_proxy",
    "conduit_marketplace_list_proxies": "marketplace_list_proxies",
    "conduit_marketplace_get_proxy": "marketplace_get_proxy",
    "conduit_marketplace_test_proxy": "marketplace_test_proxy",
    "conduit_marketplace_save_session": "marketplace_save_session",
    "conduit_marketplace_list_accounts": "marketplace_list_accounts",
    "conduit_marketplace_list_sessions": "marketplace_list_sessions",
    "conduit_marketplace_get_session": "marketplace_get_session",
    "conduit_marketplace_bootstrap_session": "marketplace_bootstrap_session",
    "conduit_marketplace_run_job": "marketplace_execute_job",
    "conduit_marketplace_enqueue_job": "marketplace_enqueue_job",
    "conduit_marketplace_queue_status": "marketplace_queue_status",
    "conduit_marketplace_get_job": "marketplace_get_job",
    "conduit_marketplace_list_jobs": "marketplace_list_jobs",
    "conduit_marketplace_list_results": "marketplace_list_results",
    "conduit_marketplace_get_result": "marketplace_get_result",
    "conduit_marketplace_export_result": "marketplace_export_result",
}

RESOURCES = [
    Resource(
        uri="conduit://session/status",
        name="session_status",
        description="Current Conduit session status (URL/title/budget when available).",
        mimeType="application/json",
    ),
    Resource(
        uri="conduit://session/audit/tail",
        name="audit_tail",
        description="Recent audit-log events for this session (secrets redacted).",
        mimeType="application/json",
    ),
    Resource(
        uri="conduit://proof/latest",
        name="proof_latest",
        description="Metadata for the most recently exported proof bundle.",
        mimeType="application/json",
    ),
]

PROMPTS = [
    Prompt(
        name="research_with_proof",
        description="Research a topic with Conduit and export a cryptographic proof bundle.",
        arguments=[
            PromptArgument(name="topic", description="Research topic", required=True),
            PromptArgument(name="max_pages", description="Max pages to open (default 3)", required=False),
        ],
    ),
    Prompt(
        name="monitor_page_changes",
        description="Fingerprint a page and check for signed content changes.",
        arguments=[PromptArgument(name="url", description="Page URL to monitor", required=True)],
    ),
    Prompt(
        name="audit_site_structure",
        description="Map/crawl a site structure and export proof.",
        arguments=[
            PromptArgument(name="url", description="Site root URL", required=True),
            PromptArgument(name="depth", description="Crawl depth (optional)", required=False),
        ],
    ),
]


async def _dispatch(tool_name: str, arguments: dict) -> str:
    global _last_proof
    arguments = dict(arguments or {})

    if tool_name == "conduit_eval":
        dry_run = bool(arguments.pop("dry_run", False))
        confirm = bool(arguments.pop("confirm", False))
        script = arguments.get("script") or arguments.get("js_code") or ""
        if dry_run:
            return _truncate_json(
                {
                    "dry_run": True,
                    "would_execute": True if confirm else False,
                    "script": script,
                    "hint": "Re-call with confirm:true (and dry_run:false) to execute. Source is audited verbatim.",
                }
            )
        if not confirm:
            return _truncate_json(
                {
                    "error": "conduit_eval requires confirm:true",
                    "hint": "Set confirm:true after reviewing the script, or dry_run:true to preview.",
                }
            )
        if "script" not in arguments and "js_code" in arguments:
            arguments["script"] = arguments.pop("js_code")

    action = ACTION_MAP.get(tool_name)
    if action is None:
        return _truncate_json({"error": f"Unknown tool: {tool_name}"})

    bridge = await _get_bridge()
    args = {"action": action, **arguments}
    try:
        result = await bridge.execute(args)
        if tool_name == "conduit_export_proof":
            meta = result if isinstance(result, dict) else {"raw": result}
            _last_proof = {
                "path": meta.get("path"),
                "bundle_name": meta.get("bundle_name"),
                "chain_hash": meta.get("chain_hash"),
                "action_count": meta.get("action_count"),
                "resource_uri": "conduit://proof/latest",
            }
            if isinstance(result, dict):
                result = {**result, "resource_uri": "conduit://proof/latest"}
        if isinstance(result, str):
            return _truncate_json(result)
        return _truncate_json(result)
    except Exception as exc:
        return _truncate_json({"error": str(exc), "action": action})


async def _read_resource(uri: str) -> list[TextResourceContents]:
    parsed = urlparse(uri)
    if parsed.scheme != "conduit":
        raise ValueError(f"Unsupported URI scheme (only conduit:// allowed): {uri}")

    path = (parsed.netloc + parsed.path).strip("/")

    if path == "session/status" or uri.rstrip("/") == "conduit://session/status":
        status: dict[str, Any] = {
            "bridge_started": _bridge is not None,
            "note": "Status fields populate after the first tool call starts the bridge.",
        }
        if _bridge is not None:
            for attr in ("current_url", "url", "page_url", "budget_remaining", "action_count"):
                if hasattr(_bridge, attr):
                    try:
                        status[attr] = getattr(_bridge, attr)
                    except Exception:
                        pass
            browser = getattr(_bridge, "_browser_tool", None) or getattr(_bridge, "browser", None)
            if browser is not None:
                page = getattr(browser, "page", None) or getattr(browser, "_page", None)
                if page is not None:
                    try:
                        status["url"] = page.url
                    except Exception:
                        pass
                    try:
                        status["title"] = await page.title()
                    except Exception:
                        pass
        return [TextResourceContents(uri=uri, mimeType="application/json", text=json.dumps(status, default=str))]

    if path.startswith("session/audit") or "audit/tail" in uri:
        events: list[Any] = []
        note = "No audit API exposed on bridge; call tools then export_proof for full chain."
        if _bridge is not None:
            audit = getattr(_bridge, "audit", None) or getattr(_bridge, "_audit_log", None)
            if audit is not None and hasattr(audit, "tail"):
                try:
                    events = list(audit.tail(50))
                    note = "Last up to 50 events via audit.tail."
                except Exception as exc:
                    note = f"audit.tail failed: {exc}"
            elif audit is not None and hasattr(audit, "recent"):
                try:
                    events = list(audit.recent(50))
                    note = "Last up to 50 events via audit.recent."
                except Exception as exc:
                    note = f"audit.recent failed: {exc}"
        payload = {"events": events, "note": note, "redacted": True}
        return [TextResourceContents(uri=uri, mimeType="application/json", text=json.dumps(payload, default=str))]

    if path == "proof/latest" or uri.rstrip("/") == "conduit://proof/latest":
        payload = _last_proof or {
            "error": "No proof exported yet",
            "hint": "Call conduit_export_proof first, then re-read conduit://proof/latest.",
        }
        return [TextResourceContents(uri=uri, mimeType="application/json", text=json.dumps(payload, default=str))]

    if path.startswith("proof/"):
        bundle_id = path.split("/", 1)[1]
        payload = {
            "bundle_id": bundle_id,
            "latest": _last_proof,
            "hint": "Use conduit_export_proof; bundle files live on disk under the returned path.",
        }
        return [TextResourceContents(uri=uri, mimeType="application/json", text=json.dumps(payload, default=str))]

    raise ValueError(f"Unknown resource URI: {uri}")


def _get_prompt(name: str, arguments: dict | None) -> GetPromptResult:
    args = arguments or {}
    if name == "research_with_proof":
        topic = args.get("topic", "<topic>")
        max_pages = args.get("max_pages", "3")
        text = (
            f"Research topic: {topic} (max_pages={max_pages}).\n"
            "1) conduit_search with the topic.\n"
            "2) conduit_navigate to the best result URLs.\n"
            "3) conduit_extract_main (fmt=md) on each page.\n"
            "4) conduit_export_proof and read conduit://proof/latest.\n"
            "Prefer accessibility refs from conduit_accessibility_snapshot before clicking."
        )
        return GetPromptResult(
            description="Research with cryptographic proof",
            messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))],
        )
    if name == "monitor_page_changes":
        url = args.get("url", "<url>")
        text = (
            f"Monitor page changes for {url}.\n"
            "1) conduit_navigate to the URL.\n"
            "2) conduit_fingerprint and store the hash.\n"
            "3) Later: conduit_check_changed with that fingerprint.\n"
            "4) conduit_export_proof when done."
        )
        return GetPromptResult(
            description="Monitor page for signed changes",
            messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))],
        )
    if name == "audit_site_structure":
        url = args.get("url", "<url>")
        depth = args.get("depth", "2")
        text = (
            f"Audit site structure for {url} (depth={depth}).\n"
            "1) conduit_navigate to the URL.\n"
            "2) conduit_map then conduit_crawl as needed.\n"
            "3) conduit_export_proof and read conduit://proof/latest."
        )
        return GetPromptResult(
            description="Audit site structure with proof",
            messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))],
        )
    raise ValueError(f"Unknown prompt: {name}")


def create_server() -> Server:
    """Build the low-level MCP server (mcp 1.x decorators or mcp 2.x handlers)."""
    server = Server("conduit-browser")

    if hasattr(server, "list_tools") and hasattr(server, "call_tool"):
        @server.list_tools()
        async def list_tools() -> list[Tool]:
            return TOOLS

        @server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[TextContent]:
            result = await _dispatch(name, arguments or {})
            return [TextContent(type="text", text=result)]

        if hasattr(server, "list_resources") and hasattr(server, "read_resource"):
            @server.list_resources()
            async def list_resources() -> list[Resource]:
                return RESOURCES

            @server.read_resource()
            async def read_resource(uri: str):
                return await _read_resource(str(uri))

        if hasattr(server, "list_prompts") and hasattr(server, "get_prompt"):
            @server.list_prompts()
            async def list_prompts() -> list[Prompt]:
                return PROMPTS

            @server.get_prompt()
            async def get_prompt(name: str, arguments: dict | None):
                return _get_prompt(name, arguments)

        return server

    # mcp 2.x low-level API
    from mcp.types import (
        CallToolRequestParams,
        CallToolResult,
        GetPromptRequestParams,
        ListPromptsResult,
        ListResourcesResult,
        ListToolsResult,
        PaginatedRequestParams,
        ReadResourceRequestParams,
        ReadResourceResult,
    )

    async def _list_tools(_ctx, _params):
        return ListToolsResult(tools=TOOLS)

    async def _call_tool(_ctx, params):
        result = await _dispatch(params.name, params.arguments or {})
        return CallToolResult(content=[TextContent(type="text", text=result)])

    async def _list_resources(_ctx, _params):
        return ListResourcesResult(resources=RESOURCES)

    async def _read_resource_handler(_ctx, params):
        contents = await _read_resource(str(params.uri))
        return ReadResourceResult(contents=contents)

    async def _list_prompts(_ctx, _params):
        return ListPromptsResult(prompts=PROMPTS)

    async def _get_prompt_handler(_ctx, params):
        return _get_prompt(params.name, params.arguments)

    server.add_request_handler("tools/list", PaginatedRequestParams, _list_tools)
    server.add_request_handler("tools/call", CallToolRequestParams, _call_tool)
    server.add_request_handler("resources/list", PaginatedRequestParams, _list_resources)
    server.add_request_handler("resources/read", ReadResourceRequestParams, _read_resource_handler)
    server.add_request_handler("prompts/list", PaginatedRequestParams, _list_prompts)
    server.add_request_handler("prompts/get", GetPromptRequestParams, _get_prompt_handler)
    return server


async def main():
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
