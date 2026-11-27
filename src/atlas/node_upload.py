"""INGEST-HANG-2: how long and how often a node retries a finished run's upload.

On 26 Nov 2026 the orchestrator's upload path was blocked for well over ten minutes. Nodes gave up
after 8 tries (2 s doubling to 256 s, about 9 to 10 minutes) and one hung try could hold an
upload for 30 minutes (the session's 1,800 s read timeout): thousands of pulse runs of two hours
were lost as stale.

Now a node retries a run's upload until the end of the UTC hour the run started in (an on-demand
pulse droplet is removed by then anyway), and for at least MIN_WINDOW after its first try, so a
run finished just before the hour ends keeps the old window. Between tries it waits a doubling
backoff capped at BACKOFF_MAX, with up to a quarter of jitter, or the orchestrator's longer
Retry-After (at most RETRY_AFTER_MAX; INGEST-HANG-1's 503 ingest_busy). A try that gets no reply
byte for READ_SECONDS is given up and retried; a census upload keeps the session's longer
timeout, since a large census is imported before the reply. A retried upload is safe: the
orchestrator answers a stored run from its run key, and an import skips a run it already has.
While the node retries, each try that reaches the orchestrator keeps the run `uploading`
(nodework.begin_upload), which the reaper fails only after 3 x batch.stale_seconds without one.

The node's shutdown ends the wait: one last try, then the upload gives up (UploadGaveUp).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from .node import Active

log = logging.getLogger(__name__)

BACKOFF_MAX = 60.0  # seconds: the longest wait of the node's own between two tries
RETRY_AFTER_MAX = 300.0  # seconds: the longest Retry-After an upload waits
MIN_WINDOW = timedelta(minutes=10)  # the shortest retry window, from the first try
READ_SECONDS = 60.0  # an upload try (not a census's) with no reply byte for this long is retried
HOUR = timedelta(hours=1)


class UploadGaveUp(RuntimeError):
    """A run's upload was not accepted before its retry window ended or the node stopped."""


def retry_after(exc: BaseException) -> float:
    """A refused upload's Retry-After in seconds (503 ingest_busy, INGEST-HANG-1), else 0."""
    headers = exc.headers if isinstance(exc, aiohttp.ClientResponseError) else None
    try:
        return min(float((headers or {}).get("Retry-After", 0)), RETRY_AFTER_MAX)
    except ValueError:
        return 0.0


def window_end(start: datetime | None, now: datetime) -> datetime:
    """When the upload retries of a run that started at `start` end: the end of its UTC hour, at
    least MIN_WINDOW after `now` (no start known: MIN_WINDOW)."""
    end = now + MIN_WINDOW
    if start is None:
        return end
    hour = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return max(end, hour + HOUR)


def backoff(base: float, attempt: int, wait: float = 0.0) -> float:
    """The wait after failed try `attempt` (0 the first): `base` doubling up to BACKOFF_MAX, with
    up to a quarter of jitter; or the orchestrator's `wait` (Retry-After) when longer."""
    delay = min(base * 2 ** min(attempt, 32), BACKOFF_MAX) * (1 + random.random() / 4)
    return max(delay, wait)


def timeout(kind: str | None) -> aiohttp.ClientTimeout | None:
    """An upload try's timeout: READ_SECONDS without a reply byte; None for a census (the
    session's)."""
    if kind == "census":
        return None
    return aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=READ_SECONDS)


@dataclass
class Window:
    """One upload's retries: until `end`, or until `stop` (the node's shutdown) is set."""

    end: datetime
    stop: asyncio.Event
    base: float
    options: dict[str, Any] = field(default_factory=dict)  # each try's request options
    attempt: int = 0  # the failed tries so far

    @classmethod
    def of(cls, active: Active | None, base: float) -> Window:
        """The window of the run `active` (None, an unknown run: MIN_WINDOW from now)."""
        now = datetime.now(UTC)
        if active is None:
            return cls(window_end(None, now), asyncio.Event(), base)
        assignment = active.assignment
        start = active.start or datetime.fromisoformat(assignment["start_at"])
        limit = timeout(assignment.get("kind"))
        options = {"timeout": limit} if limit is not None else {}
        return cls(window_end(start, now), active.stop, base, options)

    async def wait(self, batch_id: int, error: BaseException) -> None:
        """The wait after a failed try; UploadGaveUp when the window is over or the node is
        stopping (a stop during the wait ends it early: one last try)."""
        name = type(error).__name__
        remaining = (self.end - datetime.now(UTC)).total_seconds()
        if remaining <= 0 or self.stop.is_set():
            raise UploadGaveUp(f"upload of batch {batch_id} not accepted ({name})")
        delay = min(backoff(self.base, self.attempt, retry_after(error)), remaining)
        self.attempt += 1
        log.warning("upload of batch %s failed (%s); retry in %.0fs", batch_id, name, delay)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.stop.wait(), delay)
