"""A budgeted network's browser tries on one host leave its pulses their connections
(BROWSER-BUDGET-1; docs/browser.html, Runner).

proxy-cheap's budget (daily/budget.py) keeps BROWSER_RESERVE of its 100 connections for its
browser tests, but a heavy page opens a tunnel per origin: 2 browsers on its mini opened far
more than 10, and its pulses fell from about 99% to 2-12% while a LinkedIn run lasted (17 Nov
2026). A budgeted network's tries on its host therefore share one Gate:

- one try at a time (`tries`), whatever its engine, so that try has the whole reserve;
- at most `limit` gateway connections at once, counted in the try's relay (relay.py), which
  every connection of both engines goes through, so the limit is exactly what the gateway sees.
  A connection over it waits; while one waits, the connection idle longest is closed: its last
  byte came from the gateway (an answer ended) IDLE_S or more ago. A browser keeps idle
  keep-alive tunnels open for minutes and never learns of the gate, so without this a page
  needing more origins than the limit would wait for its own idle tunnels; a browser reopens a
  closed idle connection by itself. A connection whose last byte went to the gateway waits for
  an answer (a CONNECT, a TLS handshake, a request with a slow first byte: proxy-cheap answers
  slowly), and a browser does not resend a request on a fresh connection, so it is closed only
  after PENDING_S (a client's HTTP/2 window update or ping acknowledgement must not keep a
  really idle connection open for ever).

`stats()` gives the limit, the peak, the connections that waited and the idle ones closed since
the last call (the host's hour line). A network without a budget has no gate and runs as before.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable

IDLE_S = 1.0  # a connection answered this long ago may be closed for a waiting one
PENDING_S = 10.0  # the same for one whose last byte went to the gateway (an answer pending)
POLL_S = 0.1  # how often a waiting connection looks for an idle one to close
TRIES = 1


class Lease:
    """One gateway connection the gate counts, until `release()`."""

    def __init__(self, gate: Gate) -> None:
        self.gate = gate
        self.last = gate.clock()
        self.pending = True  # its last byte went to the gateway: an answer may be on its way
        self.writers: tuple[asyncio.StreamWriter, ...] = ()
        self.closing = False

    def open(self, writers: Iterable[asyncio.StreamWriter]) -> None:
        """Its browser and gateway sides are both connected: it may be closed once idle."""
        self.writers = tuple(writers)
        self.last = self.gate.clock()

    def touch(self, *, up: bool) -> None:
        """A byte went to the gateway (`up`) or came from it."""
        self.last, self.pending = self.gate.clock(), up

    def idle(self, now: float) -> float | None:
        """How long it has been idle past its limit (negative before); None while it cannot be
        closed (closing, not open)."""
        if self.closing or not self.writers:
            return None
        return now - self.last - (PENDING_S if self.pending else self.gate.idle_s)

    def close(self) -> None:
        """Both sides end now; the relay then releases the lease."""
        self.closing = True
        for writer in self.writers:
            writer.transport.abort()

    def release(self) -> None:
        self.gate._release(self)


class Gate:
    """`limit` gateway connections and TRIES tries at once for one network on one host."""

    def __init__(self, limit: int, *, idle_s: float = IDLE_S) -> None:
        if limit < 1:
            raise ValueError("a gate holds at least one connection")
        self.limit, self.idle_s = limit, idle_s
        self.tries = asyncio.Semaphore(TRIES)
        self._held: set[Lease] = set()
        self._freed = asyncio.Event()
        self.peak = self.waits = self.closed_idle = 0

    @staticmethod
    def clock() -> float:
        return asyncio.get_running_loop().time()

    @property
    def held(self) -> int:
        return len(self._held)

    async def acquire(self) -> Lease:
        """A lease once fewer than `limit` are held, closing idle ones while it waits."""
        waited = False
        while len(self._held) >= self.limit:
            if not waited:
                self.waits, waited = self.waits + 1, True
            self._close_idle()
            self._freed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._freed.wait(), POLL_S)
        lease = Lease(self)
        self._held.add(lease)
        self.peak = max(self.peak, len(self._held))
        return lease

    def _close_idle(self) -> None:
        """Close the lease idle longest past its limit, if none is closing already (its
        release frees the place a waiting connection needs)."""
        if any(lease.closing for lease in self._held):
            return
        now = self.clock()
        idle = [(lease.idle(now), lease) for lease in self._held]
        found = [(seconds, lease) for seconds, lease in idle if seconds is not None]
        if found:
            seconds, lease = max(found, key=lambda pair: pair[0])
            if seconds >= 0:
                self.closed_idle += 1
                lease.close()

    def _release(self, lease: Lease) -> None:
        if lease in self._held:
            self._held.discard(lease)
            self._freed.set()

    def stats(self) -> dict[str, int]:
        """The limit and, since the last call, the peak, waits and idle connections closed."""
        found = {"limit": self.limit, "peak": self.peak, "waits": self.waits}
        found["closed_idle"] = self.closed_idle
        self.peak, self.waits, self.closed_idle = len(self._held), 0, 0
        return found
