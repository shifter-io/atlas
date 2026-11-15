"""PROXY-CHEAP-BUDGET-1: the most connections a node holds open at once over all its runs.

A pulse droplet of a network with a connection budget (daily.budget) runs `atlas node
--max-connections N`, N its share of the budget for the hour. The hour's two hourly pulse targets
(http and https) start half a step apart and overlap by design, so each run takes at most
N // RUNS_AT_ONCE connections (never more than it was assigned, at least 1), and a run whose
share is not free waits for an earlier one to finish: the node never holds more than N.
"""

from __future__ import annotations

import asyncio
import contextlib

RUNS_AT_ONCE = 2
WAIT_SECONDS = 1.0  # how often a waiting run re-checks its stop


class ConnectionPool:
    def __init__(self, limit: int) -> None:
        if type(limit) is not int or limit < 1:
            raise ValueError("max connections must be a positive integer")
        self.limit, self.used = limit, 0
        self._freed = asyncio.Condition()

    def share(self, assigned: int) -> int:
        """The connections one run takes: its assignment, at most its part of the limit."""
        return max(1, min(assigned, self.limit // RUNS_AT_ONCE))

    async def acquire(self, count: int, stop: asyncio.Event) -> bool:
        """Wait until `count` connections are free and take them; False when stopped first."""
        async with self._freed:
            while self.used + count > self.limit and not stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._freed.wait(), WAIT_SECONDS)
            if stop.is_set():
                return False
            self.used += count
            return True

    async def release(self, count: int) -> None:
        async with self._freed:
            self.used -= count
            self._freed.notify_all()
