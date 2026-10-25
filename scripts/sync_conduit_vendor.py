"""Vendor the Conduit MCP server (committed HEAD only) into vendor/conduit.

Usage: python scripts/sync_conduit_vendor.py [--src PATH] [--ref HEAD]

The gateway drives Conduit through its stdio MCP server
(vendor/conduit/conduit_mcp_server.py). We vendor from `git archive` so
uncommitted edits in the Conduit checkout never reach production.
"""
from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import tarfile
from pathlib import Path

PATHS = [
    "conduit_mcp_server.py",
    "conduit_platform.py",
    "audit.py",
    "receipt.py",
    "replay.py",
    "tools",
    "skills",
    "requirements.txt",
    "LICENSE",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=r"C:\Users\Work\Desktop\GitHub\Conduit")
    ap.add_argument("--ref", default="HEAD")
    args = ap.parse_args()
    dest = Path(__file__).resolve().parents[1] / "vendor" / "conduit"
    sha = subprocess.check_output(["git", "-C", args.src, "rev-parse", args.ref], text=True).strip()
    blob = subprocess.check_output(["git", "-C", args.src, "archive", "--format=tar", args.ref, *PATHS])
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        tar.extractall(dest)
    for pyc in dest.rglob("__pycache__"):
        shutil.rmtree(pyc, ignore_errors=True)
    (dest / "VENDOR.md").write_text(
        f"# Vendored Conduit\n\nSource: {args.src}\nCommit: {sha}\nRef: {args.ref}\n"
        f"Paths: {', '.join(PATHS)}\n\nRe-sync: `python scripts/sync_conduit_vendor.py`\n",
        encoding="utf-8",
    )
    print(f"vendored conduit@{sha[:12]} -> {dest}")


if __name__ == "__main__":
    main()
