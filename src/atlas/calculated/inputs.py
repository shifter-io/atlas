"""Calculation inputs grouped per product for build()."""

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .aggregate import floor_time
from .browser import NOT_BROWSER

BUCKET = timedelta(minutes=5)
HOUR = timedelta(hours=1)
COUNTERS = ("completed", "successes", "challenges", "blocks")  # scheduled.COUNTERS


def group_by_product(rows, drop_id=False):
    """Rows by product_id, without that key when drop_id is set."""
    groups = defaultdict(list)
    for r in rows:
        groups[r["product_id"]].append(
            {k: v for k, v in r.items() if k != "product_id"} if drop_id else r
        )
    return groups


def light_load(run, flagged_runs, timed_target):
    """Whether a run's histograms count for the breakdowns' latency: light load of the latency
    target (pulses and runs outside a batch), never a census or load run nor a pulse that ran
    during a census (flagged_runs). build() and the list signature (lists.py) use it."""
    return (
        run.get("test") in (None, "pulse")
        and not flagged_runs.get(run["run_key"])
        and run["scope"]["target"]["id"] == timed_target
    )


def under_load_run(run, flagged_runs, timed_target):
    """Whether a run's requests measure the latency under load: a run of the latency target at
    load concurrency (a daily test outside a batch, a census or a load run), not a pulse and not
    run during a census (flagged_runs). Pulses measure the latency of normal requests."""
    return (
        run.get("test") != "pulse"
        and not flagged_runs.get(run["run_key"])
        and run["scope"]["target"]["id"] == timed_target
    )


def requested_geo(rows, as_of, names):
    """Geo rows (as scheduled.geo_rows()'s) of the requests no exit place records: the bucket rows
    of placeless runs (a daily test stores no geo rows: daily.ingest) with a requested country,
    summed per smallest window (hours) and that country, without location or network, so
    aggregate.breakdowns() counts them towards the requested country's row (its fallback, named
    from names), as a request without an exit address. Counters keep every request; the
    histogram the light-load ones (the latency target's, as scheduled.LIGHT)."""
    found = {}
    for row in rows:
        at, cc = row["bucket_start"], row.get("country")
        if not row.get("placeless") or not cc or not as_of - 720 * HOUR <= at < as_of:
            continue
        hours = 24 if at >= as_of - 24 * HOUR else 168 if at >= as_of - 168 * HOUR else 720
        g = found.get((hours, cc))
        if g is None:
            g = found[hours, cc] = {"hours": hours, "location": None, "network": None}
            g |= {"fallback": (cc, names.get(cc)), "histogram": Counter()}
            g |= dict.fromkeys(COUNTERS, 0)
        c = row["counters"]
        for k in COUNTERS:
            g[k] += c.get(k, 0)
        if row.get("light"):
            g["histogram"].update(dict(c["latency_histogram"]["bins"]))
    return [g | {"histogram": sorted(g["histogram"].items())} for _, g in sorted(found.items())]


@dataclass(frozen=True, slots=True)
class RunFields:
    """What a run adds to each of its bucket rows (BucketRow): its fields, shared by its buckets,
    and what its rows' slot and last_observed derive from (slot_at: its batch's slot, if any)."""

    fields: dict
    slot_at: datetime | None
    seconds: int
    finished_at: datetime


class BucketRow(Mapping):
    """A group's bucket row (calculator._group_inputs()): its bucket's fields, its run's (RunFields)
    and its slot and last_observed, derived when read. It references the bucket and its run instead
    of copying them into one dict per bucket (CALC-MEM-2); their keys are apart."""

    __slots__ = ("bucket", "run")
    DERIVED = ("slot", "last_observed")

    def __init__(self, bucket, run):
        self.bucket, self.run = bucket, run

    def __getitem__(self, key):
        if key in self.bucket:
            return self.bucket[key]
        if key == "slot":
            return floor_time(self.run.slot_at or self.bucket["bucket_start"], self.run.seconds)
        if key == "last_observed":
            return min(self.run.finished_at, self.bucket["bucket_start"] + BUCKET)
        return self.run.fields[key]

    def __iter__(self):
        yield from self.bucket
        yield from self.run.fields
        yield from self.DERIVED

    def __len__(self):
        return len(self.bucket) + len(self.run.fields) + len(self.DERIVED)

    def __or__(self, other):
        return dict(self) | other


class GeoGroups:
    """Each product group's geo rows, read per product (CALC-MEM-2): a product's rows are read when
    one of its groups asks for them (geo.product(): rollups.ProductGeo, or the full mode's rows
    picked from its list), given to its groups (scheduled: each product's census-pulse group,
    scopes: each scope's group) and dropped when another product is read."""

    def __init__(self, geo, scheduled, scopes):
        if not hasattr(geo, "product"):
            geo = _Listed(geo)
        self.geo, self.scheduled, self.scopes = geo, scheduled, scopes
        self.product_id, self.groups = None, {}

    def __getitem__(self, key):
        """The geo rows of group key ((product_id, cohort)), in their read order."""
        if key[0] != self.product_id:
            self.product_id, self.groups = key[0], defaultdict(list)
            for g in self.geo.product(key[0]):
                scope = g["scope_id"]
                group = self.scheduled.get(key[0]) if scope is None else self.scopes.get(scope)
                if group:
                    self.groups[group].append(g)
        return self.groups.pop(key, [])


class _Listed:
    """The geo rows of a list (the full mode's, read up front) by product, as rollups.ProductGeo."""

    def __init__(self, rows):
        self.rows = group_by_product(rows)

    def product(self, product_id):
        return self.rows.get(product_id, [])


def tested_by_product(tested):
    """Per product: the latest test time per country, and the tested rows with trimmed countries."""
    tested_groups, monitored_groups = defaultdict(dict), defaultdict(list)
    for t in tested:
        country, group = t["country"].strip(), tested_groups[t["product_id"]]
        group[country] = max(group.get(country, t["last"]), t["last"])
        monitored_groups[t["product_id"]].append(t | {"country": country})
    return tested_groups, monitored_groups


def previous_products(previous, as_of):
    """The previous snapshot's products by id (none if it is newer than as_of).

    previous is parts.previous(): as_of, the methodology id and each group's id and scores only.
    Their scores are stamped with the previous methodology id where they lack one.
    """
    if not previous or datetime.fromisoformat(previous["as_of"]) > as_of:
        return {}
    for old_product in previous["products"]:
        for old_window in old_product["windows"].values():
            old_scores = [*old_window["scores"].values()]
            old_scores += [t["score"] for t in old_window["targets"].values()]
            for old_score in old_scores:
                old_score.setdefault("methodology_id", previous["methodology"]["id"])
    return {p["id"]: p for p in previous["products"]}


def cutoff(conn, requested, latest):
    """The calculation's cutoff: `requested` on the 5-minute bucket grid of the stored facts.

    Exact half-open windows need whole buckets, so a time inside a bucket moves to one of its
    edges. It moves to the bucket's end when the bucket holds data and every run with data in it
    had finished by `requested` (always so for `latest`, a calculation of everything stored now:
    a run is stored only once it has finished), so a run that ended just before the calculation
    is in it. Otherwise it moves to the bucket's start, which leaves out any part of a run that
    ended after `requested`. Each bucket is in a snapshot once or not at all. Only network runs
    count: a browser run never moves the cutoff (so never a network window); a bucket that holds
    only browser data waits for a later calculation.
    """
    start = datetime.fromtimestamp(int(requested.timestamp() // 300) * 300, UTC)
    if start == requested:
        return start
    row = conn.execute(
        "SELECT count(*) AS runs,count(*) FILTER (WHERE r.finished_at>%s) AS running "
        "FROM (SELECT DISTINCT run_key FROM run_buckets WHERE bucket_start=%s) k "
        "JOIN test_runs r USING(run_key) LEFT JOIN test_batches b ON b.batch_id=r.batch_id "
        f"WHERE {NOT_BROWSER}",
        (requested, start),
    ).fetchone()
    closed = row["runs"] and (latest or not row["running"])
    return start + timedelta(minutes=5) if closed else start
