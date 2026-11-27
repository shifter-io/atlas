"""INGEST-HANG-2: an alert when /v1/results stops answering uploads.

On 26 Nov 2026 every upload waited behind a stuck import for hours and nothing said so until runs
were lost. The upload handler (api.results) tells WATCH when a request comes in and how it was
answered. The job `results_watch` (every minute) raises the major alert `results_stalled` when
nodes have been uploading for `results.stalled_alert_minutes` (default 5) and this process has
answered none of those uploads 2xx. The wait begins with the first upload after a 2xx (or after a
quiet spell) and lasts while an upload is in flight or one was answered with a status a node
retries (5xx, 408, 429); a 2xx ends it. A refusal the node does not retry (400, 404, 409, 422)
is an answer: with nothing else in flight or retried, it ends the wait too. A quiet spell with no
upload raises nothing, and neither does the first upload after it.

The alert (one row, `results_stalled`, bumped each minute the stall lasts, so the server alerts
post it once) is acknowledged by the job's first tick after an upload is answered 2xx again. The
state is the process's own: after a restart the first 2xx acknowledges an alert an earlier
process left open.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .db import Database, setting
from .events import raise_alert

KIND = "results_stalled"
RETRIED = (408, 429)  # with every 5xx: the statuses a node retries (node.NodeAgent._call)


@dataclass
class ResultsWatch:
    """The upload handler's state in this process (monotonic seconds)."""

    ok_at: float | None = None  # the last 2xx
    waiting_at: float | None = None  # when the uploads now waiting for a 2xx began to wait
    retrying: bool = False  # an answer a node retries came since the last 2xx
    open: int = 0  # uploads being received or imported now
    # When the job last raised the alert; -inf: an earlier process may have left it open.
    alerted_at: float | None = float("-inf")

    def began(self, now: float | None = None) -> None:
        if self.waiting_at is None:
            self.waiting_at = time.monotonic() if now is None else now
        self.open += 1

    def ended(self, status: int, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.open -= 1
        if 200 <= status < 300:
            self.ok_at, self.retrying = now, False
            self.waiting_at = now if self.open else None
        elif status >= 500 or status in RETRIED:
            self.retrying = True
        elif not self.open and not self.retrying:
            self.waiting_at = None  # a refusal for good, and nothing else waits

    def stalled(self, now: float, limit: float) -> float | None:
        """How long uploads have waited for a 2xx, when that is at least `limit`; else None."""
        if self.waiting_at is None:
            return None
        waited = now - self.waiting_at
        return waited if waited >= limit else None


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
            f"/v1/results answered no upload 2xx for {int(silent // 60)} min of uploads "
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
