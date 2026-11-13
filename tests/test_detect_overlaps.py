"""AUDIT-DAILY-2: detect.overlaps() counts shared addresses with hash aggregates and joins.

The old form counted each row's shared address with an EXISTS subquery over the materialized
rows, a SubPlan per row that never finished on a real hour (893,744 rows). The new form must
give the same (product_id, data) lists, finish 100k rows in seconds and plan no SubPlan.
"""

from __future__ import annotations

import json
import random
import time

import pytest
from psycopg.rows import dict_row

from atlas import detect
from atlas.storage import connect

pytest_plugins = ["test_storage"]

SOURCE = "SELECT product_id,provider,ip_id FROM o_fixture"
TABLE = "CREATE TEMP TABLE o_fixture (product_id integer, provider text, ip_id bigint)"
# An hour of `size` rows (%s): product p belongs to provider "p<p>"; about 84% of the
# addresses are one provider's, 16% are shared by three providers and a few by six
# (AUDIT-DAILY-2's shape of a real hour).
SYNTHETIC = (
    "INSERT INTO o_fixture SELECT DISTINCT g%%18+1,'p'||(g%%18+1),CASE WHEN g%%100<62 THEN g "
    "WHEN g%%100<97 THEN 100000000+g/3 ELSE 200000000+g/200 END FROM generate_series(1,%s) g"
)


def old_overlaps(cur, source, params):
    """The form before AUDIT-DAILY-2, kept as the reference the rewrite must match."""
    query = "WITH o AS MATERIALIZED (" + source + ") "
    totals = {
        row["product_id"]: row
        for row in cur.execute(
            query + "SELECT a.product_id,count(*) AS unique_ips,count(*) FILTER (WHERE EXISTS "
            "(SELECT 1 FROM o b WHERE b.ip_id=a.ip_id AND b.provider<>a.provider)) AS shared "
            "FROM o a GROUP BY 1",
            params,
        )
    }
    peers = {}
    for row in cur.execute(
        query + "SELECT a.product_id,b.provider,count(DISTINCT a.ip_id) AS shared FROM o a "
        "JOIN o b ON b.ip_id=a.ip_id AND b.provider<>a.provider GROUP BY 1,2 ORDER BY 1,2",
        params,
    ):
        peers.setdefault(row["product_id"], {})[row["provider"]] = row["shared"]
    return [
        (
            product,
            {
                "value": row["shared"] / row["unique_ips"],
                "shared_ips": row["shared"],
                "unique_ips": row["unique_ips"],
                "peers": peers.get(product, {}),
            },
        )
        for product, row in sorted(totals.items())
        if row["shared"]
    ]


class Recording:
    """A cursor that keeps every query it runs, to EXPLAIN them afterwards."""

    def __init__(self, cur):
        self.cur, self.queries = cur, []

    def execute(self, query, params=None):
        self.queries.append((query, params))
        return self.cur.execute(query, params)


def fill(cur, rows):
    cur.execute("TRUNCATE o_fixture")
    with cur.copy("COPY o_fixture (product_id,provider,ip_id) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)


def plans(cur, function):
    """The JSON plans of every query `function` runs over the fixture."""
    recording = Recording(cur)
    function(recording, SOURCE, ())
    return [
        json.dumps(cur.execute("EXPLAIN (FORMAT JSON) " + query, params).fetchone())
        for query, params in recording.queries
    ]


@pytest.fixture
def cur(database):
    with connect(database) as conn:
        cursor = conn.cursor(row_factory=dict_row)
        cursor.execute(TABLE)
        yield cursor
        conn.rollback()


def test_addresses_in_one_two_and_five_providers(cur):
    # Address 1: alpha only. 2: alpha and beta. 3: five providers. 4: beta's two products.
    rows = [(1, "alpha", 1), (1, "alpha", 2), (1, "alpha", 3), (2, "beta", 2), (2, "beta", 3)]
    rows += [(3, "beta", 4), (2, "beta", 4)]
    rows += [(product, f"p{product}", 3) for product in (4, 5, 6)]
    fill(cur, rows)
    found = detect.overlaps(cur, SOURCE, ())
    assert found == old_overlaps(cur, SOURCE, ())
    assert found == [
        (
            1,
            {
                "value": 2 / 3,
                "shared_ips": 2,
                "unique_ips": 3,
                "peers": {"beta": 2, "p4": 1, "p5": 1, "p6": 1},
            },
        ),
        (
            2,
            {
                "value": 2 / 3,
                "shared_ips": 2,
                "unique_ips": 3,
                "peers": {"alpha": 2, "p4": 1, "p5": 1, "p6": 1},
            },
        ),
        (
            4,
            {
                "value": 1.0,
                "shared_ips": 1,
                "unique_ips": 1,
                "peers": {"alpha": 1, "beta": 1, "p5": 1, "p6": 1},
            },
        ),
        (
            5,
            {
                "value": 1.0,
                "shared_ips": 1,
                "unique_ips": 1,
                "peers": {"alpha": 1, "beta": 1, "p4": 1, "p6": 1},
            },
        ),
        (
            6,
            {
                "value": 1.0,
                "shared_ips": 1,
                "unique_ips": 1,
                "peers": {"alpha": 1, "beta": 1, "p4": 1, "p5": 1},
            },
        ),
    ]  # beta's product 3 shares address 4 with beta only: not shared


def test_random_fixtures_match_the_old_form(cur):
    for seed in range(40):
        rng = random.Random(seed)
        products = {p: f"p{rng.randrange(1, 5)}" for p in range(1, rng.randrange(2, 8))}
        addresses = rng.randrange(1, 30)
        rows = {
            (product, provider, rng.randrange(addresses))
            for product, provider in products.items()
            for _ in range(rng.randrange(0, 25))
        }
        fill(cur, sorted(rows))
        assert detect.overlaps(cur, SOURCE, ()) == old_overlaps(cur, SOURCE, ()), seed


def test_the_plan_has_no_subplan_and_the_old_one_had(cur):
    fill(cur, [(1, "alpha", 1), (2, "beta", 1)])
    assert all("SubPlan" not in plan for plan in plans(cur, detect.overlaps))
    assert any("SubPlan" in plan for plan in plans(cur, old_overlaps))


@pytest.mark.serial
def test_a_100k_row_hour_finishes_within_two_seconds(cur):
    size = 100_000
    cur.execute(SYNTHETIC, (size,))
    cur.execute("ANALYZE o_fixture")
    rows = cur.execute("SELECT count(*) AS n FROM o_fixture").fetchone()["n"]
    began = time.monotonic()
    found = detect.overlaps(cur, SOURCE, ())
    seconds = time.monotonic() - began
    assert rows > 95_000 and len(found) == 18
    assert all(0.2 < data["value"] < 0.6 for _, data in found)
    assert seconds < 2, f"{rows} rows took {seconds:.2f} s"
    assert all("SubPlan" not in plan for plan in plans(cur, detect.overlaps))
