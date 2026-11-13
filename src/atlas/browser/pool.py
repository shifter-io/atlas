"""One host's pool of browser processes, leased one try at a time (D8; docs/browser.html, Runner).

Every run on a host shares one BrowserPool. Leases are granted first in, first out, so runs
waiting together interleave. A process is launched on first use, replaced after
`recycle_after` tries, and replaced as soon as a lease ends with it disconnected (a crash) or
a lease is granted with it disconnected. A pool may launch several engines (one launch per
engine): its `size` bounds the open browsers of all of them together, a lease asks for one
engine (`pool.browser(engine)`, or through `pool.engine(name)`, that engine's pool) and prefers
a free process of that engine, then an empty slot; a free process of another engine is closed
and replaced (a switch). Every launch is matched by exactly one close: a recycle, a crash, a
switch or the pool's close (`launches == recycles + crashes + switches + closes` once closed).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from . import procstats


class Pooled(Protocol):
    """What the pool needs of a browser process (driver.Browser)."""

    def alive(self) -> bool: ...
    async def close(self) -> None: ...


@dataclass(slots=True)
class _Slot:
    browser: Any = None
    tries: int = 0
    engine: str | None = None


def resident_bytes() -> int | None:
    """Resident memory of this process's descendants (browsers and their drivers), from ps;
    None when ps cannot tell."""
    table = procstats.read_ps()
    if table is None:
        return None
    return sum(table[pid].rss_bytes for pid in procstats.descendants(table, os.getpid()))


Launch = Callable[[], Awaitable[Pooled]]


class EnginePool:
    """One engine's pool on a shared BrowserPool: what a run's transport leases from."""

    def __init__(self, pool: BrowserPool, engine: str) -> None:
        self.pool, self.name = pool, engine
        self.refused_networks = pool.refused_networks

    def browser(self) -> contextlib.AbstractAsyncContextManager[Any]:
        return self.pool.browser(self.name)


class BrowserPool:
    """`size` browser processes from `launch()` (or `launch[engine]()`, a launch per engine),
    leased with `async with pool.browser()`."""

    def __init__(
        self,
        size: int,
        launch: Launch | Mapping[str, Launch],
        *,
        recycle_after: int,
        memory: Callable[[], int | None] = resident_bytes,
    ) -> None:
        for name, value in (("size", size), ("recycle_after", recycle_after)):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name}: must be a positive integer")
        self.size, self.recycle_after = size, recycle_after
        self._launches = dict(launch) if isinstance(launch, Mapping) else {None: launch}
        self._memory = memory
        self._idle: deque[_Slot] = deque(_Slot() for _ in range(size))
        self._waiters: deque[asyncio.Future[_Slot]] = deque()
        names = ("leases", "waits", "launches", "recycles", "crashes", "switches", "closes")
        self._counts = dict.fromkeys(names, 0)
        self._closed = False
        # Networks a transport already logged as refused: logged once per host (one pool).
        self.refused_networks: set[tuple[str | None, str | None]] = set()

    async def __aenter__(self) -> BrowserPool:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    def engine(self, name: str) -> EnginePool:
        """The pool of one of the launch's engines."""
        if name not in self._launches:
            raise ValueError("engine: the pool has no launch for it")
        return EnginePool(self, name)

    def _take_idle(self, engine: str | None) -> _Slot:
        """A free slot: one with a process of `engine`, else an empty one, else the oldest."""
        slot = min(self._idle, key=lambda s: (s.engine != engine, s.browser is not None))
        self._idle.remove(slot)
        return slot

    async def _acquire(self, engine: str | None) -> _Slot:
        if self._closed:
            raise RuntimeError("the browser pool is closed")
        if self._idle and not self._waiters:
            return self._take_idle(engine)
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

    async def _retire(self, slot: _Slot, engine: str | None = None) -> None:
        """Close the slot's process when it crashed, is due for recycling, is not of `engine`
        (when given) or the pool closed."""
        if slot.browser is None:
            return
        if not slot.browser.alive():
            self._counts["crashes"] += 1
        elif slot.tries >= self.recycle_after:
            self._counts["recycles"] += 1
        elif engine is not None and slot.engine != engine:
            self._counts["switches"] += 1
        elif self._closed:
            self._counts["closes"] += 1
        else:
            return
        browser, slot.browser, slot.tries, slot.engine = slot.browser, None, 0, None
        with contextlib.suppress(Exception):
            await browser.close()

    @contextlib.asynccontextmanager
    async def browser(self, engine: str | None = None) -> AsyncIterator[Any]:
        """Wait for a free slot (first in, first out, whatever the engine) and hold a process
        of `engine` (default: the launch's first) for one try."""
        engine = next(iter(self._launches)) if engine is None else engine
        launch = self._launches[engine]
        slot = await self._acquire(engine)
        try:
            # Died while idle, or of another engine: replace it before the try.
            await self._retire(slot, engine)
            if slot.browser is None:
                slot.browser, slot.engine = await launch(), engine
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
        """Leases, waits, launches, recycles, crashes, switches and closes so far, and resident
        memory."""
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
