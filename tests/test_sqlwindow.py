"""Database-counted window figures (calculated.sqlwindow, calculated.poolfig) against the Python
originals: aggregate.pool() and breakdowns() over the calculator's observation rows, and a whole
calculate() run. Run ranges, distinct IPs and the statement's plan: test_sqlwindow_ranges."""

import json
import random
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.rows import dict_row
from test_storage import measurement

from atlas.calculated import calculator, store
from atlas.calculated.aggregate import breakdowns, pool
from atlas.calculated.poolfig import pool_figures
from atlas.calculated.sqlwindow import (
    LABELS,
    WindowInputs,
    WindowSpec,
    window_inputs,
)
from atlas.partitions import partition
from atlas.storage import connect, ingest

pytest_plugins = ["test_storage", "calculated_template"]


@pytest.fixture
def database(calculated_database):
    """Every test here works on the current calculated schema, copied from its template."""
    return calculated_database


POLICY = "configs/methodology.toml"
HOSTING = ["hosting", "datacenter"]
MINUTE = timedelta(minutes=1)
# Places (country_code, country, region_code, region, city) and networks (asn, asn_organization,
# usage_type) that exercise the calculator's truthiness: empty strings, NULLs, region names.
PLACES = [
    ("US", "United States", "CA", "California", "Los Angeles"),
    ("US", "United States", "CA", "California", None),
    ("US", "United States", None, "Texas", "Austin"),
    ("US", "United States", "", None, "Reno"),
    ("DE", "Germany", "BE", "Berlin", "Berlin"),
    ("DE", "Germany", None, None, None),
    ("FR", "France", "IDF", "Ile-de-France", "Paris"),
    ("", "Nowhere", None, None, "Ghost"),
    (None, None, None, None, None),
]
NETWORKS = [
    (64500, "Alpha Net", "hosting"),
    (64500, "Alpha Net", "isp"),
    (64501, "Beta", "residential"),
    (64502, "Gamma", ""),
    (64503, "Delta", None),
    (None, None, "datacenter"),
    (None, None, None),
]
# Runs: (provider, requested country); the first five are one product.
RUNS = [("fixture", "us"), ("fixture", "us"), ("fixture", "de"), ("fixture", None)]
RUNS += [("fixture", "US"), ("second", "us")]


def _floor(moment):
    return datetime.fromtimestamp(moment.timestamp() // 300 * 300, UTC)


def _buckets(as_of):
    """Observation buckets per run: short runs (run_ranges), a multi-day run, window edges."""
    day = timedelta(days=1)
    return [
        [as_of - k * 5 * MINUTE for k in range(1, 11)],  # ten consecutive, up to the cutoff
        [as_of - day, as_of - day + 5 * MINUTE, as_of - day + 15 * MINUTE, as_of],
        [as_of - k * (day + 35 * MINUTE) for k in range(1, 16)],  # fifteen days
        [
            as_of - 31 * day,
            as_of - 30 * day - 5 * MINUTE,
            as_of - 30 * day,
            as_of - 2 * 60 * MINUTE,
        ],
        [as_of - 3 * 60 * MINUTE, as_of - 2 * day],
        [as_of - k * 6 * 60 * MINUTE for k in range(1, 6)],
    ]


def _ids(conn, table, identity, columns, values):
    names = ",".join(columns)
    marks = ",".join(["%s"] * len(columns))
    conn.execute(f"INSERT INTO {table}({names}) VALUES ({marks}) ON CONFLICT DO NOTHING", values)
    where = " AND ".join(f"{c} IS NOT DISTINCT FROM %s" for c in columns)
    return conn.execute(f"SELECT {identity} FROM {table} WHERE {where}", values).fetchone()[0]


def _labels(conn, rng, releases, ips):
    locations = []
    for place in PLACES:
        place_id = _ids(conn, "place_names", "place_id", LABELS[:5], place)
        locations.append(
            _ids(conn, "enrichment_locations", "location_id", ("place_id",), (place_id,))
        )
    networks = []
    for asn, name, usage in NETWORKS:
        asn_name_id = _ids(conn, "asn_names", "asn_name_id", ("asn", "name"), (asn, name))
        columns = ("asn_name_id", "isp", "organization", "connection_type", "usage_type")
        values = (asn_name_id, None, None, None, usage)
        networks.append(_ids(conn, "enrichment_networks", "network_id", columns, values))
    for release in releases:
        for ip_id in ips:
            if rng.random() < 0.85:  # the rest stay unenriched in this release
                conn.execute(
                    "INSERT INTO ip_enrichment(ip_id,release_id,location_id,network_id,matched) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (
                        ip_id,
                        release,
                        rng.choice(locations),
                        rng.choice(networks),
                        rng.choice([True, False, None]),
                    ),
                )


async def _runs(database, tmp_path):
    keys = []
    for i, (provider, country) in enumerate(RUNS):
        d = await measurement(
            tmp_path / f"run{i}", requests=2, provider=provider, requested_country=country
        )
        ingest(d, database)
        keys.append(json.loads((d / "summary.json").read_text())["run_id"])
    with connect(database) as conn:
        rows = conn.execute(
            "SELECT r.run_id::text,r.run_key,s.product_id FROM test_runs r "
            "JOIN scope_stats s USING(scope_id)"
        ).fetchall()
    found = {run_id: (run_key, product) for run_id, run_key, product in rows}
    return [found[run_id] for run_id in keys]


async def seeded(database, tmp_path):
    """Six runs of two products with seeded random observations of 240 documentation IPs over
    31 days, three DB-IP releases, unenriched IPs and per-run releases."""
    runs = await _runs(database, tmp_path)
    rng = random.Random(20261104)
    as_of = _floor(datetime.now(UTC)) + 10 * MINUTE
    with connect(database) as conn:
        releases = [
            conn.execute(
                "INSERT INTO dbip_releases(sha256,metadata) VALUES (%s,'{}') RETURNING release_id",
                (f"{n:064x}",),
            ).fetchone()[0]
            for n in (11, 12, 13)
        ]
        addresses = [f"198.51.100.{n}" for n in range(1, 121)]
        addresses += [f"203.0.113.{n}" for n in range(1, 101)]
        addresses += [f"2001:db8::{n:x}" for n in range(1, 21)]
        ips = [
            conn.execute(
                "INSERT INTO ip_addresses(ip) VALUES (%s) RETURNING ip_id", (address,)
            ).fetchone()[0]
            for address in addresses
        ]
        _labels(conn, rng, releases, ips)
        for (run_key, _), enriched in zip(runs, ([0], [0, 1], [1], [], [], [2]), strict=True):
            for i in enriched:
                conn.execute(
                    "INSERT INTO run_enrichments VALUES (%s,%s,'fixture','{}')",
                    (run_key, releases[i]),
                )
        seen = set()
        for (run_key, product), buckets in zip(runs, _buckets(as_of), strict=True):
            for bucket in buckets:
                partition(conn, "ip_observations", bucket)
                for ip_id in rng.sample(ips, rng.randint(8, 30)):
                    seen.add((product, ip_id))
                    conn.execute(
                        "INSERT INTO ip_observations VALUES (%s,%s,%s,%s,%s,%s)",
                        (bucket, run_key, ip_id, rng.randint(1, 4), bucket, bucket + MINUTE),
                    )
        edges = [as_of - timedelta(hours=24), as_of - timedelta(hours=24, microseconds=1)]
        for product, ip_id in sorted(seen):
            first = (
                rng.choice(edges)
                if rng.random() < 0.2
                else as_of - rng.random() * 40 * (24 * 60 * MINUTE)
            )
            conn.execute(
                "INSERT INTO product_ips VALUES (%s,%s,%s,%s,1,1)",
                (product, ip_id, first, as_of + 24 * 60 * MINUTE),
            )
    assert len({ip for _, ip in seen}) >= 200
    return {"runs": runs, "as_of": as_of, "releases": releases}


@pytest.fixture
async def scenario(calculated_database, tmp_path):
    """`seeded` in `calculated_database`: the `database` of this module's tests, and of the test
    of test_calculated_listref that borrows this fixture."""
    return await seeded(calculated_database, tmp_path)


# --- the Python originals ------------------------------------------------------------------

ROWS = (
    "SELECT o.run_key,o.bucket_start,o.ip_id,o.count,o.first_seen,o.last_seen,"
    "m.first_seen AS product_first_seen FROM ip_observations o "
    "JOIN product_ips m ON m.product_id=%s AND m.ip_id=o.ip_id "
    "WHERE o.run_key=ANY(%s) AND o.bucket_start>=%s AND o.bucket_start<%s "
    "ORDER BY o.run_key,o.bucket_start,o.ip_id"
)


def _run_releases(conn, newest, per_run):
    if not per_run:
        return lambda run_key: newest
    own = dict(
        conn.execute("SELECT run_key,max(release_id) FROM run_enrichments GROUP BY 1").fetchall()
    )
    return lambda run_key: own.get(run_key, newest)


def observations(conn, spec, newest, per_run=False):
    """The calculator's rows of one window: its query's fields, labelled from each run's
    release, with the run's requested country, in pool()'s order."""
    labels = {
        (r["ip_id"], r["release_id"]): r
        for r in conn.cursor(row_factory=dict_row).execute(
            "SELECT ip_id,release_id,matched," + ",".join(LABELS) + " FROM ip_enrichment_fields"
        )
    }
    release = _run_releases(conn, newest, per_run)
    fields = ("matched", *LABELS)
    rows = conn.cursor(row_factory=dict_row).execute(
        ROWS, (spec.product_id, list(spec.runs), spec.since, spec.until)
    )
    result = []
    for r in rows:
        label = labels.get((r["ip_id"], release(r["run_key"])), {})
        result.append(
            r
            | {k: label.get(k) for k in fields}
            | {"requested_country": spec.asked.get(r["run_key"])}
        )
    return result


def expanded(inputs: WindowInputs) -> dict:
    """pool() entries standing for the combinations: one per IP, the observations on the
    first, so breakdowns() over them equals breakdowns() over the real IPs."""
    entries, n = {}, 0
    for c in inputs.combos:
        for i in range(c.ips):
            entries[n] = {"row": c.row(), "count": c.observations if i == 0 else 0}
            n += 1
    return entries


def specs_of(scenario):
    (r0, p), (r1, _), (r2, _), (r3, _), (r4, _) = scenario["runs"][:5]
    r5, q = scenario["runs"][5]
    as_of, asked = scenario["as_of"], {r0: "us", r1: "us", r2: "de", r3: None, r4: "US"}
    hours = timedelta(hours=1)
    alpha = (r0, r1, r2, r3, r4)
    specs = [
        WindowSpec(("all", h), p, alpha, as_of - h * hours, as_of, asked) for h in (24, 168, 720)
    ]
    return specs + [
        WindowSpec("two countries", p, (r0, r2), as_of - 720 * hours, as_of, asked),
        WindowSpec("same country", p, (r0, r4), as_of - 720 * hours, as_of, asked),
        WindowSpec("edge", p, (r1,), as_of - 24 * hours, as_of - 23 * hours - 50 * MINUTE, asked),
        WindowSpec("old", p, (r3,), as_of - 744 * hours, as_of - 720 * hours, asked),
        WindowSpec("empty", p, (r0,), as_of - 960 * hours, as_of - 936 * hours, asked),
        WindowSpec("other product", q, (r5,), as_of - 168 * hours, as_of, {r5: "us"}),
    ]


def _hours(spec):
    return round((spec.until - spec.since).total_seconds() / 3600)


@pytest.mark.parametrize("mode", ["newest", "per_run", "none"])
async def test_window_inputs_equal_pool_and_breakdowns(scenario, database, mode):
    newest = None if mode == "none" else scenario["releases"][-1]
    per_run = mode == "per_run"
    specs = specs_of(scenario)
    with connect(database) as conn:
        found = window_inputs(
            conn, specs, release_mode="per_run" if per_run else "newest", release_id=newest
        )
        assert list(found) == [s.key for s in specs]
        checked = 0
        for spec in specs:
            rows = observations(conn, spec, newest, per_run)
            expected, ips = pool(rows, spec.since, _hours(spec), HOSTING)
            inputs = found[spec.key]
            assert pool_figures(inputs, spec.since, _hours(spec), HOSTING) == expected, spec.key
            assert breakdowns(expanded(inputs), []) == breakdowns(ips, []), spec.key
            assert sum(c.ips for c in inputs.combos) == inputs.unique_ips == len(ips)
            checked += bool(rows)
    assert checked == len(specs) - 1  # only "empty" has no rows
    assert found["empty"] == WindowInputs()


async def test_the_fixture_covers_the_edge_cases(scenario, database):
    specs = {s.key: s for s in specs_of(scenario)}
    with connect(database) as conn:
        found = window_inputs(
            conn, specs.values(), release_mode="per_run", release_id=scenario["releases"][-1]
        )
    whole = found[("all", 720)]
    assert whole.unique_ips >= 200 and 0 < whole.recurring_ips < whole.unique_ips
    places = {c.place for c in whole.combos}
    assert (None,) * 5 in places and any(p[0] == "" for p in places)
    # IPs returned for two requested countries are located but never match.
    mixed = found["two countries"]
    assert sum(c.geo_matched for c in mixed.combos) < sum(c.located for c in mixed.combos)
    # Requested countries compare in lower case: "us" and "US" are one country.
    same = found["same country"]
    assert sum(c.geo_matched for c in same.combos) > 0
    edge = found["edge"]  # [as_of-24h, as_of-23h50m): the first two buckets of the run
    assert 0 < edge.unique_ips < found[("all", 24)].unique_ips
    assert scenario["as_of"] - timedelta(hours=24) in whole.first_seen


async def test_window_inputs_match_a_whole_calculation(scenario, database, monkeypatch):
    """Every product window of calculate() (each run's own release, none for a run without
    one): pool counts and per-label IP and observation counts of every breakdown."""
    seen = {}
    original = calculator.build

    def build(inputs, *args, **kwargs):
        seen["runs"] = inputs.runs
        seen["payload"] = original(inputs, *args, **kwargs)
        return seen["payload"]

    monkeypatch.setattr(calculator, "build", build)
    # The synthetic observations have no request counters, so the payload check, which
    # compares observations with successful requests, does not apply to this fixture.
    monkeypatch.setattr(store, "validate_payload", lambda payload, products=None: None)
    as_of = scenario["as_of"]
    calculator.calculate(database, POLICY, as_of=as_of)
    groups = {}
    for r in seen["runs"]:
        groups.setdefault(r["group"], []).append(r)
    specs = []
    for product in seen["payload"]["products"]:
        runs = groups[(product["product_id"], product["cohort"])]
        asked = {r["run_key"]: r["scope"]["config"].get("requested_country") for r in runs}
        for label, w in product["windows"].items():
            key = (product["id"], label)
            hours = w["period"]["hours"]
            since = as_of - timedelta(hours=hours)
            spec = WindowSpec(key, product["product_id"], tuple(asked), since, as_of, asked)
            specs.append((spec, product, w))
    with connect(database) as conn:
        # calculate() labels each run with its own newest release (calculated/releases.py) and
        # leaves a run without one unlabelled: "per_run" mode with no fallback release.
        found = window_inputs(
            conn, [s for s, _, _ in specs], release_mode="per_run", release_id=None
        )
    assert len(specs) >= 12
    for spec, product, w in specs:
        hosting = product["scoring"]["hosting_usage_types"]
        figures = pool_figures(found[spec.key], spec.since, w["period"]["hours"], hosting)
        assert figures == {k: w["pool"][k] for k in figures}, spec.key
        assert _counted(breakdowns(expanded(found[spec.key]), [])) == _counted(w["breakdowns"])


def _counted(dimensions):
    """Each breakdown's rows with IPs: their labels, IPs and observations."""
    return {
        name: sorted(
            (
                json.dumps({k: r[k] for k in r if k in (*LABELS, "name")}),
                r["ips"],
                r["observations"],
            )
            for r in rows
            if r["ips"]
        )
        for name, rows in dimensions.items()
    }
