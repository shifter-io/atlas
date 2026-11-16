"""The `*` fleet IP memos (fleetsql.py) with the methodology's country_verdicts (COUNTRY-MIN-IPS-1).

The `*` totals count every observation of the window; with country_verdicts only the counted
runs' (incremental.counted_only(): their network's country counted when they started). The build
already holds those runs' bucket rows, so fleetsql takes them from it (Counted) instead of
reading every run_buckets row: a bucket's runs digest is RUN_SETS' digest of its counted runs, and
a bucket, slice or fold reads exactly their observations, an index range per (run, bucket) or
(run, slice).

The fold into calculated_ip_last_seen only ever adds (greatest()), so a run that stops counting
after its bucket was folded would stay in it: when a verdict imported after the last fold was
decided before the newest folded bucket ended, or the setting changed since, both IP memos are
emptied and counted again (ruled()). Daily tests are imported minutes after they start and the
hourly calculation runs after that (orchestrator/jobs.py), so that is rare.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_left
from datetime import timedelta
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
LATE = (
    "SELECT EXISTS (SELECT FROM country_verdicts v WHERE v.run_key>%s AND v.decided_at<"
    "(SELECT max(bucket_start) FROM calculated_bucket_ips)+interval '5 minutes')"
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


class Counted:
    """The counted runs' bucket rows of a build, sorted by bucket and run key (each read takes
    the rows of its time range only)."""

    def __init__(self, rows, rule):
        self.rows, self.rule = rows, rule  # rule: policy.country_verdicts()

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


def ruled(conn, rule, fold):
    """Empty both IP memos when they were folded under another country rule (rule:
    policy.country_verdicts(); none recorded: without it; verdicts.same()) or, with it, a
    verdict imported since the last fold rules on a folded bucket; record the rule and the
    newest verdict when the build folds (fold) or emptied them."""
    cur = conn.cursor(row_factory=tuple_row)
    found = cur.execute(READ, (STATE,)).fetchone()
    state = found[0] if found else {"on": False}
    emptied = not verdicts.same(state, rule)
    if rule and not emptied:
        emptied = cur.execute(LATE, (state["through"],)).fetchone()[0]
    for statement in EMPTY if emptied else ():
        cur.execute(statement)
    if emptied or fold:
        newest = cur.execute(NEWEST).fetchone()[0]
        cur.execute(WRITE, (STATE, Jsonb(verdicts.params(rule) | {"through": newest})))
