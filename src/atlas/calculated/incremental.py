"""What a calculation reads before build(), in its two modes (contract K6).

"full" (`calculate --full`, and calculator.calculate()'s default) reads every run and every IP
observation of the 30-day window and aggregates them in Python (Full): the oracle
every test compares the incremental calculation with.

"incremental" (`calculate`'s default) reads metadata first: the runs (every run
outside a batch, whose payload rows list them; batch runs started since a day before the window,
with a slot or a bucket in it, and each product's first, so every group exists in payload order;
census and pulse summaries are not read), the window's buckets and each daily window's list
signature. A window whose signature names a stored list (a hit) takes its list id, entry counts
and figures from calculated_lists. The other windows (misses, and every census-pulse window until
CALC-PULSE-1) are counted in the database (sqlwindow.window_inputs(), each run labelled with its
own release as full mode reads them): breakdowns() over the label combinations, weighted by their
IPs, and the window's geo rows give its list. A group's missed lists are written together once its
last missed window is counted (list_write.write_lists(): one INSERT, one label lookup and one
COPY), so a group keeps only its own lists' rows in memory, and every window names its list. No
observation row comes back to Python. What moves with the cutoff comes from the
frozen extension modules (hourly.py, fleetsql.py, rollups.py: the census-pulse geo rows from
per-hour rollups, read per product as build() measures it), whose bodies count in the database.
The fingerprint hashes run metadata and signatures instead of observations: a stored run's
observations never change. It hashes the geo rollup marks of the window (rollups.marks()) instead
of the geo rows: an hour's rows change only with a run newer than its mark or a run linked to a
batch (hashed with the runs), and the edge hours' rows with the runs.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from functools import partial
from types import SimpleNamespace

from psycopg.rows import dict_row

from ..estimates import window as estimates
from ..storage import RAW_DAYS
from . import calculator, fleetsql, hourly, ipstate, list_write, lists, parts, rollups
from .aggregate import breakdowns, daily_pools, iso, pool, pool_lists
from .fleet import KIND
from .inputs import light_load, under_load_run
from .policy import digest, effective, latency_target, payload_limits
from .poolfig import pool_figures
from .releases import enriched_observations, used_releases
from .scheduled import scheduled_inputs
from .slim import POOL_LISTS
from .sqlwindow import WindowSpec, window_inputs
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
# pool()'s counts after new_ips and churn_rate, in its order: list figures (list_write.FIGURES).
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
        if self.source.unwritten:
            raise ValueError("A missed window's list was not written")
        groups = {(p["product_id"], p["cohort"]): p for p in payload["products"]}
        run_group = {r["run_key"]: r["group"] for r in self.runs}
        found = estimates.windows_with(groups, run_group, estimates.fetch(conn, self.as_of))
        estimates.place(found, estimates.distinct(conn, found), config)
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

    def window(self, key, seen, since, hours, hosting, geo, build_id=None):
        """The window's pool counts, its IPs (pool() entries), pool lists and breakdowns."""
        ps, ips = pool(seen, since, hours, hosting)
        dimensions = breakdowns(ips, geo)
        return ps, ips, pool_lists(dimensions), {"breakdowns": dimensions}

    def ips(self, key, hours, points):
        return points  # series() counted each point's IPs from the observations

    def cohorts(self, fleets, overlaps, fleet_groups, as_of):
        return fleets, overlaps  # build() counted them from the IP sets


def read(conn, config, method_id, as_of, mode="incremental", prefetch=None):
    """The inputs of a calculation at as_of (on the bucket grid) in mode ("full" or
    "incremental"), read in the calculation's snapshot; prefetch (parallel_reads.Prefetcher):
    the incremental mode reads its missed groups' window inputs ahead over its helpers."""
    if mode not in MODES:
        raise ValueError(f"calculation mode must be one of {MODES}, not {mode!r}")
    start = as_of - timedelta(days=RAW_DAYS)  # older raw rows are archived
    roles = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in calculator.BATCH_ROLES.items())
    evidence = conn.execute(EVIDENCE).fetchall()
    # The full mode reads the 30-day geo rows (the oracle), the incremental one their rollups,
    # per product when build() measures it (rollups.ProductGeo).
    reader = scheduled_inputs
    if mode != "full":
        reader = partial(rollups.scheduled_inputs, geo=rollups.ProductGeo)
    scheduled = reader(conn, as_of, latency_target(config))
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
        buckets = _buckets(conn, start, as_of)
        active = sorted({b["run_key"] for b in buckets})
        release = used_releases(
            conn, [r["run_key"] for r in conn.execute(OBSERVED, (active, start, as_of))]
        )
        group_runs(runs, config, scheduled["overlaps"])
        head = {"implementation_id": calculator.IMPLEMENTATION_ID, "periods": periods(as_of)}
        signed = lists.signatures(conn, head | {"methodology": {"id": method_id}}, runs, buckets)
        hashed = {"runs": [_hashed(r) for r in runs]}
        hashed["signatures"] = sorted([*key, value] for key, value in signed.items())
        source = Incremental(conn, config, as_of, runs, buckets, signed, prefetch)
    hashed |= {"release": release, "buckets": buckets, "evidence": evidence}
    # The incremental mode hashes the geo rows' rollup marks, the full mode the rows.
    geo = [g.plain() for g in scheduled["geo"]] if signed is None else rollups.marks(conn, as_of)
    hashed["scheduled"] = {k: v for k, v in scheduled.items() if k != "geo"} | {"geo": geo}
    fingerprint = digest(jsonable(hashed | {"implementation_id": calculator.IMPLEMENTATION_ID}))
    fields = {"runs": runs, "buckets": buckets, "release": release, "signed": signed}
    return Inputs(source=source, fingerprint=fingerprint, as_of=as_of, **common, **fields)


def _hashed(run):
    """A run as the fingerprint hashes it: without its summary, with its geo artifact and its
    latency model when it names them (lists.geo_artifact(), lists.latency_model()), so geo rows
    added to a stored run, or its latency patched, change the inputs."""
    found = {k: v for k, v in run.items() if k != "summary"}
    found |= {"geo": geo} if (geo := lists.geo_artifact(run)) else {}
    return found | {"latency_model": m} if (m := lists.latency_model(run)) else found


def periods(as_of):
    """The payload's periods (build()), which the list signatures read."""
    return {
        label: {"start": as_of - timedelta(hours=h), "end": as_of, "hours": h}
        for label, h in (("24h", 24), ("7d", 168), ("30d", 720))
    }


def _buckets(conn, start, as_of):
    """The window's buckets: the daily runs', then the batch runs' (rollups.scheduled_rows(), one
    read), together in the full calculation's order."""
    query = BUCKETS.format(join=" JOIN test_runs r USING(run_key)", daily=" AND r.batch_id IS NULL")
    found = conn.execute(query, (start, as_of)).fetchall()
    found += rollups.scheduled_rows(conn, start, as_of)
    return sorted(found, key=lambda b: (b["bucket_start"], b["run_key"]))


def group_runs(runs, config, overlaps):
    """Give each run its product group and its light and loaded flags, as build() does."""
    flagged = {o["run_key"]: o["census"] for o in overlaps if o["run_key"] is not None}
    timed_target = latency_target(config)
    for r in runs:
        kind = effective(config, r["provider"], r["name"]).get("kind", r["name"])
        cohort, _ = calculator.cohort_for(r["scope"], kind, r["batch_id"] is not None)
        r["group"], r["light"] = (r["product_id"], cohort), light_load(r, flagged, timed_target)
        r["loaded"] = under_load_run(r, flagged, timed_target)


class Incremental(Full):
    """build()'s source in the incremental mode: stored lists for the hit windows, the missed
    windows counted in the database, and the extension modules for the rest."""

    def __init__(self, conn, config, as_of, runs, buckets, signed, prefetch=None):
        active = {b["run_key"] for b in buckets}
        self.conn, self.as_of, self.limits = conn, as_of, payload_limits(config)
        self.signed, self.counted = signed, {}
        # group -> (prepared list, its reference, None) per missed window, or (None, its
        # reference, the reference of the earlier window whose list it shares: _same())
        self.unwritten = defaultdict(list)
        self.alike = {}  # group -> (inputs, geo rows, window()'s result) of its last own list
        self.deferred = None  # in a worker (parallel_measure.py): group -> its lists to write
        self.runs = defaultdict(dict)  # group -> run_key -> requested country, if in the window
        self.scope_of = ipstate.scopes(r for r in runs if r["run_key"] in active)
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
        # group -> hours of its missed windows, the groups in the order build() measures them
        # (calculator._measure(): product by product, each product's in the runs' order)
        products = {p: n for n, p in enumerate(dict.fromkeys(groups.values()))}
        measured = {g: n for n, g in enumerate(sorted(groups, key=lambda g: products[g[0]]))}
        self.missed = defaultdict(list)
        for g, h in sorted(missed, key=lambda m: (measured[m[0]], m[1])):
            self.missed[g].append(h)
        super().__init__(())  # no observation row: missed windows are counted in the database
        self._count(groups, windows, max(windows) // 24)
        self.prefetch = prefetch
        if prefetch is not None:  # each group's specs, made here: the helpers only read
            specs = {g: self._specs(g) for g in self.missed}
            read = lambda c, g: self._window_inputs(c, specs[g])  # noqa: E731
            prefetch.start(self.missed, read, self._rows(runs, buckets))

    def _hits(self, signed, windows):
        """The stored lists of the windows' signatures: {(group, hours): list row}."""
        keys = [(*key, signature) for key, signature in signed.items() if key[2] in windows]
        if not keys:
            return {}
        params = [list(column) for column in zip(*keys, strict=True)]
        rows = self.conn.cursor(row_factory=dict_row).execute(STORED, params)
        return {((r["product_id"], r["cohort"]), r["window_hours"]): r for r in rows}

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

    def window(self, key, seen, since, hours, hosting, geo, build_id=None):
        hit = self.hits.get((key, hours))
        if hit is None:
            return self._missed(key, since, hours, hosting, geo, build_id)
        figures, n = hit["figures"], self.ranges.get(key)
        found = None if n is None else self.figures[key][n]  # None: no run in the window
        new, size = hourly.new_ips(found, since), figures["unique_ips"]
        ps = {"unique_ips": size, "new_ips": new, "churn_rate": new / size if size else None}
        ps |= {k: figures[k] for k in POOL} | {"shares_basis": SHARES_BASIS}
        return self._referenced(ps, hit["list_id"], hit["entries"], figures["list_entries"])

    def _referenced(self, ps, list_id, entries, pooled):
        """window()'s result for a window that names its list: entries and pooled count the
        rows of its breakdowns and pool lists, the payload keeps the first ones (parts.fill())."""
        kept = {n: min(c, self.limits[n]) for n, c in (entries | pooled).items()}
        reference = {"lists": {"id": list_id, "entries": kept}, "breakdown_entries": entries}
        return ps, {}, {"list_entries": pooled}, reference  # no IPs: cohorts() counts them

    def _missed(self, key, since, hours, hosting, geo, build_id):
        """A missed window counted in the database: its pool figures, and its list (the
        breakdowns of its weighted label combinations and geo rows), written with the group's
        other missed lists once its last missed window is counted (_write()). An unsigned
        (census-pulse) window whose list would be the one of the group's last window with a list
        of its own shares that list: computed and written once (_same())."""
        inputs = self._inputs(key, hours)
        ps = pool_figures(inputs, since, hours, hosting)
        bins = sorted((at, n) for at, n in inputs.first_seen.items() if at >= since)
        alike = (inputs, len(geo), {k: ps.get(k) for k in list_write.FIGURES}, bins)
        same = self._same(key, hours, alike)
        if same is None:
            found = self._listed(key, hours, inputs, ps, geo, bins)
            self.alike[key] = (alike, found)
        else:
            entries, pooled = same[3]["breakdown_entries"], same[2]["list_entries"]
            found = self._referenced(ps, None, entries, pooled)
            self.unwritten[key].append((None, found[3]["lists"], same[3]["lists"]))
        if len(self.unwritten[key]) == len(self.missed[key]):
            self._write(key, build_id)
        return found

    def _listed(self, key, hours, inputs, ps, geo, bins):
        """window()'s result for a missed window with a list of its own, prepared for _write();
        bins: its first-seen bins from its start on."""
        weighted = {
            n: {"row": c.row(), "count": c.observations, "ips": c.ips}
            for n, c in enumerate(inputs.combos)
        }
        dimensions = breakdowns(weighted, geo)
        if any(not 0 <= r["successes"] <= r["completed"] for rs in dimensions.values() for r in rs):
            raise ValueError("Breakdown counters do not reconcile")  # as store.validate_payload()
        pooled = pool_lists(dimensions)
        window = {"breakdowns": dimensions, "pool": ps | pooled, "period": {"hours": hours}}
        window["first_seen"] = {iso(at): n for at, n in bins}  # list_write.UNHASHED
        group = {"product_id": key[0], "cohort": key[1]}
        prepared = list_write.prepare(group, window, self.signed.get((*key, hours)))
        entries = {name: len(found) for name, found in dimensions.items()}
        found = self._referenced(ps, None, entries, {n: len(pooled[n]) for n in POOL_LISTS})
        self.unwritten[key].append((prepared, found[3]["lists"], None))
        return found

    def _same(self, key, hours, alike):
        """window()'s result for the group's last window with a list of its own when this
        window's list would be that list (CALC-SCALE-1: while the data is younger than a day, the
        24h, 7d and 30d census-pulse lists are one list written three times). alike: the
        window's IP inputs, its geo rows (its group's rows of a class up to its hours, so as many
        rows are the same rows), list figures and first-seen bins, which give the list. A signed
        (daily) window keeps its own list, the one its signature names."""
        last = self.alike.get(key)
        if last is None or self.signed.get((*key, hours)) is not None:
            return None
        return last[1] if last[0] == alike else None

    def _write(self, key, build_id):
        """Write the lists of the group's missed windows and name each in its reference; a
        window sharing an earlier window's list names that list; a worker (parallel_measure.py)
        keeps them for the calculation to write (deferred)."""
        unwritten = self.unwritten.pop(key)
        self.alike.pop(key, None)
        if self.deferred is not None:
            self.deferred[key] = unwritten
        else:
            self.write_deferred(unwritten, build_id)

    def write_deferred(self, unwritten, build_id):
        """Write one group's missed windows' lists (_missed()'s) and name each in its reference,
        a window sharing an earlier window's list naming that list."""
        own = [(prepared, reference) for prepared, reference, _ in unwritten if prepared]
        ids = list_write.write_lists(self.conn, build_id, [prepared for prepared, _ in own])
        for (_, reference), list_id in zip(own, ids, strict=True):
            reference["id"] = list_id
        for prepared, reference, source in unwritten:
            if prepared is None:
                reference["id"] = source["id"]

    def _inputs(self, key, hours):
        """The window's sqlwindow inputs: on its group's first miss, every missed window of the
        group is counted in one statement (read ahead by the prefetcher, if any), and each is
        dropped once used."""
        if (key, hours) not in self.counted:
            if self.prefetch is None:
                self.counted |= self._window_inputs(self.conn, self._specs(key))
            else:
                self.counted |= self.prefetch.take(key)
        return self.counted.pop((key, hours))

    @staticmethod
    def _rows(runs, buckets):
        """Each group's estimated observation rows: its runs' successful requests in the window
        (each gives at most one row), from the bucket rows read already."""
        found, group = defaultdict(int), {r["run_key"]: r["group"] for r in runs}
        for b in buckets:
            found[group[b["run_key"]]] += b["counters"]["successes"]
        return found

    def _specs(self, key):
        asked, end = self.runs[key], self.as_of
        return [
            WindowSpec((key, h), key[0], tuple(asked), end - timedelta(hours=h), end, asked)
            for h in self.missed[key]
        ]

    @staticmethod
    def _window_inputs(conn, specs):
        return window_inputs(conn, specs, release_mode="per_run", release_id=None)

    def ips(self, key, hours, points):
        return fleetsql.fill(points, self.points.get((key, hours), {}))

    def cohorts(self, fleets, overlaps, fleet_groups, as_of):
        """build()'s fleet entries and overlaps with every IP count across groups from the
        database (fleetsql.py): its IP sets are empty."""
        members = {
            key: (entry, [(p, list(self.runs[p["product_id"], p["cohort"]])) for p, *_ in ms])
            for (key, ms), entry in zip(
                fleet_groups.items(), fleets[: len(fleet_groups)], strict=True
            )
        }
        windows, n = {}, len(fleet_groups)
        for (_, label), ms in fleet_groups.items():
            windows[label] = {"hours": ms[0][0]["windows"][label]["period"]["hours"]}
        for label, entry in zip(windows, fleets[n : n + len(windows)], strict=True):
            windows[label]["fleet"] = entry
        out = fleetsql.fleet_entries(self.conn, as_of, members, windows, self.scope_of)
        by_id = {p["id"]: p for ms in fleet_groups.values() for p, *_ in ms}
        for (group_id, label), shares in out["shares"].items():
            by_id[group_id]["windows"][label]["overlap"] = shares
        found = fleets[n + len(windows) :]
        kinds = self.kinds(found, fleet_groups, windows, as_of, out["vectors"])
        return out["fleet"] + kinds, out["overlap"]

    def kinds(self, entries, fleet_groups, windows, as_of, vectors=None):
        """The per-kind fleet entries (cohort "*:<kind>", fleet.fleet_totals()) with their
        distinct IPs counted in the database over their groups' runs (hourly.bucket_ips(), as a
        network's): the window's, from vectors (ipstate.py: the latest build) or as one point as
        wide as it, and each series point's."""
        if not entries:
            return []
        runs, buckets = defaultdict(set), {}
        for (_, label), ms in fleet_groups.items():
            for p, *_ in ms:
                runs[KIND + p["kind"], label] |= set(self.runs[p["product_id"], p["cohort"]])
        for e in entries:
            hours = windows[e["window"]]["hours"]
            since = as_of - timedelta(hours=hours)
            step = timedelta(minutes=5) if hours == 24 else timedelta(days=1)
            if vectors is None:
                buckets[e["cohort"], e["window"], "pool"] = (since, as_of, as_of - since)
            buckets[e["cohort"], e["window"], "series"] = (since, as_of, step)
        counted = hourly.bucket_ips(self.conn, {k: sorted(runs[k[:2]]) for k in buckets}, buckets)
        found = []
        for e in entries:
            key = (e["cohort"], e["window"])
            if vectors is None:
                ips = counted[(*key, "pool")].get(buckets[(*key, "pool")][0], 0)
            else:
                held = vectors[windows[e["window"]]["hours"]]
                ips = held.distinct({self.scope_of[k] for k in runs[key]})
            points = fleetsql.fill(e["series"], counted[(*key, "series")])
            found.append(e | {"unique_ips": ips, "series": points})
        return found
