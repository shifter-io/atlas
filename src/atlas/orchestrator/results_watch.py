"""INGEST-HANG-2: an alert when /v1/results stops answering uploads.

On 26 Nov 2026 every upload waited behind a stuck import for hours and nothing said so until runs
were lost. The upload handler (api.results) tells WATCH when a request comes in and how it was
answered. The job `results_watch` (every minute) raises the major alert `results_stalled` when
this process has answered no upload 2xx for `results.stalled_alert_minutes` (default 5) while
nodes are uploading: an upload is still in flight, or one was answered with a status a node
retries (5xx, 408, 429) since the last 2xx. A refusal the node does not retry (400, 404, 409,
422) is an answer: it neither starts nor ends a stall. Quiet hours with no upload raise nothing.

The alert (one row, `results_stalled`, bumped each minute the stall lasts, so the server alerts
post it once) is acknowledged by the job's first tick after an upload is answered 2xx again. The
state is the process's own: a restart counts the stall from the start, and its first 2xx
acknowledges an alert an earlier process left open.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .db import Database, setting
from .events import raise_alert

KIND = "results_stalled"
RETRIED = (408, 429)  # with every 5xx: the statuses a node retries (node.NodeAgent._call)


@dataclass
class ResultsWatch:
    """The upload handler's state in this process (monotonic seconds)."""

    started: float = field(default_factory=time.monotonic)
    ok_at: float | None = None  # the last 2xx
    failed_at: float | None = None  # the last answer a node retries
    open: int = 0  # uploads being received or imported now
    # When the job last raised the alert; -inf: an earlier process may have left it open.
    alerted_at: float | None = float("-inf")

    def began(self) -> None:
        self.open += 1

    def ended(self, status: int, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.open -= 1
        if 200 <= status < 300:
            self.ok_at = now
        elif status >= 500 or status in RETRIED:
            self.failed_at = now

    def stalled(self, now: float, limit: float) -> float | None:
        """The seconds since the last 2xx when that is at least `limit` while uploads wait;
        else None."""
        since = self.started if self.ok_at is None else self.ok_at
        waiting = self.open > 0 or (self.failed_at is not None and self.failed_at > since)
        silent = now - since
        return silent if waiting and silent >= limit else None


WATCH = ResultsWatch()


async def check(
    db: Database, watch: ResultsWatch = WATCH, now: float | None = None
) -> dict[str, Any]:
    """The job `results_watch`: raise `results_stalled` during a stall, acknowledge it after."""
    now = time.monotonic() if now is None else now
    async with db.connection() as conn:
        minutes = float(await setting(conn, "results.stalled_alert_minutes"))
    silent = watch.stalled(now, minutes * 60)
    if silent is not None:
        message = (
            f"/v1/results answered no upload for {int(silent // 60)} min "
            f"({watch.open} uploads in flight); nodes keep retrying until their run's hour ends"
        )
        data = {"silent_s": int(silent), "in_flight": watch.open, "minutes": minutes}
        async with db.transaction() as conn:
            await raise_alert(conn, KIND, message, severity="major", dedupe_key=KIND, data=data)
        watch.alerted_at = now
        return {"stalled_s": int(silent), "in_flight": watch.open}
    ok, alerted = watch.ok_at, watch.alerted_at
    if ok is not None and alerted is not None and ok > alerted:
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE alerts SET acknowledged_at=now() WHERE dedupe_key=%s "
                "AND acknowledged_at IS NULL",
                (KIND,),
            )
        watch.alerted_at = None
        return {"recovered": True}
    return {}
