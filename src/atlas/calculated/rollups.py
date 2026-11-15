"""Census and pulse inputs of the incremental calculation (contract K6). Its geo rows come from
per-hour rollups of run_geo_buckets (CALC-PULSE-1a, calculated_geo_hours) instead of a scan of the
30-day window, and its batch runs' request buckets from per-hour copies of them (CALC-PULSE-1b,
calculated_pulse_buckets) instead of one scan of the window per product.

geo_rows() returns exactly scheduled.geo_rows()'s rows at any cutoff on the 5-minute grid. A geo
row's window changes where scheduled_inputs() cuts the 30 days (7 days and 1 day back), so the
whole hours between the cuts are read from the rollups, and an hour a cut falls inside (none when
the cutoff is on the hour) from run_geo_buckets. A rollup's histogram holds the requests of every
light-load candidate (pulses and runs outside a batch) per target; the pulses that ran during a
census (the overlaps) are subtracted when read, by run key, so no rollup depends on them. The
read is one statement that returns a row per geo key, its bins aggregated into arrays, through a
server-side cursor, and the rows stay tuples (scheduled.GeoRow) (CALC-MEM-1). The build reads
them per product (ProductGeo, CALC-MEM-2) when it measures the product, so it holds one product's
rows at a time.

fill() writes them before the calculation's transaction, a day at most per transaction, and
refresh() in it, before it reads, whatever went stale in between: an hour it reads is
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

from datetime import UTC, datetime, timedelta
from itertools import accumulate, pairwise

from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb

from .. import verdicts
from ..storage import connect
from . import scheduled
from .aggregate import census_overlaps
from .browser import NOT_BROWSER

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
LOCK = 817452902  # the calculation's advisory lock (calculator.calculate())
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
    "WHERE r.batch_id IS NOT NULL AND h.hour=ANY(%(hours)s) "
    # A verdict imported after the hour was counted (its run key is newer than the mark) that
    # was decided before the hour ended: the hour's runs may count otherwise now.
    "UNION SELECT m.hour FROM calculated_geo_rollups m WHERE m.hour=ANY(%(hours)s) "
    "AND EXISTS (SELECT FROM country_verdicts v WHERE v.run_key>m.through_run "
    "AND v.decided_at<m.hour+interval '1 hour') ORDER BY 1"
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
# Whether the geo rollups were counted with the methodology's country_verdicts: a
# calculated_distinct row (a memo no collection deletes).
RULE = "rollups-country-verdicts"
RULED = "SELECT counts FROM calculated_distinct WHERE digest=%s"
RULE_MARK = (
    "INSERT INTO calculated_distinct(digest,counts) VALUES (%s,%s) "
    "ON CONFLICT(digest) DO UPDATE SET counts=excluded.counts,last_used=now()"
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
COUNTERS = scheduled.COUNTERS
KEYS = ",".join(scheduled.GEO_KEY)  # OUT's columns
# One product's geo rows (ProductGeo.product()): the rollups' condition and run_geo_buckets'.
PRODUCT = ("AND h.product_id=%(product)s ", "AND s.product_id=%(product)s ")
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
# done pulse slots; counted: whether its network's country counted at its start (the census
# overlaps keep every run; the pulse slots and tested spans only the counted ones).
BATCH_RUNS = (
    f"{verdicts.WITH_DAYS}SELECT br.product_id,br.batch_id,br.run_key,b.kind,"
    "b.status AS batch_status,b.target_id,"
    "b.scheduled_for,b.country_code,br.status,br.requests,br.successes,br.started_at,"
    "br.finished_at,r.started_at AS run_started,r.finished_at AS run_finished,"
    "cv.day IS NOT NULL AS counted FROM batch_runs br JOIN test_batches b USING(batch_id) "
    "LEFT JOIN test_runs r ON r.run_key=br.run_key "
    f"{verdicts.counted('br.product_id', 'trim(b.country_code)', verdicts.RUN_AT)}"
    "WHERE b.kind<>'browser' AND ("
    "br.started_at IS NOT NULL AND br.started_at<%(end)s "
    "AND coalesce(br.finished_at,%(end)s)>=%(margin)s OR b.kind='pulse' "
    "AND b.scheduled_for>=%(start)s AND b.scheduled_for<%(end)s AND br.finished_at<%(end)s "
    "AND br.status='done') ORDER BY b.target_id,br.product_id,br.batch_id"
)
PULSE = ("product_id", "batch_id", "target_id", "scheduled_for", "status", "requests", "successes")
PULSE += ("run_key",)
# A pulse or load run's census_overlaps() keys, and the rows a batch-run read fetches at a time.
TESTED = ("product_id", "batch_id", "run_key", "started_at", "finished_at")
STREAM = 20000


def scheduled_inputs(conn, as_of, timed_target, geo=None, rule=False):
    """The census/pulse history of the 30-day window (scheduled.scheduled_inputs(), rule: the
    methodology's country_verdicts), its geo rows from the rollups (geo, geo_rows() by default:
    ProductGeo reads them per product when the build measures it) and its batch runs read once
    (batch_inputs())."""
    geo = geo or geo_rows
    return scheduled.scheduled_inputs(
        conn, as_of, timed_target, geo=geo, runs=batch_inputs, rule=rule
    )


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
    """scheduled.batch_inputs() from one read of the batch runs (BATCH_RUNS), streamed: the census
    overlaps, the done pulse runs and the tested spans, each in the order its query returns."""
    start, end, margin = bounds["start"], bounds["end"], bounds["margin"]
    census, candidates, pulses, monitored = [], [], [], []
    with conn.cursor("batch_runs", row_factory=dict_row) as cur:
        cur.itersize = STREAM
        for r in cur.execute(BATCH_RUNS, bounds):
            r["counted"] = r["counted"] or not bounds.get("verdicts", False)
            last = min(r["finished_at"] or end, end)
            if r["started_at"] is not None and r["started_at"] < end and last >= margin:
                _started(r, last, (start, end), (census, candidates, monitored))
            if _slot(r, start, end) and r["counted"]:
                pulses.append({k: r[k] for k in PULSE} | {"country": r["country_code"].strip()})
    # Only a product with a census run has overlaps (census_overlaps()).
    products = {c["product_id"] for c in census}
    tested = [dict(zip(TESTED, t, strict=True)) for t in candidates if t[0] in products]
    census.sort(key=lambda c: (c["product_id"], c["batch_id"]))
    tested.sort(key=lambda t: (t["product_id"], t["batch_id"]))
    monitored.sort(key=lambda s: (s["product_id"], s["country"], s["started_at"]))
    return census_overlaps(census, tested), pulses, monitored


def _started(r, last, window, into):
    """File a started batch run (last: its end, clipped to the window's end) as a census run, a
    pulse or load run that may overlap one (a TESTED tuple), and a tested span."""
    (start, end), (census, candidates, monitored) = window, into
    run = (r["run_started"] or r["started_at"], min(r["run_finished"] or last, end))
    if r["kind"] == "census":
        key = {"product_id": r["product_id"], "batch_id": r["batch_id"]}
        census.append(key | {"started_at": run[0], "finished_at": run[1]})
    elif r["kind"] in ("pulse", "load"):
        candidates.append((r["product_id"], r["batch_id"], r["run_key"], *run))
    if r["kind"] != "load" and last >= start and r["counted"]:
        span = {"started_at": r["started_at"], "last": last}
        monitored.append({"product_id": r["product_id"], "country": r["country_code"]} | span)


def _slot(r, start, end):
    """Whether the batch run is a done pulse slot (scheduled.pulse_runs())."""
    due = r["kind"] == "pulse" and r["batch_status"] not in ("skipped", "killed")
    finished = r["finished_at"] is not None and r["finished_at"] < end
    return due and start <= r["scheduled_for"] < end and finished and r["status"] == "done"


def geo_rows(conn, bounds, overlaps, timed_target):
    """scheduled.geo_rows() of the window in bounds, read from the rollups (module docstring):
    one row per geo key, its bins aggregated in SQL (_geo()), streamed in batches."""
    return ProductGeo(conn, bounds, overlaps, timed_target).rows()


class ProductGeo:
    """The window's geo rows (geo_rows()), read per product (CALC-MEM-2): its rollups are
    refreshed when it is made, then product() reads one product's rows when the build measures
    the product (inputs.GeoGroups), and rows() reads every product's."""

    def __init__(self, conn, bounds, overlaps, timed_target):
        hours, edges = spans((bounds["start"], bounds["d7"], bounds["d1"], bounds["end"]))
        # In the build's transaction, not yet committed.
        self.recounted = refresh(conn, hours, bounds.get("verdicts", False))
        flagged = sorted(o["run_key"] for o in overlaps if o["run_key"] is not None)
        params = {"verdicts": False} | bounds
        params |= {"hours": hours, "flagged": flagged, "latency_target": timed_target}
        raw = []
        for n, (lo, hi) in enumerate(edges):
            raw.append(f"g.bucket_start>=%(lo{n})s AND g.bucket_start<%(hi{n})s")
            params |= {f"lo{n}": lo, f"hi{n}": hi}
        self.conn, self.params = conn, params
        self.raw = f"WHERE ({' OR '.join(raw) or 'false'}) AND {NOT_BROWSER}"

    def rows(self):
        return self._read(_geo(self.raw), self.params)

    def product(self, product_id):
        """The geo rows of product product_id, in rows()' order."""
        return self._read(_geo(self.raw, PRODUCT), self.params | {"product": product_id})

    def _read(self, query, params):
        with self.conn.cursor("geo_rows", row_factory=tuple_row) as cur:
            cur.itersize = STREAM
            rows = list(cur.execute(query, params))
        return scheduled.labelled(self.conn, rows)


def marks(conn, as_of):
    """The geo rollup marks ([hour, through_run]) of the hours geo_rows() reads at as_of, which
    the incremental fingerprint hashes instead of the geo rows: refresh() recounts an hour and
    records the newest run key whenever a geo row it holds changes."""
    cuts = (as_of - 720 * HOUR, as_of - 168 * HOUR, as_of - 24 * HOUR, as_of)
    return (
        conn.cursor(row_factory=tuple_row)
        .execute(
            "SELECT hour,through_run FROM calculated_geo_rollups WHERE hour=ANY(%s) ORDER BY 1",
            (spans(cuts)[0],),
        )
        .fetchall()
    )


def _geo(raw, only=("", "")):
    """Each geo row in GEO_KEY order: its counters (_totals()) and its light-load histogram's
    ms and counts (_histograms()), aggregated; only: PRODUCT for one product's rows."""
    sums = ",".join(f"sum({c})::bigint" for c in COUNTERS)
    bins = "coalesce(array_agg({}::bigint ORDER BY ms) FILTER (WHERE ms IS NOT NULL),'{{}}')"
    return (
        f"SELECT {KEYS},{sums},{bins.format('ms')},{bins.format('n')} FROM ({_totals(raw, only)} "
        f"UNION ALL {_histograms(raw, only)}) u GROUP BY {KEYS} ORDER BY {KEYS}"
    )


def _totals(raw, only):
    """Each geo row's counters: its rollup hours' and its raw buckets' (raw: their condition)."""
    rollup, edge = (",".join(f"{t}.{c}" for c in COUNTERS) for t in "hg")
    return (
        f"SELECT {OUT},{','.join(COUNTERS)},NULL::bigint AS ms,NULL::bigint AS n FROM ("
        f"SELECT {ROLLUP_KEYS},{rollup} FROM calculated_geo_hours h WHERE h.hour=ANY(%(hours)s) "
        f"{only[0]}UNION ALL SELECT {RAW_KEYS},{edge} {scheduled.GEO_SOURCE}{raw}{only[1]}) t"
    )


def _histograms(raw, only):
    """Each geo row's light-load histogram bins: its rollup hours' of the latency target, less the
    overlaps' there, and its raw buckets' (scheduled.LIGHT)."""
    blank = ",".join("NULL::bigint" for _ in COUNTERS)
    return (
        f"SELECT {OUT},{blank},ms,sum(n)::bigint FROM (SELECT {ROLLUP_KEYS},b.ms,b.n "
        "FROM calculated_geo_hours h CROSS JOIN LATERAL unnest(h.light_ms,h.light_n) b(ms,n) "
        f"WHERE h.hour=ANY(%(hours)s) AND h.target=%(latency_target)s {only[0]}UNION ALL "
        f"SELECT {RAW_KEYS},x.ms,x.n {scheduled.GEO_SOURCE}{EXPAND}{raw}{scheduled.LIGHT}"
        f"{only[1]}UNION ALL {OVERLAPS} {only[1]}) v GROUP BY 1,2,3,4,5,6,11 HAVING sum(n)<>0"
    )


def refresh(conn, hours, rule=False):
    """Count the rollups of the hours (ascending) that are missing or stale (module docstring),
    and record the newest run key for every one of them; rule: the methodology's
    country_verdicts (only the runs whose country counted)."""
    _ruled(conn, rule)
    window = _window(conn, hours, "calculated_geo_hours", "calculated_geo_rollups")
    stale = [row["hour"] for row in _rows(conn, STALE, window)]
    for lo, hi in _ranges(stale):
        _count(conn, lo, hi, rule)
    conn.execute(MARK, (hours, window["newest"]))
    return stale


def fill(database, as_of, rule=False):
    """Count the missing or stale rollups of the 30 days before as_of ahead of the build, one
    range of at most a day per transaction, each with its marks and the calculation's lock: a
    failed build keeps them, and its own refresh() counts only what went stale since. rule: as
    refresh()'s."""
    hours, _ = spans((as_of - 720 * HOUR, as_of))
    if not hours:
        return []
    with connect(database) as conn:
        _ruled(conn, rule)
        window = _marks(conn, hours, "calculated_geo_rollups")
        stale = [row["hour"] for row in _rows(conn, STALE, window)]
    for lo, hi in _ranges(stale):
        with connect(database) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conn.execute(f"SELECT pg_advisory_xact_lock({LOCK})")
            span = [lo + n * HOUR for n in range((hi - lo) // HOUR)]
            newest = _marks(conn, span, "calculated_geo_rollups")["newest"]
            _count(conn, lo, hi, rule)
            conn.execute(MARK, (span, newest))
    return stale


def _count(conn, lo, hi, rule):
    """Count the rollups of the hours [lo, hi) again."""
    conn.execute("DELETE FROM calculated_geo_hours WHERE hour>=%s AND hour<%s", (lo, hi))
    conn.execute(WRITE, {"lo": lo, "hi": hi, "verdicts": rule})


def _ruled(conn, rule):
    """Drop every geo rollup counted under the other country_verdicts setting (RULED records
    the one they were counted with; none: without it, as before COUNTRY-MIN-IPS-1), so they are
    all counted again, once, with this one."""
    found = conn.cursor(row_factory=tuple_row).execute(RULED, (RULE,)).fetchone()
    if bool(found and found[0]["on"]) == rule:
        return
    conn.execute(f"SELECT pg_advisory_xact_lock({LOCK})")
    conn.execute("DELETE FROM calculated_geo_hours")
    conn.execute("DELETE FROM calculated_geo_rollups")
    conn.execute(RULE_MARK, (RULE, Jsonb({"on": rule})))


def _window(conn, hours, *tables):
    """Collect the old rollups of the tables (the marks table last); the STALE parameters of the
    hours (ascending) and the newest run key."""
    for table in tables:
        conn.execute(COLLECT.format(table), (hours[0],))
    return _marks(conn, hours, tables[-1])


def _marks(conn, hours, table):
    """The STALE parameters of the hours (ascending) in the marks table, and the newest run key."""
    marks = _rows(conn, NEWEST.format(table), (hours,))[0]
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
    """The runs of consecutive hours as [lo, hi) ranges of at most a day (one statement each)."""
    ranges = []
    for hour in hours:
        if ranges and ranges[-1][1] == hour and hour - ranges[-1][0] < DAY:
            ranges[-1][1] = hour + HOUR
        else:
            ranges.append([hour, hour + HOUR])
    return ranges


def _rows(conn, query, params):
    return conn.cursor(row_factory=dict_row).execute(query, params).fetchall()
