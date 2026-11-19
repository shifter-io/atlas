"""SCORE-HISTORY-BACKFILL-1: recompute the Atlas Score history points the current build's trends
lack (`atlas calculate-backfill-scores`).

The trends (score_history.spark()) read one record per slot, of their build's own methodology (or
one with the same Atlas Scores, score_history.same_scores()): the 23 hours before the current
build's as_of (24h) and the 29 UTC days before its day (7d, 30d). A missing point is such a slot
without a record of the methodology: an hour, at as_of - k hours, or a
day, at its last hour on the builds' grid (23:MM, MM as_of's minute). Points before the first
stored run finished have nothing to measure and are left out.

Each point is the real calculation (calculator.calculate(), incremental) at that cutoff, in its own
transaction, which is rolled back once the built payload's networks are scored: no build, list,
memo or day record of it stays. Its networks' balanced scores are then written with source
"backfill" in a transaction of their own, so a run stopped half way keeps the points done and the
next run goes on from there (resumable), and running it again computes nothing new (idempotent). A
published record is never replaced: a recomputed one has its own key (its source), and a spark
takes the published one of the same as_of. A recomputed record of an earlier methodology is
replaced by the next run under the new one.

A point holds the calculation's advisory lock (rollups.LOCK) for its whole transaction, as every
build does, so it never runs alongside the hourly build: a point starts only while no build holds
the lock (else the run stops with Busy), and a build that starts meanwhile waits for the point to
end. A point costs about a recount at its cutoff (an older cutoff reuses no stored list), so
`--limit` bounds the points of one run. Each point runs in a process of its own (FORK), which ends
with it, so no point's memory (an hourly build's, about 7.5 GB at production's size) stays for the
next: a run of several points needs one point's memory, as its MemoryMax is set for one build.

Production computes no point itself (a point holds the calculation lock for about a recount, so it
would hold up the hourly builds): the points are computed on a copy of its database elsewhere (the
staging copy), written out by export() and recorded in production by import_scores(), which takes
the lock for a moment only and refuses records of another methodology than its current build's. A
point computed on a copy equals production's while the copy holds the facts its windows read.

A point sees the stored facts as they are now (late runs included, raw rows older than
storage.RAW_DAYS archived) and keeps no earlier build's score (retained scores come from the
previous build, and the current one is newer than the point).
"""

from __future__ import annotations

import json
import multiprocessing
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

from ..storage import connect
from . import rollups, run_lock
from .calculator import calculate
from .latency_store import utc_day
from .policy import load
from .score_history import DAY, HOUR, SLOTS, same_scores, slots_back

SOURCE = "backfill"
FORK = True  # each point in a forked process of its own (False: in this one)
CURRENT = "SELECT b.as_of FROM current_calculation JOIN calculation_builds b USING(build_id)"
FIRST = "SELECT min(finished_at) FROM test_runs"
STORED = (
    "SELECT period,as_of FROM calculated_score_history "
    "WHERE methodology_id=ANY(%s) AND as_of>=%s AND as_of<%s"
)
METHOD = (
    "SELECT b.methodology_id FROM current_calculation JOIN calculation_builds b USING(build_id)"
)
FIELDS = ("provider", "kind", "period", "as_of", "score", "methodology_id")
EXPORTED = (
    f"SELECT {','.join(FIELDS)} FROM calculated_score_history "
    f"WHERE source='{SOURCE}' AND methodology_id=%s ORDER BY as_of,provider,kind,period"
)
WRITE = (
    f"INSERT INTO calculated_score_history VALUES (%s,%s,%s,%s,%s,%s,'{SOURCE}') "
    "ON CONFLICT(provider,kind,period,as_of,source) DO UPDATE SET score=excluded.score,"
    "methodology_id=excluded.methodology_id"
)


class Busy(run_lock.Busy):
    """A build holds the calculation's lock: no point starts beside it (a calculation's
    run_lock.Busy, raised by a point's calculate(), is one too)."""


class _Scored(Exception):  # noqa: N818 (raised to roll the point's build back, not an error)
    """Raised in calculate()'s transaction with the point's history rows, so it rolls back."""

    def __init__(self, rows):
        super().__init__("point scored")
        self.rows = rows


def missing(conn, method_id, hours=SLOTS["24h"] - 1, days=SLOTS["30d"] - 1):
    """(the current build's as_of, its trends' points without a record of method_id or of a
    methodology with the same Atlas Scores, oldest first, wanted()); (None, []) without a current
    build."""
    row = conn.execute(CURRENT).fetchone()
    if row is None:
        return None, []
    as_of, first = row[0], conn.execute(FIRST).fetchone()[0]
    since = min(utc_day(as_of) - days * DAY, as_of - (hours + 1) * HOUR)
    ids = same_scores(conn, method_id, since, as_of)
    stored = conn.execute(STORED, (ids, since, as_of)).fetchall()
    return as_of, wanted(as_of, stored, first, hours, days)


def wanted(as_of, stored, first, hours, days):
    """The points, oldest first, of the 24h trend's last hours hours and the 30d trend's last
    days earlier days that have no record in stored ((period, as_of) of the build's methodology),
    after first (the first stored run's end; None: no run)."""
    slots = defaultdict(set)  # each trend's slots with a record (24h: hours, 30d: days)
    for period, at in stored:
        if period in ("24h", "30d"):
            slots[period].add(slots_back(as_of, at, period))
    found = {as_of - k * HOUR for k in range(1, hours + 1) if k not in slots["24h"]}
    minute = (as_of - utc_day(as_of)) % HOUR  # the builds' grid: as_of's minute of the hour
    ends = {k: utc_day(as_of) - (k - 1) * DAY - HOUR + minute for k in range(1, days + 1)}
    found |= {at for k, at in ends.items() if k not in slots["30d"]}
    return sorted(at for at in found if first is not None and at > first)


def backfill(database, methodology, *, hours=23, days=29, limit=None, dry_run=False):
    """Recompute and record (dry_run: only compute) the current build's missing trend points,
    oldest first, at most limit of them; returns what was done and how many points are left."""
    _, method_id = load(methodology)
    with connect(database) as conn:
        as_of, todo = missing(conn, method_id, hours, days)
    done = []
    for at in todo[:limit]:
        started = time.monotonic()
        rows = isolated(database, methodology, at)
        if not dry_run:
            write(database, rows)
        scores = {f"{p}/{k}/{period}": s for p, k, period, _, s, _ in rows}
        seconds = round(time.monotonic() - started, 1)
        done.append({"as_of": at.isoformat(), "seconds": seconds, "scores": scores})
    return {
        "methodology_id": method_id,
        "current_as_of": as_of and as_of.isoformat(),
        "dry_run": dry_run,
        "points": done,
        "left": [at.isoformat() for at in todo[len(done) :]],
    }


def isolated(database, methodology, at):
    """point() in a forked process of its own, which ends with it (FORK), else in this one."""
    if not FORK:
        return point(database, methodology, at)
    with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("fork")) as pool:
        return pool.submit(point, database, methodology, at).result()


def point(database, methodology, at):
    """The history rows of a calculation at cutoff at, its build rolled back (Busy while a
    build holds the lock)."""
    with connect(database) as conn:
        if not conn.execute(f"SELECT pg_try_advisory_lock({rollups.LOCK})").fetchone()[0]:
            raise Busy(f"A calculation is running; no point starts beside it ({at.isoformat()})")
        conn.execute(f"SELECT pg_advisory_unlock({rollups.LOCK})")
    try:
        calculate(database, methodology, as_of=at, publish=_scored, mode="incremental")
    except _Scored as scored:
        return scored.rows
    raise RuntimeError("The calculation returned without scoring the point")


def _scored(conn, payload):
    """calculate()'s publish step for a point: its networks' balanced scores, as RECORD reads a
    published head (score_history.py), raised so the build rolls back."""
    as_of = payload["as_of"]
    as_of = datetime.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    method_id, rows = payload["methodology"]["id"], []
    for n in payload.get("networks") or ():
        for label, w in n["windows"].items():
            score = w["scores"].get("balanced", {}).get("score")
            rows.append((n["provider"], n["kind"], label, as_of, score, method_id))
    raise _Scored(rows)


def write(database, rows):
    """Record a point's rows as recomputed (source "backfill"), in a transaction of their own."""
    with connect(database) as conn:
        conn.cursor().executemany(WRITE, rows)


def export(database, method_id, path):
    """Write the database's recomputed records of methodology method_id to path (JSON) for
    import_scores()."""
    with connect(database) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(EXPORTED, (method_id,)).fetchall()
    records = [dict(zip(FIELDS, row, strict=True)) | {"as_of": row[3].isoformat()} for row in rows]
    Path(path).write_text(json.dumps({"methodology_id": method_id, "records": records}, indent=1))
    return {"methodology_id": method_id, "records": len(records), "path": str(path)}


def import_scores(database, path):
    """Record export()'s records as recomputed (source "backfill"), again or not (idempotent), in
    one short transaction holding the calculation's lock (Busy while a build holds it), and only
    when every record is of the current build's methodology (else ValueError, nothing written)."""
    found = json.loads(Path(path).read_text())
    rows = [tuple(r[k] for k in FIELDS) for r in found["records"]]
    rows = [(*r[:3], datetime.fromisoformat(r[3]), *r[4:]) for r in rows]
    with connect(database) as conn:
        if not conn.execute(f"SELECT pg_try_advisory_xact_lock({rollups.LOCK})").fetchone()[0]:
            raise Busy("A calculation is running; import between builds")
        current = conn.execute(METHOD).fetchone()
        method_id = current and current[0]
        if method_id is None or {found["methodology_id"], *(r[5] for r in rows)} != {method_id}:
            raise ValueError(f"The records are not of the current build's methodology {method_id}")
        conn.cursor().executemany(WRITE, rows)
    return {"methodology_id": method_id, "imported": len(rows)}
