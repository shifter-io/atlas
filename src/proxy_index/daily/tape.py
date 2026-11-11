"""The live tape over stored pulse batches (SCORE-DATA-1b): `proxy-index daily tape`.

The orchestrator calls detect() every minute; a span of batches stored without it (the score
inputs of a load, SIMULATE.md) gets its tape here. replay() sets detect's watermark just before
--from, then calls detect(conn, moment) in time order, a step at a time, each call its own
transaction, until the watermark is just before --to: every batch that finished in [from, to)
is judged once, and the events (pulse_finished, down, recovered, success_drop, latency_spike,
block_spike, the hosting audits) and outages are detect's own. Events are stamped with the
step's moment, at most a step after their batch finished, as the minute calls stamp them.

Running it again over the same span adds nothing: detect skips a batch that has its pulse event
(outages._had), and a silent pulse is judged the same again (its verdict reads earlier batches
only). Another detect() moving the watermark meanwhile (a running orchestrator) stops it.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from .. import detect
from ..errors import UserError

STEP = timedelta(minutes=5)
TICK = timedelta(microseconds=1)  # the watermark is exclusive: batches finished after it
# The span's events, written after `event_id` at the replay's moments, per kind and network.
EVENTS = (
    "SELECT e.kind,coalesce(p.provider,'') AS network,count(*) FROM test_events e "
    "LEFT JOIN products p USING(product_id) WHERE e.event_id>%s AND e.at>%s AND e.at<=%s "
    "GROUP BY 1,2 ORDER BY 1,2"
)
# The outages the span's batches opened (incidents), per network, and those still open.
OUTAGES = (
    "SELECT p.provider,count(*),count(*) FILTER (WHERE o.ended_at IS NULL) FROM outages o "
    "JOIN test_batches b USING(batch_id) JOIN products p ON p.product_id=o.product_id "
    "WHERE b.finished_at>=%s AND b.finished_at<%s GROUP BY 1 ORDER BY 1"
)
NEWEST = "SELECT coalesce(max(event_id),0) FROM test_events"


class TapeError(UserError):
    """Another detect() moved the watermark while the tape ran (stop the orchestrator first)."""


def watermark(conn) -> datetime | None:
    row = conn.execute("SELECT value FROM settings WHERE key=%s", (detect.WATERMARK,)).fetchone()
    return datetime.fromisoformat(row[0]["finished_at"]) if row else None


def replay(conn, since: datetime, until: datetime, *, step=STEP, dry_run=False, config=None):
    """detect() over the batches that finished in [since, until); returns the report: the span,
    the steps, the events per kind and network, the span's outages and the seconds it took.

    Each step commits on its own, as the orchestrator's calls do. A dry run takes every step in
    one transaction, so a later step sees the earlier ones' events, then rolls back."""
    cfg = config or detect.thresholds()
    settle, began = timedelta(seconds=cfg["settle_seconds"]), time.monotonic()
    newest = conn.execute(NEWEST).fetchone()[0]
    upto, steps = since - TICK, 0
    try:
        detect.set_watermark(conn.cursor(), upto, upto + settle)
        while upto < until - TICK:
            _commit(conn, dry_run)
            if watermark(conn) != upto:
                raise TapeError(f"another detect() moved the watermark from {upto.isoformat()}")
            upto = min(upto + step, until - TICK)
            detect.detect(conn, upto + settle, config=cfg)
            steps += 1
        _commit(conn, dry_run)
        report = {"from": since.isoformat(), "to": until.isoformat(), "dry_run": dry_run}
        report |= {"steps": steps} | tallies(conn, newest, since, until, settle)
    finally:
        if dry_run:
            conn.rollback()
    return report | {"seconds": round(time.monotonic() - began, 3)}


def _commit(conn, dry_run: bool) -> None:
    if not dry_run:
        conn.commit()


def tallies(conn, newest: int, since: datetime, until: datetime, settle: timedelta) -> dict:
    events: dict = {}
    for kind, network, count in conn.execute(EVENTS, (newest, since, until + settle)):
        events.setdefault(kind, {})[network] = count
    outages = {
        network: {"outages": count, "open": still}
        for network, count, still in conn.execute(OUTAGES, (since, until))
    }
    return {"events": events, "outages": outages}
