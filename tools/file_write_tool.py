"""File-write tool for ephemeral job artifact storage."""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Any

from . import register_tool

log = logging.getLogger(__name__)


async def file_write(*, path: str, content: str, _job_dir: Path | None = None, **kwargs: Any) -> dict[str, Any]:
    if _job_dir is None:
        return {"ok": False, "error": "no_job_dir"}
    root = Path(_job_dir)
    if not root.is_dir():
        return {"ok": False, "error": "no_job_dir"}
    candidate = Path(path)
    if candidate.is_absolute():
        return {"ok": False, "error": "path_escape_attempt"}
    root = root.resolve(strict=True)
    safe_path = (root / candidate).resolve(strict=False)
    try:
        safe_path.relative_to(root)
    except ValueError:
        return {"ok": False, "error": "path_escape_attempt"}
    # Existing symlink components have already been resolved above. Reject any
    # symlink leaf as well so writes cannot retarget between validation and IO.
    if safe_path.exists() and safe_path.is_symlink():
        return {"ok": False, "error": "symlink_path_rejected"}
    safe_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        safe_path.parent.resolve(strict=True).relative_to(root)
    except (OSError, ValueError):
        return {"ok": False, "error": "path_escape_attempt"}
    safe_path.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(safe_path), "size": len(content)}


FILE_WRITE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file_write",
        "description": "Write a file to the job's ephemeral storage. Path must be relative to the job dir.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within job dir, e.g. 'report.md' or 'output/data.json'",
                },
                "content": {"type": "string", "description": "File contents (UTF-8 text)"},
            },
            "required": ["path", "content"],
        },
    },
}


def register() -> None:
    register_tool("file_write", file_write, FILE_WRITE_SCHEMA)
