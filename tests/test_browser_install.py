"""Chromium is installed lazily, once, and never at boot."""
from __future__ import annotations

import asyncio
import inspect

import pytest

import browser_install
import main


@pytest.fixture
def empty_cache(tmp_path, monkeypatch):
    cache = tmp_path / "ms-playwright"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    return cache


class _Proc:
    def __init__(self, cache, calls):
        self._cache = cache
        calls.append(1)

    async def wait(self):
        await asyncio.sleep(0.05)  # the download takes a while; concurrent callers pile up on the lock
        self._cache.mkdir(parents=True, exist_ok=True)
        (self._cache / "chromium-1").mkdir(exist_ok=True)
        return 0


def test_lifespan_does_not_start_a_chromium_download():
    source = inspect.getsource(main.lifespan)
    assert "patchright" not in source
    assert "subprocess" not in source


@pytest.mark.asyncio
async def test_concurrent_first_uses_share_one_install(empty_cache, monkeypatch):
    calls: list[int] = []

    async def fake_exec(*args, **kwargs):
        assert args[1:] == ("-m", "patchright", "install", "chromium")
        return _Proc(empty_cache, calls)

    monkeypatch.setattr(browser_install.asyncio, "create_subprocess_exec", fake_exec)
    await asyncio.gather(*(browser_install.ensure_chromium_installed() for _ in range(5)))
    assert len(calls) == 1
    assert browser_install.chromium_installed()


@pytest.mark.asyncio
async def test_nothing_runs_when_chromium_is_already_present(empty_cache, monkeypatch):
    (empty_cache / "chromium-1").mkdir(parents=True)

    async def must_not_run(*args, **kwargs):
        raise AssertionError("install attempted although Chromium is present")

    monkeypatch.setattr(browser_install.asyncio, "create_subprocess_exec", must_not_run)
    await browser_install.ensure_chromium_installed()


@pytest.mark.asyncio
async def test_a_failed_install_does_not_raise_so_the_callers_own_launch_error_surfaces(empty_cache, monkeypatch):
    async def boom(*args, **kwargs):
        raise OSError("no python")

    monkeypatch.setattr(browser_install.asyncio, "create_subprocess_exec", boom)
    await browser_install.ensure_chromium_installed()
    assert not browser_install.chromium_installed()
