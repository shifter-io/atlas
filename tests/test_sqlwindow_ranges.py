"""Database-counted run ranges and distinct IPs (calculated.sqlwindow) against the Python
originals on test_sqlwindow's scenario, the window statement's plan, and refused specs."""

from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.rows import dict_row
from test_sqlwindow import HOSTING, MINUTE, RUNS, observations, seeded, specs_of

from proxy_index.calculated.aggregate import pool
from proxy_index.calculated.poolfig import pool_figures
from proxy_index.calculated.sqlwindow import (
    WindowInputs,
    WindowSpec,
    distinct_ips,
    run_ranges,
    window_inputs,
    window_query,
)
from proxy_index.estimates import window as estimates_window
from proxy_index.storage import connect

pytest_plugins = ["test_storage", "calculated_template"]


@pytest.fixture
def database(calculated_database):
    """Every test here works on the current calculated schema, copied from its template."""
    return calculated_database


@pytest.fixture
async def scenario(database, tmp_path):
    """test_sqlwindow's seeded observations."""
    return await seeded(database, tmp_path)


def _buckets_of(conn, run_key):
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT bucket_start FROM ip_observations WHERE run_key=%s ORDER BY 1",
            (run_key,),
        )
    ]


async def test_run_ranges_equal_pool_of_each_range(scenario, database):
    newest = scenario["releases"][-1]
    keys = [k for k, _ in scenario["runs"]]
    products = dict(scenario["runs"])
    asked = dict(zip(keys, (c for _, c in RUNS), strict=True))
    with connect(database) as conn:
        own = dict(conn.execute("SELECT run_key,max(release_id) FROM run_enrichments GROUP BY 1"))
        buckets = {k: _buckets_of(conn, k) for k in keys}
        ranges = run_ranges(conn, keys, release_mode="per_run", release_id=newest)
        expected = set()
        for k, bs in buckets.items():
            for i, first in enumerate(bs if len(bs) <= 12 else ()):
                for last in bs[i:]:
                    key = (k, own.get(k, newest), first, last)
                    expected.add(key)
                    spec = WindowSpec(key, products[k], (k,), first, last + MINUTE, asked)
                    rows = observations(conn, spec, newest, per_run=True)
                    figures = pool_figures(ranges[key], first, 48, HOSTING)
                    assert figures == pool(rows, first, 48, HOSTING)[0], key
        assert set(ranges) == expected
        assert any(len(bs) > 12 for bs in buckets.values())  # the multi-day run has none
        short = run_ranges(conn, keys, release_mode="newest", release_id=newest, max_buckets=3)
    small = [len(bs) for bs in buckets.values() if len(bs) <= 3]
    assert small and len(short) == sum(n * (n + 1) // 2 for n in small)
    assert {k[1] for k in short} == {newest}


async def test_distinct_ips_equal_the_estimates_count(scenario, database):
    runs = [k for k, _ in scenario["runs"]]
    sets = [
        [(runs[0], "US"), (runs[1], "US"), (runs[2], "DE")],
        [(runs[3], "FR")],
        [(runs[0], "US"), (runs[5], "US")],
    ]
    found = [(None, [{"run_key": k, "country_code": c} for k, c in pairs]) for pairs in sets]
    with connect(database) as conn:
        conn.row_factory = dict_row
        expected = estimates_window.distinct(conn, found)
        assert distinct_ips(conn, sets) == expected
        assert distinct_ips(conn, sets, by_country=False) == [{None: e[None]} for e in expected]
        assert distinct_ips(conn, []) == []


async def test_the_window_statement_reads_observations_by_primary_key(scenario, database):
    specs = specs_of(scenario)
    statement, params, _ = window_query(specs, release_mode="per_run", release_id=None)
    with connect(database) as conn, conn.transaction():
        conn.execute("SET LOCAL enable_seqscan = off")
        cursor = psycopg.ClientCursor(conn)
        plan = cursor.execute("EXPLAIN (FORMAT JSON) " + statement, params).fetchone()[0]
    scans = list(_scans(plan[0]["Plan"]))
    assert scans and all(name.endswith("_pkey") for name in scans), scans


def _scans(node):
    """The index (else the node type) of every scan of ip_observations in a plan."""
    kind = node["Node Type"]
    if kind == "Bitmap Index Scan" and node["Index Name"].startswith("ip_observations"):
        yield node["Index Name"]
    elif node.get("Relation Name", "").startswith("ip_observations") and kind != "Bitmap Heap Scan":
        yield node.get("Index Name", kind)
    for child in node.get("Plans", []):
        yield from _scans(child)


def test_bad_specs_and_modes_are_refused():
    t = datetime(2026, 11, 4, tzinfo=UTC)
    with pytest.raises(ValueError, match="ends before"):
        window_query([WindowSpec("w", 1, (1,), t, t)], release_mode="newest", release_id=None)
    with pytest.raises(ValueError, match="release_mode"):
        window_query([], release_mode="latest", release_id=None)
    with pytest.raises(ValueError, match="distinct"):
        window_inputs(
            None,
            [WindowSpec("w", 1, (), t, t + MINUTE)] * 2,
            release_mode="newest",
            release_id=None,
        )
    with pytest.raises(ValueError, match="5-minute grid"):
        pool_figures(WindowInputs(), t + MINUTE, 24)
    assert pool_figures(WindowInputs(), t, 24) == pool([], t, 24)[0]
    assert window_inputs(None, [], release_mode="newest", release_id=None) == {}
