"""One host's pool of browser processes, leased one try at a time (D8; docs/browser.html, Runner).

Every run on a host shares one BrowserPool. Leases are granted first in, first out, so runs
waiting together interleave. A process is launched on first use, replaced after
`recycle_after` tries, and replaced as soon as a lease ends with it disconnected (a crash) or
a lease is granted with it disconnected. Every launch is matched by exactly one close: a
recycle, a crash or the pool's close (`launches == recycles + crashes + closes` once closed).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


class Pooled(Protocol):
    """What the pool needs of a browser process (driver.Browser)."""

    def alive(self) -> bool: ...
    async def close(self) -> None: ...


@dataclass(slots=True)
class _Slot:
    browser: Any = None
    tries: int = 0


def resident_bytes() -> int | None:
    """Resident memory of this process's descendants (browsers and their drivers), from ps;
    None when ps cannot tell."""
    try:
        listing = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,rss="],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    children: dict[int, list[int]] = defaultdict(list)
    rss: dict[int, int] = {}
    for line in listing.splitlines():
        pid, ppid, kb = (int(value) for value in line.split())
        children[ppid].append(pid)
        rss[pid] = kb
    total, stack = 0, list(children[os.getpid()])
    while stack:
        pid = stack.pop()
        total += rss[pid] * 1024
        stack.extend(children[pid])
    return total


class BrowserPool:
    """`size` browser processes from `launch()`, leased with `async with pool.browser()`."""

    def __init__(
        self,
        size: int,
        launch: Callable[[], Awaitable[Pooled]],
        *,
        recycle_after: int,
        memory: Callable[[], int | None] = resident_bytes,
    ) -> None:
        for name, value in (("size", size), ("recycle_after", recycle_after)):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name}: must be a positive integer")
        self.size, self.recycle_after = size, recycle_after
        self._launch, self._memory = launch, memory
        self._idle: deque[_Slot] = deque(_Slot() for _ in range(size))
        self._waiters: deque[asyncio.Future[_Slot]] = deque()
        names = ("leases", "waits", "launches", "recycles", "crashes", "closes")
        self._counts = dict.fromkeys(names, 0)
        self._closed = False
        # Networks a transport already logged as refused: logged once per host (one pool).
        self.refused_networks: set[tuple[str | None, str | None]] = set()

    async def __aenter__(self) -> BrowserPool:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def _acquire(self) -> _Slot:
        if self._closed:
            raise RuntimeError("the browser pool is closed")
        if self._idle and not self._waiters:
            return self._idle.popleft()
        self._counts["waits"] += 1
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            slot = await waiter
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():
                self._release(waiter.result())
            else:
                self._waiters.remove(waiter)
            raise
        if self._closed:
            # Granted before close() and resumed after it: nothing launches on a closed pool.
            await self._retire(slot)
            self._idle.append(slot)
            raise RuntimeError("the browser pool is closed")
        return slot

    def _release(self, slot: _Slot) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(slot)
                return
        self._idle.append(slot)

    async def _retire(self, slot: _Slot) -> None:
        """Close the slot's process when it crashed, is due for recycling or the pool closed."""
        if slot.browser is None:
            return
        if not slot.browser.alive():
            self._counts["crashes"] += 1
        elif slot.tries >= self.recycle_after:
            self._counts["recycles"] += 1
        elif self._closed:
            self._counts["closes"] += 1
        else:
            return
        browser, slot.browser, slot.tries = slot.browser, None, 0
        with contextlib.suppress(Exception):
            await browser.close()

    @contextlib.asynccontextmanager
    async def browser(self) -> AsyncIterator[Any]:
        """Wait for a free process (first in, first out) and hold it for one try."""
        slot = await self._acquire()
        try:
            if slot.browser is not None and not slot.browser.alive():
                await self._retire(slot)  # died while idle: replace it before the try
            if slot.browser is None:
                slot.browser = await self._launch()
                self._counts["launches"] += 1
            self._counts["leases"] += 1
            slot.tries += 1
            yield slot.browser
        finally:
            await self._retire(slot)
            if self._closed:
                self._idle.append(slot)
            else:
                self._release(slot)

    def stats(self) -> dict[str, int | None]:
        """Leases, waits, launches, recycles, crashes and closes so far, and resident memory."""
        return {"size": self.size, **self._counts, "resident_bytes": self._memory()}

    async def close(self) -> None:
        """Close every idle process; a leased one is closed when its lease ends."""
        self._closed = True
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_exception(RuntimeError("the browser pool is closed"))
        # A lease ending meanwhile appends its slot to _idle: never iterate it across an await.
        done: list[_Slot] = []
        while self._idle:
            slot = self._idle.popleft()
            await self._retire(slot)
            done.append(slot)
        self._idle.extend(done)
