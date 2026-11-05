"""Window values that move with the cutoff (contract K6), kept exact every hour from memos.

A stored list holds what a window's runs fix; these change every hour even when the runs do not:
new IPs (new_ips()), pool counts of time slices (slice_figures(): the daily pools and each
window's range), distinct IPs per series point (bucket_ips()), the estimates' distinct IPs
(window_distinct()) and the lists a newly processed run makes stale (stale_lists()).

They are counted once and kept in the K6 memo tables, so an hour without a new test reads no
observation:

- calculated_run_ranges: when a run is first used, the pool figures of each range of its
  observation buckets that a slice or a series point can cut out of it: every single bucket and,
  for a run of at most MAX_RANGE_BUCKETS buckets, every prefix and suffix (a 24-hour slice edge
  cuts such a run once). Keyed by the run, its labels' release (0: unenriched) and the bounds.
- calculated_slot_figures: the figures of a slice or point that holds two or more runs (or a
  range no run-range row has), keyed by the digest of its runs, their release, bounds and
  requested countries and the run-range rows' creation (see the late-run rule); SQL on a miss.
- calculated_distinct: the estimates' distinct counts, keyed by the digest of the run set.

A slice or point is fully determined by the observation buckets of its runs that it holds, so
equal digests count equal observations. First-seen histograms come from product_ips, which a
late run (one that saw an IP before runs already counted) lowers: stale_lists() applies the
late-run rule, deleting the run ranges it makes stale, so they and every slot memo holding them
(whose digest names their creation) are counted again. A stored histogram keeps only the bins a
slice of its length can count as new (from the earliest such slice start). Entries unused for 2
days and the ranges of runs started over 31 days ago are deleted (every slice_figures() call).
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .policy import digest
from .poolfig import pool_figures
from .sqlwindow import MAX_RANGE_BUCKETS, RUN_BUCKETS, WindowSpec, distinct_ips, window_inputs

HOUR, DAY = timedelta(hours=1), timedelta(days=1)
_LAST_BUCKET = timedelta(microseconds=1)  # an inclusive last bucket as a half-open bound
RECORD_RUNS = 100  # runs whose ranges one statement counts
# Distinct IPs per series point: the runs' observations in [start, until), each point the
# step-wide bin from start that holds them (series()'s slot()).
BUCKET_IPS = """SELECT k.slot,date_bin(k.step,o.bucket_start,k.start) AS at,
 count(DISTINCT o.ip_id) AS ips
FROM unnest(%s::int[],%s::bigint[],%s::timestamptz[],%s::timestamptz[],%s::interval[])
 AS k(slot,run_key,start,until,step)
JOIN ip_observations o ON o.run_key=k.run_key AND o.bucket_start>=k.start
 AND o.bucket_start<k.until
GROUP BY 1,2"""
# IPs per 5-minute bin of their product's first sighting (sqlwindow's bins), of each slot's runs'
# observations in [since, until): no labels, so no enrichment join.
FIRST_SEEN = """SELECT k.slot,
 date_bin('5 minutes',m.first_seen,'1970-01-01 00:00:00+00'::timestamptz) AS bin,count(*) AS ips
FROM (SELECT DISTINCT k.slot,k.product_id,o.ip_id
 FROM unnest(%s::int[],%s::bigint[],%s::int[],%s::timestamptz[],%s::timestamptz[])
  AS k(slot,run_key,product_id,since,until)
 JOIN ip_observations o ON o.run_key=k.run_key AND o.bucket_start>=k.since
  AND o.bucket_start<k.until) k
JOIN product_ips m ON m.product_id=k.product_id AND m.ip_id=k.ip_id GROUP BY 1,2"""
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
MEMO = "SELECT digest AS key,{column} AS value FROM {table} WHERE digest=ANY(%s)"
# last_used moves at most daily, so a memo read every hour is not rewritten every hour.
TOUCH = (
    "UPDATE {table} SET last_used=now() WHERE digest=ANY(%s) AND last_used<now()-interval '1 day'"
)
SAVE = (
    "INSERT INTO {table}(digest,{column}) SELECT * FROM unnest(%s::text[],%s::jsonb[]) "
    "ON CONFLICT (digest) DO NOTHING"
)
COLLECT = (
    "DELETE FROM calculated_slot_figures WHERE last_used<now()-interval '2 days'",
    "DELETE FROM calculated_distinct WHERE last_used<now()-interval '2 days'",
    # Run keys follow ingestion, so every key below the oldest recorded run started in the last
    # 31 days belongs to an older run (all of them when none is that recent).
    "DELETE FROM calculated_run_ranges WHERE run_key<coalesce((SELECT m.run_key "
    "FROM calculated_run_ranges m WHERE (SELECT t.started_at FROM test_runs t "
    "WHERE t.run_key=m.run_key)>=now()-interval '31 days' ORDER BY m.run_key LIMIT 1),"
    "(SELECT max(run_key)+1 FROM calculated_run_ranges))",
)
# The runs among %s not yet counted (no run-range row), with their product.
NEW = """SELECT k.run_key,s.product_id FROM unnest(%s::bigint[]) AS k(run_key)
JOIN test_runs t USING(run_key) JOIN scope_stats s USING(scope_id)
WHERE NOT EXISTS (SELECT FROM calculated_run_ranges m WHERE m.run_key=k.run_key)"""
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
# The product's counted runs that may hold such an IP: every one finished since that bucket.
INVALIDATE = """DELETE FROM calculated_run_ranges WHERE run_key IN (SELECT t.run_key
 FROM test_runs t JOIN scope_stats s USING(scope_id) WHERE s.product_id=%s AND t.finished_at>=%s)"""
STALE = "SELECT list_id FROM calculated_lists WHERE product_id=ANY(%s) AND figures ? 'first_seen'"


@dataclass(frozen=True)
class Run:
    """A run's memo index: its release (0: unenriched), observation buckets and stamp."""

    release: int
    buckets: tuple = ()
    stamp: str = ""

    def ranged(self, first, last):
        """Whether calculated_run_ranges holds the run's range [first, last]."""
        n, i, j = (
            len(self.buckets),
            bisect_left(self.buckets, first),
            bisect_left(self.buckets, last),
        )
        return i == j or (n <= MAX_RANGE_BUCKETS and (i == 0 or j == n - 1))


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
    Each comes from the memos (module docstring); this call also collects old memo entries.
    """
    _collect(conn)
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


def window_distinct(conn, run_sets, by_country=True):
    """For each run set of (run_key, country_code) pairs, its distinct IPs by country and in all
    (key None), as estimates.window.distinct() counts them; kept in calculated_distinct."""
    run_sets = [sorted(set(pairs), key=lambda p: (p[0], p[1] or "")) for pairs in run_sets]
    keys = [digest(["distinct", by_country, pairs]) for pairs in run_sets]
    wanted = {key: pairs for key, pairs in zip(keys, run_sets, strict=True) if pairs}

    def count(sets):
        return [
            sorted(c.items(), key=lambda i: i[0] or "")
            for c in distinct_ips(conn, sets, by_country)
        ]

    found = _cached(conn, "calculated_distinct", "counts", wanted, count)
    return [{c: n for c, n in found[key]} if key in found else {} for key in keys]


def stale_lists(conn, new_runs):
    """List ids whose stored figures the newly processed runs (new_runs) make stale, so their
    windows are counted again: the late-run rule.

    A run first processed here (no run-range row yet) that holds the first sighting of an IP
    other runs saw too lowered that IP's product_ips.first_seen, which the memos and the lists
    counted before. Then the run ranges of the product's runs finished since its first such
    bucket are deleted (they, and the slot memos holding them, are counted again), and the
    product's lists that store a first-seen histogram ("first_seen" in figures) are stale."""
    found = _rows(conn, NEW, (sorted(set(new_runs)),))
    if not found:
        return frozenset()
    keys, products = [r["run_key"] for r in found], [r["product_id"] for r in found]
    late = _rows(conn, LATE, (keys, products))
    for row in late:
        conn.execute(INVALIDATE, (row["product_id"], row["since"]))
    if not late:
        return frozenset()
    rows = _rows(conn, STALE, ([row["product_id"] for row in late],))
    return frozenset(row["list_id"] for row in rows)


def _collect(conn):
    """Delete slot and distinct memos unused for 2 days and the ranges of runs over 31 days old."""
    for statement in COLLECT:
        conn.execute(statement)


def _runs(conn, run_keys):
    """{run_key: Run} of every run, recording first the ones without ranges at their release."""
    keys = sorted(set(run_keys))
    rows = {r["run_key"]: r for r in _rows(conn, RUNS, (keys,))}
    missing = [k for k, r in rows.items() if not r["ranges"]]
    for n in range(0, len(missing), RECORD_RUNS):
        if _record(conn, missing[n : n + RECORD_RUNS]):
            rows |= {r["run_key"]: r for r in _rows(conn, RUNS, (missing[n : n + RECORD_RUNS],))}
    return {k: Run(r["release_id"], tuple(r["buckets"]), str(r["stamp"])) for k, r in rows.items()}


def _record(conn, keys):
    """Count and store the ranges of runs (Run.ranged()) at their release; whether any was."""
    bounds = {"keys": keys, "per_run": True, "release": None}
    specs = defaultdict(list)
    for row in _rows(conn, RUN_BUCKETS, bounds):
        held, release, run = row["buckets"], row["release_id"] or 0, row["run_key"]
        if len(held) <= MAX_RANGE_BUCKETS:
            pairs = {(0, j) for j in range(len(held))} | {
                (i, len(held) - 1) for i in range(len(held))
            }
        else:
            pairs = set()
        for i, j in sorted(pairs | {(i, i) for i in range(len(held))}):
            key = (run, release, held[i], held[j])
            since, until = held[i], held[j] + _LAST_BUCKET
            specs[release].append(
                WindowSpec(key, row["product_id"], (run,), since, until, {run: row["asked"]})
            )
    columns = [[] for _ in range(5)]
    for release, group in specs.items():
        found = window_inputs(conn, group, release_mode="newest", release_id=release or None)
        for spec in group:
            stored = _stored(found[spec.key], spec.since, 24, spec.since - DAY)
            for column, value in zip(columns, (*spec.key, Jsonb(stored)), strict=True):
                column.append(value)
    if columns[0]:
        conn.execute(RECORD, columns)
    return bool(columns[0])


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
    most a day or a point holding one recorded range, else the slot memo's (counted on a miss)."""
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


def _digest(runs, item):
    """The slot memo key of an item: what its observations and first-seen bins depend on."""
    if item.kind == "ips":
        clips = [[run, first.isoformat(), last.isoformat()] for run, first, last in item.clips]
        return digest(["ips", clips])
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
    return digest([item.kind, hours, item.product, clips])


def _count(conn, items):
    """The stored values of slot-memo misses, counted in the database."""
    values, pool = {}, [item for item in items if item.kind == "pool"]
    specs = [_spec(n, item) for n, item in enumerate(pool)]
    for spec, inputs in window_inputs(conn, specs, release_mode="per_run", release_id=None).items():
        item = pool[spec]
        hours = (item.until - item.since) // HOUR
        values[id(item)] = _stored(inputs, item.since, hours, _start(item))
    first = [item for item in items if item.kind == "first_seen"]
    for item, bins in zip(
        first, _first_seen(conn, [_spec(n, i) for n, i in enumerate(first)]), strict=True
    ):
        values[id(item)] = {"first_seen": _bins(bins, _start(item))}
    ips = [item for item in items if item.kind == "ips"]
    counted = _point_ips(conn, ips)
    for n, item in enumerate(ips):
        values[id(item)] = {"unique_ips": counted.get(n, 0)}
    return [values[id(item)] for item in items]


def _spec(n, item):
    runs = tuple(run for run, *_ in item.clips)
    return WindowSpec(n, item.product, runs, item.since, item.until, item.asked)


def _start(item):
    """The earliest start of a slice as long as item that holds its clipped bounds: bins before
    it are never new IPs of a slice using the stored value."""
    return min(first for _, first, _ in item.clips) - (item.until - item.since)


def _stored(inputs, since, hours, start):
    """A window's pool figures as a memo stores them: without its new IPs (they depend on the
    slice start) and with the first-seen bins from start on."""
    figures = pool_figures(inputs, since, hours)
    del figures["new_ips"], figures["churn_rate"]
    return figures | {"first_seen": _bins(inputs.first_seen, start)}


def _bins(bins, start):
    return {at.isoformat(): n for at, n in sorted(bins.items()) if at >= start}


def _restored(stored, item):
    """A slice's figures from its stored value (_stored()): new IPs from since on."""
    bins = {}
    for at, n in stored["first_seen"].items():
        if (at := datetime.fromisoformat(at)) >= item.since:
            bins[at] = n
    if item.kind == "first_seen":
        return {"first_seen": bins}
    size = stored["unique_ips"]
    if not size:
        return None
    new = sum(bins.values())
    return stored | {"new_ips": new, "churn_rate": new / size, "first_seen": bins}


def _first_seen(conn, specs):
    """Each spec's first-seen bins (FIRST_SEEN): [{bin start: IPs}] in specs' order."""
    columns = [[] for _ in range(5)]
    for slot, spec in enumerate(specs):
        for run_key in sorted(spec.runs):
            values = (slot, run_key, spec.product_id, spec.since, spec.until)
            for column, value in zip(columns, values, strict=True):
                column.append(value)
    bins = [{} for _ in specs]
    if columns[0]:
        for row in _rows(conn, FIRST_SEEN, columns):
            bins[row["slot"]][row["bin"]] = row["ips"]
    return bins


def _point_ips(conn, items):
    """Each series point's distinct IPs (BUCKET_IPS): {position in items: IPs}."""
    columns = [[] for _ in range(5)]
    for slot, item in enumerate(items):
        for run, *_ in item.clips:
            values = (slot, run, item.since, item.until, item.until - item.since)
            for column, value in zip(columns, values, strict=True):
                column.append(value)
    if not columns[0]:
        return {}
    return {row["slot"]: row["ips"] for row in _rows(conn, BUCKET_IPS, columns)}


def _cached(conn, table, column, wanted, compute):
    """{digest: value} of the memo table's entries for wanted ({digest: input}), computing and
    storing the missing ones (compute(inputs) gives their JSON values in order)."""
    if not wanted:
        return {}
    keys = sorted(wanted)
    query = MEMO.format(column=column, table=table)
    found = {row["key"]: row["value"] for row in _rows(conn, query, (keys,))}
    if found:
        conn.execute(TOUCH.format(table=table), (sorted(found),))
    missing = [key for key in keys if key not in found]
    if missing:
        values = compute([wanted[key] for key in missing])
        conn.execute(SAVE.format(table=table, column=column), (missing, [Jsonb(v) for v in values]))
        found |= dict(zip(missing, values, strict=True))
    return found


def _rows(conn, query, params):
    return conn.cursor(row_factory=dict_row).execute(query, params).fetchall()
