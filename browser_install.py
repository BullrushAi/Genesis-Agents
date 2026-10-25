"""Lazy Patchright Chromium install.

Render wipes ``~/.cache/ms-playwright`` on every restart. The gateway used to start a Chromium download in the
background on EVERY boot, which competes for the 512Mi instance's memory and disk while it is also recovering work.
Now nothing is downloaded at boot: the first code path that actually launches Chromium calls
:func:`ensure_chromium_installed`, which installs once (guarded by an asyncio lock, so concurrent first uses share one
download) and is a no-op afterwards.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

logger = logging.getLogger(__name__)

_INSTALL_TIMEOUT_S = 600.0
_install_lock = asyncio.Lock()


def _browsers_dir() -> str:
    return os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.expanduser("~/.cache/ms-playwright")


def chromium_installed() -> bool:
    """Same test the boot-time install used: the browser cache directory exists and is not empty."""
    cache_dir = _browsers_dir()
    return os.path.isdir(cache_dir) and any(True for _ in os.scandir(cache_dir))


async def ensure_chromium_installed() -> None:
    """Install Patchright's Chromium if it is missing. Never raises: a failed install surfaces as the caller's own
    launch error, exactly as it did when the boot-time download failed."""
    if chromium_installed():
        return
    async with _install_lock:
        if chromium_installed():  # another caller finished the install while we waited for the lock
            return
        logger.info("Patchright Chromium not found - installing on first use")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "patchright",
                "install",
                "chromium",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                code = await asyncio.wait_for(proc.wait(), timeout=_INSTALL_TIMEOUT_S)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.error("Patchright Chromium install timed out after %ss", _INSTALL_TIMEOUT_S)
                return
            if code != 0:
                logger.error("Patchright Chromium install exited with code %s", code)
        except Exception as exc:
            logger.warning("Could not run patchright install: %s", exc)
