"""Census and pulse inputs of the incremental calculation (contract K6). Its geo rows come from
per-hour rollups of run_geo_buckets (CALC-PULSE-1a, calculated_geo_hours) instead of a scan of the
30-day window; scheduled_rows() still runs the full mode's query (CALC-PULSE-1b replaces it).

geo_rows() returns exactly scheduled.geo_rows()'s rows at any cutoff on the 5-minute grid. A geo
row's window changes where scheduled_inputs() cuts the 30 days (7 days and 1 day back), so the
whole hours between the cuts are read from the rollups, and an hour a cut falls inside (none when
the cutoff is on the hour) from run_geo_buckets. A rollup's histogram holds the requests of every
light-load candidate (pulses and runs outside a batch) per target; the pulses that ran during a
census (the overlaps) are subtracted when read, by run key, so no rollup depends on them.

refresh() writes them in the calculation's transaction, before it reads: an hour it reads is
counted when it has no rollup, or holds a run newer than its rollup (a run key above its
through_run: ingestion is serialized, so run keys follow commits), or holds a run outside a batch
that a batch owns now (the run was ingested again into its batch). Every hour read then records
the newest run key. Rollups of hours over a day older than the window are deleted.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from psycopg.rows import dict_row

from . import scheduled
from .browser import NOT_BROWSER

HOUR = timedelta(hours=1)
# A product's batch-run buckets in [start, as_of), in the columns and order the calculator reads.
SCHEDULED_ROWS = (
    "SELECT b.bucket_start,b.run_key,run_bucket_counters(b) AS counters,"
    "b.outcome_counters AS outcome FROM run_buckets b JOIN test_runs r USING(run_key) "
    "JOIN scope_stats s ON s.scope_id=r.scope_id WHERE r.batch_id IS NOT NULL "
    "AND s.product_id=%s AND b.bucket_start>=%s AND b.bucket_start<%s "
    "ORDER BY b.bucket_start,b.run_key"
)
ON_HOUR = "date_bin('1 hour',g.bucket_start,'2000-01-01 00:00:00+00')"
SCOPE = "CASE WHEN r.batch_id IS NULL THEN r.scope_id END AS scope_id"
# A rollup row's key: hour, product group, location, network, fallback, target, and the run when
# it is outside a batch.
HOUR_KEYS = (
    f"{ON_HOUR} AS hour,{SCOPE},s.product_id,g.location_id,g.network_id,"
    f"{scheduled.FALLBACK} AS fallback,s.target,CASE WHEN r.batch_id IS NULL THEN g.run_key END"
)
CANDIDATE = "(b.kind IS NULL OR b.kind='pulse')"  # light load before target and overlaps
EXPAND = "CROSS JOIN LATERAL histogram(g.histogram_steps,g.histogram_counts::bigint[]) x "
SPAN = f"WHERE g.bucket_start>=%(lo)s AND g.bucket_start<%(hi)s AND {NOT_BROWSER}"
WRITE = (
    "INSERT INTO calculated_geo_hours SELECT hour,scope_id,product_id,location_id,network_id,"
    "fallback,target,run_key,sum(completed),sum(successes),sum(challenges),sum(blocks),"
    "coalesce(array_agg(ms ORDER BY ms) FILTER (WHERE ms IS NOT NULL),'{}'),"
    "coalesce(array_agg(n ORDER BY ms) FILTER (WHERE ms IS NOT NULL),'{}') FROM ("
    f"SELECT {HOUR_KEYS} AS run_key,sum(g.completed) AS completed,sum(g.successes) AS successes,"
    "sum(g.challenges) AS challenges,sum(g.blocks) AS blocks,NULL::bigint AS ms,NULL::bigint AS n "
    f"{scheduled.GEO_SOURCE}{SPAN}GROUP BY 1,2,3,4,5,6,7,8 UNION ALL SELECT {HOUR_KEYS},"
    f"NULL,NULL,NULL,NULL,x.ms,sum(x.n)::bigint {scheduled.GEO_SOURCE}{EXPAND}{SPAN}"
    f"AND {CANDIDATE} GROUP BY 1,2,3,4,5,6,7,8,x.ms) u GROUP BY 1,2,3,4,5,6,7,8"
)
# The hours to count among %(hours)s (module docstring). since: the oldest of their through_run.
STALE = (
    "SELECT k.hour FROM unnest(%(hours)s::timestamptz[]) k(hour) "
    "LEFT JOIN calculated_geo_rollups m USING(hour) WHERE m.hour IS NULL "
    f"UNION SELECT n.hour FROM (SELECT {ON_HOUR} AS hour,max(g.run_key) AS run_key "
    "FROM run_geo_buckets g WHERE g.run_key>%(since)s AND g.bucket_start>=%(lo)s "
    "AND g.bucket_start<%(hi)s GROUP BY 1) n JOIN calculated_geo_rollups m USING(hour) "
    "WHERE n.run_key>m.through_run AND m.hour=ANY(%(hours)s) "
    "UNION SELECT h.hour FROM calculated_geo_hours h JOIN test_runs r USING(run_key) "
    "WHERE r.batch_id IS NOT NULL AND h.hour=ANY(%(hours)s) ORDER BY 1"
)
NEWEST = (
    "SELECT coalesce(max(run_key),0) AS newest,coalesce((SELECT min(through_run) "
    "FROM calculated_geo_rollups WHERE hour=ANY(%s)),max(run_key),0) AS since FROM test_runs"
)
MARK = (
    "INSERT INTO calculated_geo_rollups SELECT unnest(%s::timestamptz[]),%s ON CONFLICT (hour) "
    "DO UPDATE SET through_run=excluded.through_run "
    "WHERE calculated_geo_rollups.through_run<>excluded.through_run"
)
# Rollups of hours over a day older than the first hour read.
COLLECT = (
    "DELETE FROM calculated_geo_hours WHERE hour<%s::timestamptz-interval '1 day'",
    "DELETE FROM calculated_geo_rollups WHERE hour<%s::timestamptz-interval '1 day'",
)
# A geo row's key (scheduled.GEO_KEY) from its time at: rollup hours and raw buckets alike.
OUT = (
    "scope_id,product_id,CASE WHEN at>=%(d1)s THEN 24 WHEN at>=%(d7)s THEN 168 ELSE 720 END "
    "AS hours,location_id,network_id,fallback"
)
ROLLUP_KEYS = "h.hour AS at,h.scope_id,h.product_id,h.location_id,h.network_id,h.fallback"
RAW_KEYS = (
    f"g.bucket_start AS at,{SCOPE},s.product_id,g.location_id,g.network_id,"
    f"{scheduled.FALLBACK} AS fallback"
)
COUNTERS = ("completed", "successes", "challenges", "blocks")
# The overlaps' light-load histograms in the hours read from rollups, subtracted from them.
OVERLAPS = (
    f"SELECT {RAW_KEYS},x.ms,-x.n {scheduled.GEO_SOURCE}{EXPAND}WHERE g.run_key=ANY(%(flagged)s) "
    f"AND {ON_HOUR}=ANY(%(hours)s) AND g.bucket_start>=%(start)s AND g.bucket_start<%(end)s "
    f"AND {NOT_BROWSER}AND {CANDIDATE} AND s.target=%(latency_target)s"
)


def scheduled_inputs(conn, as_of, timed_target):
    """The census/pulse history of the 30-day window (scheduled.scheduled_inputs()), its geo rows
    from the rollups (geo_rows())."""
    return scheduled.scheduled_inputs(conn, as_of, timed_target, geo=geo_rows)


def scheduled_rows(conn, product_id, start, as_of):
    """The request buckets of the product's batch runs in [start, as_of)."""
    return _rows(conn, SCHEDULED_ROWS, (product_id, start, as_of))


def geo_rows(conn, bounds, overlaps, timed_target):
    """scheduled.geo_rows() of the window in bounds, read from the rollups (module docstring)."""
    hours, edges = spans((bounds["start"], bounds["d7"], bounds["d1"], bounds["end"]))
    refresh(conn, hours)
    flagged = sorted(o["run_key"] for o in overlaps if o["run_key"] is not None)
    params = bounds | {"hours": hours, "flagged": flagged, "latency_target": timed_target}
    raw = []
    for n, (lo, hi) in enumerate(edges):
        raw.append(f"g.bucket_start>=%(lo{n})s AND g.bucket_start<%(hi{n})s")
        params |= {f"lo{n}": lo, f"hi{n}": hi}
    raw = f"WHERE ({' OR '.join(raw) or 'false'}) AND {NOT_BROWSER}"
    totals = _rows(conn, _totals(raw), params)
    histograms = defaultdict(list)
    for row in _rows(conn, _histograms(raw), params):
        histograms[tuple(row[k] for k in scheduled.GEO_KEY)].append([row["ms"], row["n"]])
    return scheduled.labelled(conn, totals, histograms)


def _totals(raw):
    """Each geo row's counters: its rollup hours' and its raw buckets' (raw: their condition)."""
    sums = ",".join(f"sum({c})::bigint AS {c}" for c in COUNTERS)
    rollup, edge = (",".join(f"{t}.{c}" for c in COUNTERS) for t in "hg")
    return (
        f"SELECT {OUT},{sums} FROM (SELECT {ROLLUP_KEYS},{rollup} FROM calculated_geo_hours h "
        f"WHERE h.hour=ANY(%(hours)s) UNION ALL SELECT {RAW_KEYS},{edge} {scheduled.GEO_SOURCE}"
        f"{raw}) u GROUP BY 1,2,3,4,5,6 ORDER BY 1,2,3,4,5,6"
    )


def _histograms(raw):
    """Each geo row's light-load histogram bins: its rollup hours' of the latency target, less the
    overlaps' there, and its raw buckets' (scheduled.LIGHT)."""
    return (
        f"SELECT {OUT},ms,sum(n)::bigint AS n FROM (SELECT {ROLLUP_KEYS},b.ms,b.n "
        "FROM calculated_geo_hours h CROSS JOIN LATERAL unnest(h.light_ms,h.light_n) b(ms,n) "
        "WHERE h.hour=ANY(%(hours)s) AND h.target=%(latency_target)s UNION ALL "
        f"SELECT {RAW_KEYS},x.ms,x.n {scheduled.GEO_SOURCE}{EXPAND}{raw}{scheduled.LIGHT}"
        f"UNION ALL {OVERLAPS}) u GROUP BY 1,2,3,4,5,6,7 HAVING sum(n)<>0 "
        "ORDER BY 1,2,3,4,5,6,7"
    )


def refresh(conn, hours):
    """Count the rollups of the hours (ascending) that are missing or stale (module docstring),
    and record the newest run key for every one of them."""
    for statement in COLLECT:
        conn.execute(statement, (hours[0],))
    marks = _rows(conn, NEWEST, (hours,))[0]
    window = {"hours": hours, "since": marks["since"], "lo": hours[0], "hi": hours[-1] + HOUR}
    stale = [row["hour"] for row in _rows(conn, STALE, window)]
    if stale:
        conn.execute("DELETE FROM calculated_geo_hours WHERE hour=ANY(%s)", (stale,))
    for lo, hi in _ranges(stale):
        conn.execute(WRITE, {"lo": lo, "hi": hi})
    conn.execute(MARK, (hours, marks["newest"]))
    return stale


def spans(cuts):
    """The whole hours between consecutive cuts (ascending), and the ranges [(lo, hi)] of the
    hours a cut falls inside, clipped to the cuts."""
    hours, edges = [], []
    for lo, hi in pairwise(cuts):
        first, last = _floor(lo), _floor(hi)
        if first < lo:
            edges.append((lo, first + HOUR))
            first += HOUR
        if last < hi:
            edges.append((last, hi))
        hours += [first + n * HOUR for n in range((last - first) // HOUR)]
    return hours, edges


def _floor(at):
    return datetime.fromtimestamp(int(at.timestamp() // 3600) * 3600, UTC)


def _ranges(hours):
    """The runs of consecutive hours as [lo, hi) ranges."""
    ranges = []
    for hour in hours:
        if ranges and ranges[-1][1] == hour:
            ranges[-1][1] = hour + HOUR
        else:
            ranges.append([hour, hour + HOUR])
    return ranges


def _rows(conn, query, params):
    return conn.cursor(row_factory=dict_row).execute(query, params).fetchall()
