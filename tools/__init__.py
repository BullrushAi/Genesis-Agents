"""Tool registry for the agent runtime. Tools are async callables returning JSON-serializable dicts."""
from __future__ import annotations
import copy
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

_TOOLS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {}
_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {}
def register_tool(name: str, fn: Callable[..., Awaitable[dict[str, Any]]], schema: dict[str, Any]) -> None:
    """Register a tool once; reject shadowing by another implementation."""
    if name in _TOOLS:
        if _TOOLS[name] is fn and _TOOL_SCHEMAS[name] == schema:
            return
        raise RuntimeError(f"duplicate tool registration: {name}")
    _TOOLS[name] = fn
    _TOOL_SCHEMAS[name] = schema


def get_tool(name: str) -> Callable[..., Awaitable[dict[str, Any]]] | None:
    return _TOOLS.get(name)


def tool_schemas_for(allowed: list[str]) -> list[dict[str, Any]]:
    """Return OpenAI-format tool definitions for the allowed tool names."""
    schemas: list[dict[str, Any]] = []
    for name in allowed:
        if name not in _TOOL_SCHEMAS:
            continue
        schema = copy.deepcopy(_TOOL_SCHEMAS[name])
        # Runtime-injected context is never model-controlled. Close the
        # top-level argument object even when an older schema omitted the JSON
        # Schema keyword (whose permissive default is unsafe for tool calls).
        parameters = schema.get("function", {}).get("parameters")
        if isinstance(parameters, dict) and parameters.get("type") == "object":
            parameters.setdefault("additionalProperties", False)
        schemas.append(schema)
    return schemas


def validate_tool_arguments(name: str, arguments: Any) -> dict[str, Any] | None:
    """Validate model-emitted arguments against the registered tool schema.

    This intentionally implements the bounded JSON-Schema subset used by the
    registry.  Returning a structured refusal keeps malformed, missing,
    unexpected, wrong-type, and enum-invalid values away from tool code.
    """
    schema = _TOOL_SCHEMAS.get(name)
    if schema is None:
        return {"ok": False, "error": "unknown_tool_schema", "tool": name}
    parameters = schema.get("function", {}).get("parameters") or {}
    if not isinstance(arguments, dict):
        return {"ok": False, "error": "invalid_tool_arguments", "tool": name, "reason": "arguments_must_be_object"}
    properties = parameters.get("properties") or {}
    required = parameters.get("required") or []
    missing = [key for key in required if key not in arguments]
    if missing:
        return {"ok": False, "error": "invalid_tool_arguments", "tool": name, "reason": "missing_required", "fields": missing}
    unexpected = sorted(key for key in arguments if key not in properties or str(key).startswith("_"))
    if unexpected:
        return {"ok": False, "error": "invalid_tool_arguments", "tool": name, "reason": "unexpected_arguments", "fields": unexpected}

    expected_python = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    for key, value in arguments.items():
        rule = properties.get(key) or {}
        json_type = rule.get("type")
        python_type = expected_python.get(json_type)
        # bool is an int subclass; JSON integer/number must not accept it.
        wrong_numeric_bool = json_type in {"integer", "number"} and isinstance(value, bool)
        if python_type is not None and (wrong_numeric_bool or not isinstance(value, python_type)):
            return {"ok": False, "error": "invalid_tool_arguments", "tool": name, "reason": "wrong_type", "field": key, "expected": json_type}
        if "enum" in rule and value not in rule["enum"]:
            return {"ok": False, "error": "invalid_tool_arguments", "tool": name, "reason": "enum_violation", "field": key}
    return None


def register_default_tools() -> None:
    """Idempotent - auto-discover every *_tool.py module and call its register() function."""
    import importlib
    from pathlib import Path

    tools_dir = Path(__file__).parent
    for entry in sorted(tools_dir.glob("*_tool.py")):
        module_name = entry.stem
        try:
            mod = importlib.import_module(f".{module_name}", package=__name__)
            if hasattr(mod, "register"):
                mod.register()
                log.debug("registered tool module: %s", module_name)
        except Exception:
            log.exception("failed to register tool module %s", module_name)
            raise
