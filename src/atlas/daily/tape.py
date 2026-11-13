"""The live tape over stored pulse batches (SCORE-DATA-1b): `atlas daily tape`.

The orchestrator calls detect() every minute; a span of batches stored without it (the score
inputs of a load, SIMULATE.md) gets its tape here. replay() sets detect's watermark just before
--from and moves it, in time order, until it is just before --to: every batch that finished in
[from, to) is judged once, and the events (pulse_finished, down, recovered, success_drop,
latency_spike, block_spike, the hosting audits) and outages are detect's own. Events are stamped
with the moment of the step their batch finished in (at most a step after it), as the minute
calls stamp them, plus settle_seconds.

The fast replay (TAPE-FAST-1, the default) judges an hour of batches a transaction with detect's
judging functions and an in-memory lookup (detect_replay): a fixed number of round trips an hour
plus the event and outage writes. The step replay (fast=False) calls detect(conn, moment) every
step, each call its own transaction; both give the same events and outages, and a span begun
by one and finished by the other matches either alone.

Running it again over the same span adds nothing: detect skips a batch that has its pulse event
(outages._had), and a silent pulse is judged the same again (its verdict reads earlier batches
only). Another detect() moving the watermark meanwhile (a running orchestrator) stops it.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from .. import detect
from ..detect_replay import HOUR, TICK, Replay
from ..errors import UserError

STEP = timedelta(minutes=5)
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


def replay(conn, since: datetime, until: datetime, *, step=STEP, dry_run=False, fast=True):
    """detect() over the batches that finished in [since, until); returns the report: the span,
    the steps, the events per kind and network, the span's outages and the seconds it took.

    Each hour (fast) or step commits on its own, as the orchestrator's calls do. A dry run takes
    them all in one transaction, so a later one sees the earlier ones' events, then rolls back."""
    cfg = detect.thresholds()
    settle, began = timedelta(seconds=cfg["settle_seconds"]), time.monotonic()
    newest = conn.execute(NEWEST).fetchone()[0]
    upto, end = since - TICK, until - TICK
    stride = step * max(1, HOUR // step) if fast else step
    try:
        detect.set_watermark(conn.cursor(), upto, upto + settle)
        hours = Replay(conn, cfg, since, until) if fast else None
        while upto < end:
            _commit(conn, dry_run)
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (detect.LOCK,))
            if watermark(conn) != upto:
                raise TapeError(f"another detect() moved the watermark from {upto.isoformat()}")
            moment = min(upto + stride, end)
            if hours:
                hours.hour(upto, moment, step)
            else:
                detect.detect(conn, moment + settle, config=cfg)
            upto = moment
        _commit(conn, dry_run)
        report = {"from": since.isoformat(), "to": until.isoformat(), "dry_run": dry_run}
        report |= {"steps": -((since - until) // step)} | tallies(
            conn, newest, since, until, settle
        )
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
