"""What a calculation reads before build(), in its two modes (contract K6).

"full" (`calculate --full`; calculator.calculate()'s default) reads every run and every IP
observation of the 30-day window and aggregates them in Python (Full): the oracle
every test compares the incremental calculation with.

"incremental" (the `calculate` command's default) reads metadata first: the runs (every run
outside a batch, whose payload rows list them; batch runs started since a day before the window,
with a slot or a bucket in it, and each product's first, so every group exists in payload order;
census and pulse summaries are not read), the window's buckets and each daily window's list
signature. A window whose signature names a stored list (a hit) takes its list id, entry counts
and figures from calculated_lists and returns no observation row to Python; the other windows
(misses, and every
census-pulse window until CALC-PULSE-1) read their runs' observations, aggregated in Python as
full mode does (CALC-INC-1b counts them in SQL). What moves with the cutoff comes from the frozen
extension modules (hourly.py, fleetsql.py, rollups.py), whose bodies count in the database. The
fingerprint hashes run metadata and signatures instead of observations: a stored run's
observations never change.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from functools import partial
from types import SimpleNamespace

from psycopg.rows import dict_row

from ..estimates import window as estimates
from ..storage import RAW_DAYS
from . import calculator, fleetsql, hourly, lists, parts, rollups
from .aggregate import breakdowns, daily_pools, pool, pool_lists
from .inputs import light_load
from .policy import digest, effective, latency_target, payload_limits
from .releases import RUN_OBSERVATIONS, enriched_observations, used_releases
from .store import jsonable, write

MODES = ("full", "incremental")
RUNS = (
    "SELECT r.run_key,r.run_id::text,r.summary_sha256,{summary} AS summary,r.started_at,"
    "r.finished_at,s.scope,s.scope_id,p.product_id,p.provider,p.name,r.batch_id,"
    "b.kind AS test,b.scheduled_for AS slot_at,trim(b.country_code) AS batch_country,"
    "rr.slug AS batch_region,coalesce(cr.role,CASE b.kind {roles} END,'exploratory') AS role "
    "FROM test_runs r JOIN scope_details s USING(scope_id) JOIN products p "
    "ON p.product_id=s.product_id LEFT JOIN test_batches b ON b.batch_id=r.batch_id "
    "LEFT JOIN runner_regions rr ON rr.region_id=b.region_id "
    "LEFT JOIN calculation_roles cr USING(run_key) {where}ORDER BY r.run_key"
)
# The incremental calculation's runs (see above).
WINDOW_RUNS = (
    "WHERE r.batch_id IS NULL OR r.started_at>=%(margin)s "
    "OR b.scheduled_for>=%(start)s AND b.scheduled_for<%(end)s "
    "OR r.run_key IN (SELECT run_key FROM run_buckets WHERE bucket_start>=%(start)s "
    "AND bucket_start<%(end)s) OR r.run_key IN (SELECT min(f.run_key) FROM test_runs f "
    "JOIN scope_stats fs USING(scope_id) WHERE f.batch_id IS NOT NULL GROUP BY fs.product_id) "
)
BUCKETS = (
    "SELECT b.bucket_start,b.run_key,run_bucket_counters(b) AS counters,"
    "b.outcome_counters AS outcome FROM run_buckets b{join} "
    "WHERE b.bucket_start>=%s AND b.bucket_start<%s{daily} ORDER BY b.bucket_start,b.run_key"
)
# The runs among %s with an observation in the window.
OBSERVED = (
    "SELECT k AS run_key FROM unnest(%s::bigint[]) k WHERE EXISTS (SELECT FROM ip_observations o "
    "WHERE o.run_key=k AND o.bucket_start>=%s AND o.bucket_start<%s)"
)
STORED = (
    "SELECT l.product_id,l.cohort,l.window_hours,l.list_id,l.entries,l.figures "
    "FROM calculated_lists l JOIN unnest(%s::int[],%s::text[],%s::int[],%s::text[]) "
    "AS k(product_id,cohort,window_hours,signature) USING(product_id,cohort,window_hours,signature)"
)
# pool()'s counts after new_ips and churn_rate, in its order: list figures (lists.FIGURES).
POOL = "recurring_share countries regions cities asns datacenter_asn_share classification_coverage"
POOL = (*POOL.split(), "geo_accuracy", "enrichment_coverage", "observations")
SHARES_BASIS = "unique exit IPs with the DB-IP field (unknowns excluded)"
EVIDENCE = "SELECT * FROM metric_evidence ORDER BY evidence_id"


class Inputs(SimpleNamespace):
    """A calculation's inputs (runs, buckets, release, evidence, scheduled), its build source,
    its fingerprint, and how its payload is stored (store())."""

    def store(self, conn, build_id, payload, config):
        """Attach the estimates and write the build (store.write()); returns the payload as
        written, its windows in the v2 form."""
        if self.signed is None:  # full: signatures and verify-on-hit after build()
            payload = estimates.attach(payload, self.runs, conn, self.as_of, config)
            persist = lists.reusing(conn, payload, self.runs, self.buckets)
            return write(conn, build_id, payload, config, persist)
        groups = {(p["product_id"], p["cohort"]): p for p in payload["products"]}
        run_group = {r["run_key"]: r["group"] for r in self.runs}
        found = estimates.windows_with(groups, run_group, estimates.fetch(conn, self.as_of))
        run_sets = [{(r["run_key"], r["country_code"]) for r in rows} for _, rows in found]
        estimates.place(found, hourly.window_distinct(conn, run_sets, True), config)
        payload = write(conn, build_id, payload, config, partial(lists.persist, signed=self.signed))
        return payload | {"products": parts.fill(conn, payload["products"])}


class Full:
    """The full calculation's source of build(): every observation of the window, aggregated in
    Python. It is the oracle the incremental calculation (Incremental, the other source) is
    compared with. build() asks its source for every value it derives from IPs."""

    def __init__(self, observations):
        self.observations, self.groups = observations, defaultdict(list)

    def group(self, run_map):
        for o in self.observations:
            r = run_map[o["run_key"]]
            self.groups[r["group"]].append(
                o | {"requested_country": r["scope"]["config"].get("requested_country")}
            )

    def days(self, key, as_of, days):
        return daily_pools(self.groups[key], as_of, days)

    def seen(self, key, since, as_of):
        return [o for o in self.groups[key] if since <= o["bucket_start"] < as_of]

    def window(self, key, seen, since, hours, hosting, geo):
        """The window's pool counts, its IPs (pool() entries), pool lists and breakdowns."""
        ps, ips = pool(seen, since, hours, hosting)
        dimensions = breakdowns(ips, geo)
        return ps, ips, pool_lists(dimensions), {"breakdowns": dimensions}

    def ips(self, key, hours, points):
        return points  # series() counted each point's IPs from the observations

    def cohorts(self, fleets, overlaps, fleet_groups, as_of):
        return fleets, overlaps  # build() counted them from the IP sets


def read(conn, config, method_id, as_of, mode="incremental"):
    """The inputs of a calculation at as_of (on the bucket grid) in mode ("full" or
    "incremental"), read in the calculation's snapshot."""
    if mode not in MODES:
        raise ValueError(f"calculation mode must be one of {MODES}, not {mode!r}")
    start = as_of - timedelta(days=RAW_DAYS)  # older raw rows are archived
    roles = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in calculator.BATCH_ROLES.items())
    evidence = conn.execute(EVIDENCE).fetchall()
    scheduled = rollups.scheduled_inputs(conn, as_of, latency_target(config))
    common = {"evidence": evidence, "scheduled": scheduled}
    if mode == "full":
        runs = conn.execute(RUNS.format(summary="r.summary", roles=roles, where="")).fetchall()
        buckets = conn.execute(BUCKETS.format(join="", daily=""), (start, as_of)).fetchall()
        release, observations = enriched_observations(conn, start, as_of)
        # Hash relevant inputs, including role corrections, enrichment and late evidence.
        hashed = {"runs": runs, "observations": observations}
        source, signed = Full(observations), None
    else:
        bounds = {"start": start, "end": as_of, "margin": start - timedelta(days=1)}
        summary = "CASE WHEN r.batch_id IS NULL THEN r.summary END"
        query = RUNS.format(summary=summary, roles=roles, where=WINDOW_RUNS)
        runs = conn.execute(query, bounds).fetchall()
        buckets = _buckets(conn, runs, start, as_of)
        active = sorted({b["run_key"] for b in buckets})
        release = used_releases(
            conn, [r["run_key"] for r in conn.execute(OBSERVED, (active, start, as_of))]
        )
        group_runs(runs, config, scheduled["overlaps"])
        head = {"implementation_id": calculator.IMPLEMENTATION_ID, "periods": periods(as_of)}
        signed = lists.signatures(conn, head | {"methodology": {"id": method_id}}, runs, buckets)
        hashed = {"runs": [{k: v for k, v in r.items() if k != "summary"} for r in runs]}
        hashed["signatures"] = sorted([*key, value] for key, value in signed.items())
        source = Incremental(conn, config, as_of, runs, buckets, signed)
    hashed |= {"release": release, "buckets": buckets} | common
    fingerprint = digest(jsonable(hashed | {"implementation_id": calculator.IMPLEMENTATION_ID}))
    fields = {"runs": runs, "buckets": buckets, "release": release, "signed": signed}
    return Inputs(source=source, fingerprint=fingerprint, as_of=as_of, **common, **fields)


def periods(as_of):
    """The payload's periods (build()), which the list signatures read."""
    return {
        label: {"start": as_of - timedelta(hours=h), "end": as_of, "hours": h}
        for label, h in (("24h", 24), ("7d", 168), ("30d", 720))
    }


def _buckets(conn, runs, start, as_of):
    """The window's buckets: the daily runs', then each product's batch runs'
    (rollups.scheduled_rows()), together in the full calculation's order."""
    query = BUCKETS.format(join=" JOIN test_runs r USING(run_key)", daily=" AND r.batch_id IS NULL")
    found = conn.execute(query, (start, as_of)).fetchall()
    for product in sorted({r["product_id"] for r in runs if r["batch_id"] is not None}):
        found += rollups.scheduled_rows(conn, product, start, as_of)
    return sorted(found, key=lambda b: (b["bucket_start"], b["run_key"]))


def group_runs(runs, config, overlaps):
    """Give each run its product group and light flag, as build() does."""
    flagged = {o["run_key"]: o["census"] for o in overlaps if o["run_key"] is not None}
    timed_target = latency_target(config)
    for r in runs:
        kind = effective(config, r["provider"], r["name"]).get("kind", r["name"])
        cohort, _ = calculator.cohort_for(r["scope"], kind, r["batch_id"] is not None)
        r["group"], r["light"] = (r["product_id"], cohort), light_load(r, flagged, timed_target)


class Incremental(Full):
    """build()'s source in the incremental mode: stored lists for the hit windows, the
    observations of the missed windows' runs, and the extension modules for the rest."""

    def __init__(self, conn, config, as_of, runs, buckets, signed):
        active = {b["run_key"] for b in buckets}
        self.conn, self.as_of, self.limits = conn, as_of, payload_limits(config)
        self.runs = defaultdict(dict)  # group -> run_key -> requested country, if in the window
        for r in runs:
            if r["run_key"] in active:
                self.runs[r["group"]][r["run_key"]] = r["scope"]["config"].get("requested_country")
        groups = {r["group"]: r["product_id"] for r in runs}
        windows = config["windows"]
        self.hits = self._hits(signed, windows)
        missed = {(g, h) for g in groups for h in windows if (g, h) not in self.hits}
        # Runs of the missed windows (the ones that changed): lists they make stale miss too.
        stale = hourly.stale_lists(conn, sorted({k for g, _ in missed for k in self.runs[g]}))
        missed |= {k for k, hit in self.hits.items() if hit["list_id"] in stale}
        self.hits = {k: hit for k, hit in self.hits.items() if k not in missed}
        super().__init__(self._observations(missed))
        self._count(groups, windows, max(windows) // 24)

    def _hits(self, signed, windows):
        """The stored lists of the windows' signatures: {(group, hours): list row}."""
        keys = [(*key, signature) for key, signature in signed.items() if key[2] in windows]
        if not keys:
            return {}
        params = [list(column) for column in zip(*keys, strict=True)]
        rows = self.conn.cursor(row_factory=dict_row).execute(STORED, params)
        return {((r["product_id"], r["cohort"]), r["window_hours"]): r for r in rows}

    def _observations(self, missed):
        """The observations of the missed windows' runs, each group's from its widest missed
        window's start (at most one query per window length)."""
        widest = {}
        for group, hours in missed:
            widest[group] = max(widest.get(group, 0), hours)
        observations = []
        for hours in sorted(set(widest.values())):
            keys = sorted({k for g, h in widest.items() if h == hours for k in self.runs[g]})
            if keys:
                since = self.as_of - timedelta(hours=hours)
                observations += self.conn.execute(RUN_OBSERVATIONS, (keys, since, self.as_of))
        return sorted(observations, key=lambda o: (o["run_key"], o["bucket_start"], o["ip_id"]))

    def _count(self, groups, windows, days):
        """Count in the database the daily pools and series IPs of every group window, and the
        first-seen IPs of the widest hit window of each group (slice_figures(), bucket_ips())."""
        products = {g: (product, self.runs[g]) for g, product in groups.items() if self.runs[g]}
        day, slices, self.ranges = timedelta(hours=24), {}, {}
        for g in products:
            slices[g] = [(self.as_of - (d + 1) * day, self.as_of - d * day) for d in range(days)]
            widest = max((h for h in windows if (g, h) in self.hits), default=None)
            if widest:  # new IPs of every hit window of the group (hourly.new_ips())
                self.ranges[g] = len(slices[g])
                slices[g].append((self.as_of - timedelta(hours=widest), self.as_of, "first_seen"))
        self.figures = hourly.slice_figures(self.conn, products, slices)
        buckets = {}
        for g in products:
            for h in windows:
                step = timedelta(minutes=5) if h == 24 else timedelta(days=1)
                buckets[g, h] = (self.as_of - timedelta(hours=h), self.as_of, step)
        keys = {(g, h): list(self.runs[g]) for g, h in buckets}
        self.points = hourly.bucket_ips(self.conn, keys, buckets)

    def days(self, key, as_of, days):
        return (self.figures.get(key) or [None] * days)[:days]

    def seen(self, key, since, as_of):
        return ()  # series() and the fleet count IPs in the database (ips(), cohorts())

    def window(self, key, seen, since, hours, hosting, geo):
        hit = self.hits.get((key, hours))
        if hit is None:
            return super().window(
                key, super().seen(key, since, self.as_of), since, hours, hosting, geo
            )
        figures, n = hit["figures"], self.ranges.get(key)
        found = None if n is None else self.figures[key][n]  # None: no run in the window
        new, size = hourly.new_ips(found, since), figures["unique_ips"]
        ps = {"unique_ips": size, "new_ips": new, "churn_rate": new / size if size else None}
        ps |= {k: figures[k] for k in POOL} | {"shares_basis": SHARES_BASIS}
        entries, pooled = hit["entries"], figures["list_entries"]
        kept = {n: min(c, self.limits[n]) for n, c in (entries | pooled).items()}
        reference = {"lists": {"id": hit["list_id"], "entries": kept}, "breakdown_entries": entries}
        return ps, {}, {"list_entries": pooled}, reference  # no IPs: cohorts() counts them

    def ips(self, key, hours, points):
        return fleetsql.fill(points, self.points.get((key, hours), {}))

    def cohorts(self, fleets, overlaps, fleet_groups, as_of):
        """build()'s fleet entries and overlaps with every IP count across groups from the
        database (fleetsql.py): its IP sets hold only the missed windows' IPs."""
        members = {
            key: (entry, [(p, list(self.runs[p["product_id"], p["cohort"]])) for p, *_ in ms])
            for (key, ms), entry in zip(
                fleet_groups.items(), fleets[: len(fleet_groups)], strict=True
            )
        }
        windows = {}
        for (_, label), ms in fleet_groups.items():
            windows[label] = {"hours": ms[0][0]["windows"][label]["period"]["hours"]}
        for label, entry in zip(windows, fleets[len(fleet_groups) :], strict=True):
            windows[label]["fleet"] = entry
        out = fleetsql.fleet_entries(self.conn, as_of, members, windows)
        by_id = {p["id"]: p for ms in fleet_groups.values() for p, *_ in ms}
        for (group_id, label), shares in out["shares"].items():
            by_id[group_id]["windows"][label]["overlap"] = shares
        return out["fleet"], out["overlap"]
