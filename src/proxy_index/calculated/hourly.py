"""Window values that move with the cutoff (contract K6), kept exact every hour from memos.

A stored list holds what a window's runs fix; these change every hour even when the runs do not:
new IPs (new_ips()), pool counts of time slices (slice_figures(): the daily pools and each
window's range), distinct IPs per series point (bucket_ips()), the estimates' distinct IPs
(window_distinct()) and the lists a newly processed run makes stale (stale_lists()).

They are counted once and kept in the K6 memo tables, so an hour without a new test reads no
observation:

- calculated_run_ranges: the pool figures of a range of one run's observation buckets, keyed by
  the run, its labels' release (0: unenriched) and the bounds. Every single bucket is counted
  when the run is first recorded (they also list its buckets); a slice of at most a day holding
  one range of a run of at most MAX_RANGE_BUCKETS buckets (the whole run, or the prefix or suffix
  a 24-hour edge cuts) is counted the first time a slice needs it.
- calculated_slot_figures: the figures of a slice or point that holds two or more runs (or a
  longer run's range, or a series point no run range has), keyed by the digest of its runs, their
  release, bounds and requested countries and the run-range rows' creation; SQL on a miss.
- calculated_distinct: the estimates' distinct counts, keyed by the digest of the run set.

A slice or point is fully determined by the observation buckets of its runs that it holds, so
equal digests count equal observations. First-seen histograms come from product_ips, which a
late run (one that saw an IP first, before runs already counted) lowers. So every caller
records runs through one step (_register()) that applies the late-run rule to each run recorded
for the first time, whichever build mode or module asked: the ranges of the product's runs with
a bucket since it are deleted and counted again, which changes every slot digest holding them.
Runs ingested after the newest recorded run are checked too, whatever their window (a run
backfilled more than 30 days back, which no window holds). Not covered: a run whose ingest
committed after a build that recorded a newer run (concurrent ingests), when no window holds it.
A stored histogram keeps only the bins a slice of its length can count as new (from the earliest
such slice start). Memos unused for 2 days and the ranges of runs started over 31 days ago are
deleted, once per transaction by the first call that records runs. What a memo miss counts in
the database, and the memo reads and writes, live in hourly_windows.py.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from psycopg.types.json import Jsonb

from .hourly_windows import BUCKET_IPS as BUCKET_IPS
from .hourly_windows import FIRST_SEEN as FIRST_SEEN
from .hourly_windows import HOUR, MEMO_PREFIX, _cached, _count, _restored, _rows, _stored
from .hourly_windows import window_distinct as window_distinct
from .policy import digest
from .sqlwindow import MAX_RANGE_BUCKETS, RUN_BUCKETS, WindowSpec, window_inputs

DAY = timedelta(days=1)
_LAST_BUCKET = timedelta(microseconds=1)  # an inclusive last bucket as a half-open bound
RECORD_RUNS = 100  # runs whose single buckets one statement counts
# Each run's labels' release (0: none) and its recorded ranges there: the single-bucket ones
# list its observation buckets; stamp is when they were counted.
RUNS = """SELECT k.run_key,r.release_id,count(m.run_key) AS ranges,min(m.created_at) AS stamp,
 coalesce(array_agg(m.first_bucket ORDER BY m.first_bucket)
  FILTER (WHERE m.first_bucket=m.last_bucket),'{}') AS buckets
FROM unnest(%s::bigint[]) AS k(run_key)
CROSS JOIN LATERAL (SELECT coalesce(max(e.release_id),0) AS release_id FROM run_enrichments e
 WHERE e.run_key=k.run_key) r
LEFT JOIN calculated_run_ranges m ON m.run_key=k.run_key AND m.release_id=r.release_id
GROUP BY 1,2"""
RECORD = """INSERT INTO calculated_run_ranges(run_key,release_id,first_bucket,last_bucket,figures)
SELECT * FROM unnest(%s::bigint[],%s::int[],%s::timestamptz[],%s::timestamptz[],%s::jsonb[])
ON CONFLICT DO NOTHING"""
RANGES = """SELECT k.slot,{value} AS value
FROM unnest(%s::int[],%s::bigint[],%s::int[],%s::timestamptz[],%s::timestamptz[])
 AS k(slot,run_key,release_id,first_bucket,last_bucket)
JOIN calculated_run_ranges m USING(run_key,release_id,first_bucket,last_bucket)"""
COLLECTED = "SELECT current_setting('proxy_index.memos_collected',true) AS done"
COLLECT = (
    "DELETE FROM calculated_slot_figures WHERE starts_with(digest,'hourly-v1-') "
    "AND last_used<now()-interval '2 days'",
    "DELETE FROM calculated_distinct WHERE starts_with(digest,'hourly-v1-') "
    "AND last_used<now()-interval '2 days'",
    # Run keys follow ingestion, so every key below the oldest recorded run started in the last
    # 31 days belongs to an older run (all of them when none is that recent).
    "DELETE FROM calculated_run_ranges WHERE run_key<coalesce((SELECT m.run_key "
    "FROM calculated_run_ranges m WHERE (SELECT t.started_at FROM test_runs t "
    "WHERE t.run_key=m.run_key)>=now()-interval '31 days' ORDER BY m.run_key LIMIT 1),"
    "(SELECT max(run_key)+1 FROM calculated_run_ranges))",
    "SELECT set_config('proxy_index.memos_collected','1',true)",  # until the transaction ends
)
# The runs to check with the late-run rule, with their product: the ones among %s never recorded
# (no run-range row at any release) and started in the last 31 days (an older one was collected,
# not late), and every unrecorded run ingested after the newest recorded run, whatever its age.
# A run without an observation is never recorded, so it stays here (one index lookup each).
NEW = """SELECT DISTINCT k.run_key,s.product_id FROM (SELECT unnest(%s::bigint[]) AS run_key,
 false AS later UNION ALL SELECT run_key,true FROM test_runs
 WHERE run_key>(SELECT max(run_key) FROM calculated_run_ranges)) k
JOIN test_runs t USING(run_key) JOIN scope_stats s USING(scope_id)
WHERE NOT EXISTS (SELECT FROM calculated_run_ranges m WHERE m.run_key=k.run_key)
 AND (k.later OR t.started_at>=now()-interval '31 days') ORDER BY 1"""
# The late-run rule: per product, the earliest bucket of a new run holding an IP's first
# sighting (product_ips.first_seen) when runs other than the new ones saw that IP too.
LATE = """WITH ip AS (
 SELECT k.product_id,o.ip_id,min(o.first_seen) AS first,min(o.bucket_start) AS bucket,
  count(DISTINCT o.run_key) AS runs
 FROM unnest(%s::bigint[],%s::int[]) AS k(run_key,product_id)
 JOIN ip_observations o ON o.run_key=k.run_key GROUP BY 1,2
)
SELECT ip.product_id,min(ip.bucket) AS since FROM ip
JOIN product_ips m ON m.product_id=ip.product_id AND m.ip_id=ip.ip_id
WHERE m.first_seen=ip.first AND m.run_count>ip.runs GROUP BY 1 ORDER BY 1"""
# The product's recorded runs that may hold such an IP: every one with a bucket since then.
INVALIDATE = """DELETE FROM calculated_run_ranges WHERE run_key IN (SELECT b.run_key
 FROM run_buckets b JOIN test_runs t USING(run_key) JOIN scope_stats s USING(scope_id)
 WHERE s.product_id=%s AND b.bucket_start>=%s) RETURNING run_key"""


@dataclass(frozen=True)
class Run:
    """A run's memo index: its release (0: unenriched), observation buckets and stamp."""

    release: int
    buckets: tuple = ()
    stamp: str = ""

    def ranged(self, first, last):
        """Whether calculated_run_ranges keeps the run's range [first, last]: a single bucket, or
        any range of a run of at most MAX_RANGE_BUCKETS buckets."""
        return first == last or len(self.buckets) <= MAX_RANGE_BUCKETS


@dataclass(eq=False)
class Item:
    """A slice or series point: its runs' clipped bounds [(run_key, first, last)]."""

    key: tuple  # where its value goes: (group key, position)
    since: datetime
    until: datetime
    clips: list
    kind: str = "pool"  # "pool", "first_seen" or "ips"
    product: int = 0
    asked: dict = field(default_factory=dict)


def new_ips(figures, since):
    """IPs of a window first seen by its product at or after since (on the 5-minute grid):
    the bins of figures["first_seen"] from since on. figures may be slice_figures() of any range
    ending at the window's end that holds it: an IP first seen at or after since was seen inside
    the window, so the widest window's bins serve every window of its group."""
    return sum(n for start, n in (figures or {}).get("first_seen", {}).items() if start >= since)


def slice_figures(conn, group_runs, slices):
    """Pool counts of each slice of each group: {key: [figures or None, ...]} in slices' order.

    group_runs maps a key to (product_id, {run_key: requested country}); slices maps it to its
    [(since, until), ...] on the 5-minute grid. A slice's figures are poolfig.pool_figures() of
    its observations (each run labelled by its own newest release, as the calculator reads them;
    shares with the default hosting usage types) plus "first_seen", the IPs per 5-minute bin of
    the product's first sighting from since on; a slice without observations is None, as
    daily_pools() has it. A slice (since, until, "first_seen") gets only {"first_seen": bins}.
    Each comes from the memos (module docstring).
    """
    runs = _runs(conn, [k for _, asked in group_runs.values() for k in asked])
    result, items = {}, []
    for key, (product, asked) in group_runs.items():
        result[key] = [None] * len(slices.get(key, ()))
        for n, (since, until, *only) in enumerate(slices.get(key, ())):
            kind = "first_seen" if only == ["first_seen"] else "pool"
            clips = _clips(runs, sorted(asked), since, until)
            items.append(Item((key, n), since, until, clips, kind, product, asked))
    for item in items:  # without an observation: None, or no first-seen bin
        if not item.clips and item.kind == "first_seen":
            result[item.key[0]][item.key[1]] = {"first_seen": {}}
    items = [item for item in items if item.clips]
    for item, stored in _values(conn, runs, items, "m.figures").items():
        result[item.key[0]][item.key[1]] = _restored(stored, item)
    return result


def bucket_ips(conn, group_runs, buckets):
    """Distinct IPs of each group's runs per series point: {key: {point start: IPs}}.

    group_runs maps a key to its run keys, buckets to (start, until, step): the points are the
    step-wide bins from start (5 minutes for a 24-hour series, else days); a point without
    observations is absent."""
    keys = list(buckets)
    runs = _runs(conn, [k for key in keys for k in group_runs.get(key, ())])
    result, items = {key: {} for key in keys}, []
    for key in keys:
        start, until, step = buckets[key]
        points = defaultdict(dict)
        for run in sorted(set(group_runs.get(key, ()))):
            held = runs[run].buckets
            for at in held[bisect_left(held, start) : bisect_left(held, until)]:
                point = start + (at - start) // step * step
                points[point][run] = (points[point].get(run, (at,))[0], at)
        for point, clips in sorted(points.items()):
            clips = [(run, first, last) for run, (first, last) in sorted(clips.items())]
            items.append(Item((key, point), point, min(point + step, until), clips, "ips"))
    for item, value in _values(conn, runs, items, "(m.figures->'unique_ips')").items():
        if value:
            result[item.key[0]][item.key[1]] = value
    return result


def stale_lists(conn, new_runs):
    """List ids whose stored figures the newly processed runs (new_runs) make stale, so their
    windows are counted again: none.

    The runs are recorded here (_register()), so the late-run rule runs before any window is
    counted: a late run's IPs move product_ips.first_seen, and the ranges of the runs it
    affects are counted again. A list stores nothing a hit window reads that moves with the
    cutoff: its figures.first_seen histogram (CALC-INC-1b) is kept but not read, since a hit's
    new IPs come from slice_figures(), which reads the memos the rule renews. Marking such lists
    stale would recount their windows without rewriting the histogram (lists.write_list() keeps
    a stored list as it is), so none is returned."""
    _runs(conn, new_runs)
    return frozenset()


def _collect(conn):
    """Delete slot and distinct memos unused for 2 days and the ranges of runs over 31 days old,
    once per transaction."""
    if _rows(conn, COLLECTED, ())[0]["done"] == "1":
        return
    for statement in COLLECT:
        conn.execute(statement)


def _runs(conn, run_keys):
    """{run_key: Run} of every run, recording first the ones without ranges at their release."""
    _collect(conn)
    keys = sorted(set(run_keys))
    rows = {r["run_key"]: r for r in _rows(conn, RUNS, (keys,))}
    changed = _register(conn, [k for k, r in rows.items() if not r["ranges"]]) & set(keys)
    if changed:
        rows |= {r["run_key"]: r for r in _rows(conn, RUNS, (sorted(changed),))}
    return {k: Run(r["release_id"], tuple(r["buckets"]), str(r["stamp"])) for k, r in rows.items()}


def _register(conn, missing):
    """Record the single buckets of the runs without ranges at their release (missing), after
    the late-run rule for the runs never recorded; the run keys recorded again or anew."""
    fresh = _rows(conn, NEW, (missing,))
    again = set()
    if fresh:
        keys, products = [r["run_key"] for r in fresh], [r["product_id"] for r in fresh]
        for row in _rows(conn, LATE, (keys, products)):
            deleted = _rows(conn, INVALIDATE, (row["product_id"], row["since"]))
            again |= {r["run_key"] for r in deleted}
    todo = sorted(set(missing) | {r["run_key"] for r in fresh} | again)
    for n in range(0, len(todo), RECORD_RUNS):
        bounds = {"keys": todo[n : n + RECORD_RUNS], "per_run": True, "release": None}
        ranges = {}
        for row in _rows(conn, RUN_BUCKETS, bounds):
            release, run = row["release_id"] or 0, row["run_key"]
            for at in row["buckets"]:
                ranges[run, release, at, at] = (row["product_id"], {run: row["asked"]})
        _store_ranges(conn, ranges)
    return set(todo)


def _store_ranges(conn, ranges):
    """Count and store run ranges ({(run_key, release, first, last): (product_id, asked)}), each
    with the run's labels at that release; their stored figures by key."""
    specs = defaultdict(list)
    for key, (product, asked) in ranges.items():
        run, release, first, last = key
        specs[release].append(WindowSpec(key, product, (run,), first, last + _LAST_BUCKET, asked))
    columns, stored = [[] for _ in range(5)], {}
    for release, group in specs.items():
        found = window_inputs(conn, group, release_mode="newest", release_id=release or None)
        for spec in group:
            stored[spec.key] = _stored(found[spec.key], spec.since, 24, spec.since - DAY)
            for column, value in zip(columns, (*spec.key, Jsonb(stored[spec.key])), strict=True):
                column.append(value)
    if columns[0]:
        conn.execute(RECORD, columns)
    return stored


def _clips(runs, run_keys, since, until):
    """[(run_key, first, last)] of the runs' observation buckets in [since, until)."""
    clips = []
    for run in run_keys:
        held = runs[run].buckets
        i, j = bisect_left(held, since), bisect_left(held, until)
        if i < j:
            clips.append((run, held[i], held[j - 1]))
    return clips


def _values(conn, runs, items, value):
    """{item: stored value}: a run range's (value, an expression of its row m) for a slice of at
    most a day or a point holding one range the run keeps (Run.ranged(); a slice counts and
    stores a missing one), else the slot memo's (counted on a miss)."""
    ranged = [
        item
        for item in items
        if len(item.clips) == 1
        and item.until - item.since <= DAY
        and runs[item.clips[0][0]].ranged(*item.clips[0][1:])
    ]
    columns = [[] for _ in range(5)]
    for slot, item in enumerate(ranged):
        run, first, last = item.clips[0]
        for column, v in zip(columns, (slot, run, runs[run].release, first, last), strict=True):
            column.append(v)
    found = {}
    if columns[0]:
        for row in _rows(conn, RANGES.format(value=value), columns):
            found[ranged[row["slot"]]] = row["value"]
    found |= _new_ranges(conn, runs, [i for i in ranged if i not in found and i.kind != "ips"])
    slots = defaultdict(list)
    for item in items:
        if item not in found:
            slots[_digest(runs, item)].append(item)
    memo = _cached(
        conn,
        "calculated_slot_figures",
        "figures",
        {d: s[0] for d, s in slots.items()},
        lambda missed: _count(conn, missed),
    )
    for key, held in slots.items():
        for item in held:
            found[item] = memo[key] if value == "m.figures" else memo[key]["unique_ips"]
    return found


def _new_ranges(conn, runs, items):
    """{item: stored figures} of slices whose one run range is not stored yet, stored now (a
    series point has no product, so it uses the slot memo instead)."""
    keys, ranges = {}, {}
    for item in items:
        run, first, last = item.clips[0]
        keys[item] = (run, runs[run].release, first, last)
        ranges[keys[item]] = (item.product, {run: item.asked.get(run)})
    stored = _store_ranges(conn, ranges)
    return {item: stored[key] for item, key in keys.items()}


def _digest(runs, item):
    """The slot memo key of an item: what its observations and first-seen bins depend on."""
    if item.kind == "ips":
        clips = [[run, first.isoformat(), last.isoformat()] for run, first, last in item.clips]
        return MEMO_PREFIX + digest(["ips", clips])
    clips = []
    for run, first, last in item.clips:
        asked = item.asked.get(run)
        info = runs[run]
        clips.append(
            [
                run,
                info.release,
                first.isoformat(),
                last.isoformat(),
                asked and asked.lower(),
                info.stamp,
            ]
        )
    hours = (item.until - item.since) / HOUR
    return MEMO_PREFIX + digest([item.kind, hours, item.product, clips])
