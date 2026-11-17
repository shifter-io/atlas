"""The `*` fleet IP memos (fleetsql.py) with the methodology's country_verdicts (COUNTRY-MIN-IPS-1).

The `*` totals count every observation of the window; with country_verdicts only the counted
runs' (incremental.counted_only(): their network's country counted when they started). The build
already holds those runs' bucket rows, so fleetsql takes them from it (Counted) instead of
reading every run_buckets row: a bucket's runs digest is RUN_SETS' digest of its counted runs, and
a bucket, slice or fold reads exactly their observations, an index range per (run, bucket) or
(run, slice).

The fold into calculated_ip_last_seen only ever adds (greatest()), so a run that stops counting
after its bucket was folded would stay in it: when the setting changed since the last fold, or a
run folded then may have stopped counting, both IP memos are emptied and counted again (ruled()).
A run that starts counting changes its buckets' digests, which are folded again: that only adds.

A verdict imported after the last fold changes nothing a fold holds unless it was decided before
the newest folded bucket ended (late): each hour's daily tests start on the hour and are imported
during it, after the hourly build has folded the minutes before, so most hours have late
verdicts (CALC-SCALE-2: emptying for each of them counted the whole window again every hour). A
verdict only changes whether runs started at or after it count, so each fold records the runs
started in the day before its cutoff that did not count (Counted.record()); the memos are emptied
only when a run of that day that existed then does not count now but did then (Counted.stopped()),
or a late verdict is older than that day.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_left
from datetime import datetime, timedelta
from itertools import groupby

from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from .. import verdicts

# fleetsql's BUCKET_IPS, FOLD and SPAN over the given runs only.
PAIRS = "unnest(%s::timestamptz[],%s::bigint[]) k(t,run_key) JOIN ip_observations o "
PAIRS += "ON o.run_key=k.run_key AND o.bucket_start=k.t"
BUCKET_IPS = f"SELECT k.t AS bucket_start,count(DISTINCT o.ip_id) AS ips FROM {PAIRS} GROUP BY 1"
FOLD = f"""INSERT INTO calculated_ip_last_seen(ip_id,last_seen)
SELECT o.ip_id,max(o.bucket_start) FROM {PAIRS} GROUP BY 1
ON CONFLICT(ip_id) DO UPDATE SET last_seen=greatest(calculated_ip_last_seen.last_seen,
 excluded.last_seen)"""
SPAN = """SELECT count(DISTINCT o.ip_id) FROM unnest(%s::bigint[]) k(run_key)
JOIN ip_observations o ON o.run_key=k.run_key AND o.bucket_start>=%s AND o.bucket_start<%s"""
# The setting and the newest verdict's run key the memos were folded with: a calculated_distinct
# row (a memo no collection deletes).
STATE = "fleet-country-verdicts"
READ = "SELECT counts FROM calculated_distinct WHERE digest=%s"
WRITE = (
    "INSERT INTO calculated_distinct(digest,counts) VALUES (%s,%s) "
    "ON CONFLICT(digest) DO UPDATE SET counts=excluded.counts,last_used=now()"
)
NEWEST = "SELECT coalesce(max(run_key),-9223372036854775807) FROM country_verdicts"
# The oldest verdict imported since the fold (run key above %s) decided before the newest folded
# bucket ended (NULL: none), and that end.
LATE = (
    "SELECT min(v.decided_at),f.until FROM (SELECT max(bucket_start)+interval '5 minutes' "
    "AS until FROM calculated_bucket_ips) f JOIN country_verdicts v ON v.run_key>%s "
    "AND v.decided_at<f.until GROUP BY f.until"
)
EMPTY = ("DELETE FROM calculated_bucket_ips", "DELETE FROM calculated_ip_last_seen")
# The IP state (ipstate.py) folds the same buckets: emptied with them, counted again from scratch.
STATE_TABLES = (
    "calculated_scope_ip_last_seen",
    "calculated_ip_vectors",
    "calculated_ip_vector_state",
)
EMPTY += tuple(f"DELETE FROM {table}" for table in STATE_TABLES)
STEP = timedelta(minutes=5)  # a stored bucket
# How far before its cutoff a fold records the runs that did not count (Counted.record()).
RECORDED = timedelta(days=1)


class Counted:
    """The counted runs' bucket rows of a build, sorted by bucket and run key (each read takes
    the rows of its time range only), and its runs' keys: the newest, and the start of each that
    did not count (RUNS' counted)."""

    def __init__(self, rows, rule, runs=()):
        self.rows, self.rule = rows, rule  # rule: policy.country_verdicts()
        self.newest = max((r["run_key"] for r in runs), default=None)
        self.uncounted = {r["run_key"]: r["started_at"] for r in runs if not r["counted"]}

    def record(self, as_of):
        """What a fold at as_of records for stopped(): the runs that did not count among those
        started in the RECORDED before it, and the newest run."""
        since = as_of - RECORDED
        excluded = sorted(k for k, at in self.uncounted.items() if at >= since)
        return {"since": since.isoformat(), "newest": self.newest, "excluded": excluded}

    def stopped(self, state, late, until):
        """Whether a run folded under state (record()'s) may have stopped counting, given the
        oldest late verdict (decided at `late`, before the folded buckets end at `until`): a
        verdict changes whether runs started at or after it count, so with late in the recorded
        day only runs started there, before until, that existed then can have stopped: those not
        counting now that counted then."""
        if "since" not in state or late < datetime.fromisoformat(state["since"]):
            return True
        since, before = datetime.fromisoformat(state["since"]), set(state["excluded"])
        return any(
            k <= state["newest"] and k not in before and since <= at < until
            for k, at in self.uncounted.items()
        )

    def inside(self, start, end):
        """The rows of the buckets in [start, end)."""
        lo = bisect_left(self.rows, start, key=_at)
        return self.rows[lo : bisect_left(self.rows, end, lo, key=_at)]

    def digests(self, start, end):
        """{bucket: RUN_SETS' digest of its counted runs} of the buckets in [start, end)."""
        return {
            at: hashlib.md5(",".join(str(b["run_key"]) for b in rows).encode()).hexdigest()
            for at, rows in groupby(self.inside(start, end), key=_at)
        }

    def pairs(self, buckets):
        """The (buckets, run keys) arrays of the counted runs' rows in `buckets`."""
        found = [b for t in buckets for b in self.inside(t, t + STEP)]
        return [b["bucket_start"] for b in found], [b["run_key"] for b in found]

    def keys(self, start, end):
        """The counted runs with a bucket in [start, end)."""
        return sorted({b["run_key"] for b in self.inside(start, end)})


def _at(row):
    return row["bucket_start"]


def ruled(conn, counted, fold, as_of):
    """Empty both IP memos when they were folded under another country rule (counted: the
    build's Counted, its rule policy.country_verdicts(), or None without the rule; none
    recorded: without it; verdicts.same()) or, with it, a run folded then may have stopped
    counting since (a late verdict: Counted.stopped()); record the rule, the newest verdict and
    Counted.record() at as_of when the build folds (fold) or emptied them."""
    rule = counted.rule if counted is not None else None
    cur = conn.cursor(row_factory=tuple_row)
    found = cur.execute(READ, (STATE,)).fetchone()
    state = found[0] if found else {"on": False}
    emptied = not verdicts.same(state, rule)
    if rule and not emptied:
        late = cur.execute(LATE, (state["through"],)).fetchone()
        emptied = late is not None and counted.stopped(state, *late)
    for statement in EMPTY if emptied else ():
        cur.execute(statement)
    if emptied or fold:
        newest = cur.execute(NEWEST).fetchone()[0]
        record = verdicts.params(rule) | {"through": newest}
        record |= counted.record(as_of) if rule else {}
        cur.execute(WRITE, (STATE, Jsonb(record)))
