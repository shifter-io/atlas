"""Census and pulse inputs of the incremental calculation (contract K6). Its geo rows come from
per-hour rollups of run_geo_buckets (CALC-PULSE-1a, calculated_geo_hours) instead of a scan of the
30-day window, and its batch runs' request buckets from per-hour copies of them (CALC-PULSE-1b,
calculated_pulse_buckets) instead of one scan of the window per product.

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

scheduled_rows() reads the batch runs' buckets the same way (refresh_buckets(): a closed hour is
copied when it has no copy, holds a run newer than its copy, or holds a run outside a batch,
recorded in calculated_pulse_rollups.loose, that a batch owns now). A copy keeps every column but
the status codes, which nothing reads, and the rows it returns hold the same counters as the full
mode's (run_bucket_counters()) less those, the bucket label and the histogram format. The census
overlaps, done pulse runs and tested spans come from one read of the batch runs (batch_inputs()),
not three (SUM-MEM-1).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import accumulate, pairwise

from psycopg.rows import dict_row, tuple_row

from . import scheduled
from .aggregate import census_overlaps
from .browser import NOT_BROWSER

HOUR = timedelta(hours=1)
BIN = "date_bin('1 hour',{}.bucket_start,'2000-01-01 00:00:00+00')"
ON_HOUR = BIN.format("g")
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
# The newest run key, and the oldest through_run of the hours %s in the marks table {}.
NEWEST = (
    "SELECT coalesce(max(run_key),0) AS newest,coalesce((SELECT min(through_run) "
    "FROM {} WHERE hour=ANY(%s)),max(run_key),0) AS since FROM test_runs"
)
MARK = (
    "INSERT INTO calculated_geo_rollups SELECT unnest(%s::timestamptz[]),%s ON CONFLICT (hour) "
    "DO UPDATE SET through_run=excluded.through_run "
    "WHERE calculated_geo_rollups.through_run<>excluded.through_run"
)
# Rollups of hours over a day older than the first hour read, from the table {}.
COLLECT = "DELETE FROM {} WHERE hour<%s::timestamptz-interval '1 day'"
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

OUTCOMES = ("challenges", "blocks", "timeouts", "proxy_errors", "bytes_up", "bytes_down")
# A batch-run bucket's columns that the calculator reads (_bucket()), in calculated_pulse_buckets'
# order after its hour: run_buckets' less the status codes.
BUCKET = ",".join(
    f"b.{c}"
    for c in (
        *("bucket_start", "run_key", "completed", "successes", "response_body_bytes", "errors"),
        *("histogram_steps", "histogram_counts", "outcome_counters", *OUTCOMES, "extra"),
    )
)
BATCH_BUCKETS = "FROM run_buckets b JOIN test_runs r USING(run_key) WHERE r.batch_id IS NOT NULL "
RANGE = "b.bucket_start>=%(lo)s AND b.bucket_start<%(hi)s"
WRITE_BUCKETS = (
    f"INSERT INTO calculated_pulse_buckets SELECT {BIN.format('b')},{BUCKET} {BATCH_BUCKETS}"
    f"AND {RANGE}"
)
# Each copied hour of [lo, hi): the newest run key and the runs outside a batch it holds.
LOOSE = (
    "INSERT INTO calculated_pulse_rollups SELECT k.hour,%(newest)s,coalesce(l.loose,'{}') FROM "
    "generate_series(%(lo)s::timestamptz,%(hi)s::timestamptz-interval '1 hour',interval '1 hour')"
    f" k(hour) LEFT JOIN (SELECT {BIN.format('b')} AS hour,array_agg(DISTINCT b.run_key) AS loose "
    f"FROM run_buckets b JOIN test_runs r USING(run_key) WHERE r.batch_id IS NULL AND {RANGE} "
    "GROUP BY 1) l USING(hour) ON CONFLICT (hour) DO UPDATE SET through_run=excluded.through_run,"
    "loose=excluded.loose"
)
# The hours to copy among %(hours)s (module docstring), as STALE.
BUCKETS_STALE = (
    "SELECT k.hour FROM unnest(%(hours)s::timestamptz[]) k(hour) "
    "LEFT JOIN calculated_pulse_rollups m USING(hour) WHERE m.hour IS NULL "
    f"UNION SELECT n.hour FROM (SELECT {BIN.format('b')} AS hour,max(b.run_key) AS run_key "
    f"FROM run_buckets b WHERE b.run_key>%(since)s AND {RANGE} GROUP BY 1) n "
    "JOIN calculated_pulse_rollups m USING(hour) WHERE n.run_key>m.through_run "
    "AND m.hour=ANY(%(hours)s) UNION SELECT m.hour FROM calculated_pulse_rollups m "
    "JOIN test_runs r ON r.run_key=ANY(m.loose) WHERE r.batch_id IS NOT NULL "
    "AND m.hour=ANY(%(hours)s) ORDER BY 1"
)
BUCKETS_MARK = (
    "UPDATE calculated_pulse_rollups SET through_run=%s WHERE hour=ANY(%s) AND through_run<>%s"
)
# Every batch run that scheduled.batch_inputs()'s three queries read, in pulse_runs()' order:
# the started runs other than browser ones that ended at most a day before the window, and the
# done pulse slots.
BATCH_RUNS = (
    "SELECT br.product_id,br.batch_id,br.run_key,b.kind,b.status AS batch_status,b.target_id,"
    "b.scheduled_for,b.country_code,br.status,br.requests,br.successes,br.started_at,"
    "br.finished_at,r.started_at AS run_started,r.finished_at AS run_finished "
    "FROM batch_runs br JOIN test_batches b USING(batch_id) "
    "LEFT JOIN test_runs r ON r.run_key=br.run_key WHERE b.kind<>'browser' AND ("
    "br.started_at IS NOT NULL AND br.started_at<%(end)s "
    "AND coalesce(br.finished_at,%(end)s)>=%(margin)s OR b.kind='pulse' "
    "AND b.scheduled_for>=%(start)s AND b.scheduled_for<%(end)s AND br.finished_at<%(end)s "
    "AND br.status='done') ORDER BY b.target_id,br.product_id,br.batch_id"
)
PULSE = ("target_id", "scheduled_for", "status", "requests", "successes", "run_key")


def scheduled_inputs(conn, as_of, timed_target):
    """The census/pulse history of the 30-day window (scheduled.scheduled_inputs()), its geo rows
    from the rollups (geo_rows()) and its batch runs read once (batch_inputs())."""
    return scheduled.scheduled_inputs(conn, as_of, timed_target, geo=geo_rows, runs=batch_inputs)


def scheduled_rows(conn, start, as_of):
    """Every batch run's request buckets in [start, as_of), ordered by bucket_start and run_key:
    whole hours from their copies, the hours an edge falls inside from run_buckets."""
    hours, edges = spans((start, as_of))
    refresh_buckets(conn, hours)
    params, raw = {"hours": hours}, []
    for n, (lo, hi) in enumerate(edges):
        raw.append(f"b.bucket_start>=%(lo{n})s AND b.bucket_start<%(hi{n})s")
        params |= {f"lo{n}": lo, f"hi{n}": hi}
    query = (
        f"SELECT {BUCKET} FROM calculated_pulse_buckets b WHERE b.hour=ANY(%(hours)s) UNION ALL "
        f"SELECT {BUCKET} {BATCH_BUCKETS}AND ({' OR '.join(raw) or 'false'}) ORDER BY 1,2"
    )
    return [_bucket(row) for row in conn.cursor(row_factory=tuple_row).execute(query, params)]


def _bucket(row):
    """A bucket row as the calculator reads it (incremental.BUCKETS), from its BUCKET columns."""
    at, run_key, completed, successes, body, errors, steps, counts, outcome, *typed, extra = row
    bins = list(zip(accumulate(steps), counts, strict=True))
    counters = {"completed": completed, "successes": successes, "errors": errors}
    counters |= {"response_body_bytes": body, "latency_histogram": {"bins": bins}}
    if outcome:  # run_bucket_counters() emits the outcome counters of v6 buckets only
        counters |= dict(zip(OUTCOMES, typed, strict=True))
    if extra:  # unknown keys (an older bucket's outcome counters), not the bucket label
        counters |= {k: v for k, v in extra.items() if k != "bucket_start"}
    return {"bucket_start": at, "run_key": run_key, "counters": counters, "outcome": outcome}


def refresh_buckets(conn, hours):
    """Copy the batch-run buckets of the hours (ascending) that have no copy or a stale one
    (module docstring), and record the newest run key for every one of them."""
    window = _window(conn, hours, "calculated_pulse_buckets", "calculated_pulse_rollups")
    stale = [row["hour"] for row in _rows(conn, BUCKETS_STALE, window)]
    if stale:
        conn.execute("DELETE FROM calculated_pulse_buckets WHERE hour=ANY(%s)", (stale,))
    for lo, hi in _ranges(stale):
        for statement in (WRITE_BUCKETS, LOOSE):
            conn.execute(statement, {"lo": lo, "hi": hi, "newest": window["newest"]})
    conn.execute(BUCKETS_MARK, (window["newest"], hours, window["newest"]))
    return stale


def batch_inputs(conn, bounds):
    """scheduled.batch_inputs() from one read of the batch runs (BATCH_RUNS): the census
    overlaps, the done pulse runs and the tested spans, each in the order its query returns."""
    start, end, margin = bounds["start"], bounds["end"], bounds["margin"]
    census, tested, pulses, monitored = [], [], [], []
    for r in conn.cursor(row_factory=dict_row).execute(BATCH_RUNS, bounds):
        key = {"product_id": r["product_id"], "batch_id": r["batch_id"]}
        last = min(r["finished_at"] or end, end)
        started = r["started_at"] is not None and r["started_at"] < end and last >= margin
        run = r["run_started"] or r["started_at"]
        times = {"started_at": run, "finished_at": min(r["run_finished"] or last, end)}
        if started and r["kind"] == "census":
            census.append(key | times)
        elif started and r["kind"] in ("pulse", "load"):
            tested.append(key | {"run_key": r["run_key"]} | times)
        if started and r["kind"] != "load" and last >= start:
            monitored.append(
                {"product_id": r["product_id"], "country": r["country_code"]}
                | {"started_at": r["started_at"], "last": last}
            )
        if _slot(r, start, end):
            pulses.append(key | {k: r[k] for k in PULSE} | {"country": r["country_code"].strip()})
    census.sort(key=lambda c: (c["product_id"], c["batch_id"]))
    tested.sort(key=lambda t: (t["product_id"], t["batch_id"]))
    monitored.sort(key=lambda s: (s["product_id"], s["country"], s["started_at"]))
    return census_overlaps(census, tested), pulses, monitored


def _slot(r, start, end):
    """Whether the batch run is a done pulse slot (scheduled.pulse_runs())."""
    due = r["kind"] == "pulse" and r["batch_status"] not in ("skipped", "killed")
    finished = r["finished_at"] is not None and r["finished_at"] < end
    return due and start <= r["scheduled_for"] < end and finished and r["status"] == "done"


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
    window = _window(conn, hours, "calculated_geo_hours", "calculated_geo_rollups")
    stale = [row["hour"] for row in _rows(conn, STALE, window)]
    if stale:
        conn.execute("DELETE FROM calculated_geo_hours WHERE hour=ANY(%s)", (stale,))
    for lo, hi in _ranges(stale):
        conn.execute(WRITE, {"lo": lo, "hi": hi})
    conn.execute(MARK, (hours, window["newest"]))
    return stale


def _window(conn, hours, *tables):
    """Collect the old rollups of the tables (the marks table last); the STALE parameters of the
    hours (ascending) and the newest run key."""
    for table in tables:
        conn.execute(COLLECT.format(table), (hours[0],))
    marks = _rows(conn, NEWEST.format(tables[-1]), (hours,))[0]
    return {"hours": hours, "lo": hours[0], "hi": hours[-1] + HOUR} | marks


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
