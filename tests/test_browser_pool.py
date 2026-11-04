"""BrowserPool: FIFO leases fair across runs, recycling, crash replacement, stats and close."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from proxy_index.browser import pool as pool_module
from proxy_index.browser.pool import BrowserPool, resident_bytes


class FakeBrowser:
    def __init__(self, number: int) -> None:
        self.number = number
        self.connected = True
        self.closed = False

    def alive(self) -> bool:
        return self.connected and not self.closed

    async def close(self) -> None:
        self.closed = True


class Launcher:
    def __init__(self, fail: bool = False) -> None:
        self.browsers: list[FakeBrowser] = []
        self.fail = fail

    async def __call__(self) -> FakeBrowser:
        if self.fail:
            raise OSError("no browser")
        self.browsers.append(FakeBrowser(len(self.browsers)))
        return self.browsers[-1]


def make(size=1, recycle_after=100, launch=None) -> tuple[BrowserPool, Launcher]:
    launch = launch or Launcher()
    return BrowserPool(size, launch, recycle_after=recycle_after, memory=lambda: 1234), launch


async def test_two_runs_interleave_first_in_first_out():
    pool, _ = make(size=1)
    order: list[str] = []

    async def run(name: str) -> None:
        for _ in range(3):
            async with pool.browser():
                order.append(name)
                await asyncio.sleep(0)

    await asyncio.gather(run("a"), run("b"))
    assert order == ["a", "b", "a", "b", "a", "b"]
    stats = pool.stats()
    assert stats["leases"] == 6 and stats["waits"] >= 5 and stats["launches"] == 1
    assert stats["resident_bytes"] == 1234 and stats["size"] == 1


async def test_recycles_after_n_tries():
    pool, launch = make(recycle_after=2)
    seen = []
    for _ in range(5):
        async with pool.browser() as browser:
            seen.append(browser.number)
    assert seen == [0, 0, 1, 1, 2]
    assert [b.closed for b in launch.browsers] == [True, True, False]
    assert pool.stats()["recycles"] == 2 and pool.stats()["launches"] == 3


async def test_a_crashed_browser_is_replaced():
    pool, launch = make()
    with pytest.raises(RuntimeError):
        async with pool.browser() as browser:
            browser.connected = False
            raise RuntimeError("crash")
    async with pool.browser() as browser:
        assert browser.number == 1
    assert launch.browsers[0].closed and pool.stats()["crashes"] == 1


async def test_a_failed_launch_frees_its_slot():
    pool, _ = make(launch=Launcher(fail=True))
    for _ in range(2):
        with pytest.raises(OSError):
            async with pool.browser():
                pass
    assert pool.stats()["launches"] == 0


async def test_a_cancelled_waiter_leaves_the_queue():
    pool, _ = make()
    async with pool.browser():
        waiter = asyncio.create_task(pool.browser().__aenter__())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    async with asyncio.timeout(1), pool.browser() as browser:
        assert browser.number == 0


async def test_a_waiter_cancelled_after_its_grant_passes_the_slot_on():
    pool, _ = make()
    granted = []
    lease = pool.browser()
    await lease.__aenter__()
    first = asyncio.create_task(pool._acquire())
    second = asyncio.create_task(pool._acquire())
    await asyncio.sleep(0)
    await lease.__aexit__(None, None, None)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    granted.append(await asyncio.wait_for(second, 1))
    assert granted and not pool._idle


async def test_close_closes_idle_and_leased_browsers_and_refuses_waiters():
    pool, launch = make(size=2)
    async with pool.browser():
        pass
    async with pool:
        lease = pool.browser()
        held = await lease.__aenter__()
        other = pool.browser()
        await other.__aenter__()
        waiter = asyncio.create_task(pool.browser().__aenter__())
        await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="closed"):
        await waiter
    assert not held.closed
    await lease.__aexit__(None, None, None)
    await other.__aexit__(None, None, None)
    assert all(b.closed for b in launch.browsers)
    with pytest.raises(RuntimeError, match="closed"):
        async with pool.browser():
            pass


@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_size_and_recycle_after_must_be_positive(bad):
    with pytest.raises(ValueError, match="size"):
        BrowserPool(bad, Launcher(), recycle_after=1)
    with pytest.raises(ValueError, match="recycle_after"):
        BrowserPool(1, Launcher(), recycle_after=bad)


def test_resident_bytes_sums_this_process_descendants(monkeypatch):
    me = pool_module.os.getpid()
    listing = f"{me} 1 500\n10 {me} 100\n11 10 50\n12 1 999\n"

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=listing)

    monkeypatch.setattr(pool_module.subprocess, "run", fake_run)
    assert resident_bytes() == 150 * 1024


def test_resident_bytes_is_none_without_ps(monkeypatch):
    def fail(*args, **kwargs):
        raise FileNotFoundError("ps")

    monkeypatch.setattr(pool_module.subprocess, "run", fail)
    assert resident_bytes() is None
