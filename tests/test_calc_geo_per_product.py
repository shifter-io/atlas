"""CALC-MEM-2: the incremental build reads its census-pulse geo rows per product (one product's at a
time, rollups.ProductGeo through inputs.GeoGroups) instead of holding every product's for the
whole build, and its bucket rows reference each bucket and its run's fields (inputs.BucketRow)
instead of copying them into one dict per bucket.

On test_calc_rollups' 3-day fixture: each product's rows equal its rows of the full mode's 30-day
read, on the hour and inside one (raw edges); the incremental build's scheduled input holds no geo
row, reads each product once, holds one product's rows at a time and equals the full build.
"""

from datetime import UTC, datetime, timedelta

import pytest
from calculated_cases import lenient_policy
from psycopg.rows import dict_row
from test_calc_rollups import bounds, fixture
from test_calculated_hourly_reads import HOUR, floor_hour
from test_calculated_incremental import assert_equal

from atlas.calculated import calculator, incremental, inputs, rollups, scheduled
from atlas.calculated.inputs import BucketRow, GeoGroups, RunFields
from atlas.calculated.policy import load
from atlas.storage import connect

pytest_plugins = ["test_storage"]

AT = datetime(2026, 11, 6, 10, 7, tzinfo=UTC)


def test_a_bucket_row_reads_like_the_copy_it_replaces():
    bucket = {"bucket_start": AT, "run_key": 4, "counters": {"completed": 3}, "outcome": True}
    fields = {"target": "generic_http", "test": "pulse", "light": True, "country": "DE"}
    slot_at = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
    run = RunFields(fields, slot_at, 1800, AT + timedelta(minutes=2))
    row = BucketRow(bucket, run)
    copy = bucket | fields | {"slot": slot_at, "last_observed": AT + timedelta(minutes=2)}
    assert dict(row) == copy and row == copy and len(row) == len(copy)
    assert row["counters"] is bucket["counters"] and row.get("region") is None
    assert "light" in row and "region" not in row
    assert row | {"light": False} == copy | {"light": False}
    with pytest.raises(KeyError):
        row["region"]
    # Without a batch slot: the slot its bucket starts in, and a run that ended later.
    later = BucketRow(bucket, RunFields(fields, None, 1800, AT + HOUR))
    assert later["slot"] == slot_at and later["last_observed"] == AT + timedelta(minutes=5)


def test_bucket_rows_reference_their_bucket_and_share_their_run(tmp_path):
    run = {"run_key": 4, "product_id": 1, "provider": "alpha", "name": "residential"}
    run |= {"scope": {"target": {"id": "generic_http"}, "config": {"requested_country": "de"}}}
    run |= {"role": "monitor", "test": "pulse", "slot_at": AT, "finished_at": AT + HOUR}
    run |= {"batch_id": 9, "batch_country": None, "batch_region": "eu"}
    buckets = [{"bucket_start": AT + n * timedelta(minutes=5), "run_key": 4} for n in range(3)]
    config, _ = load(lenient_policy(tmp_path))
    found = calculator._group_inputs([run], buckets, (), config, (), 30)
    (rows,) = found.buckets.values()
    assert all(r.bucket is b for r, b in zip(rows, buckets, strict=True))
    assert all(r.run is rows[0].run for r in rows)
    assert {r["slot"] for r in rows} == {AT.replace(minute=0)} and rows[0]["country"] == "DE"
    assert rows[0]["census_batches"] == [] and rows[0]["during_census"] is False


class Source:
    """A geo source that records the products read."""

    def __init__(self, rows):
        self.rows, self.read = rows, []

    def product(self, product_id):
        self.read.append(product_id)
        return [g for g in self.rows if g["product_id"] == product_id]


def test_geo_groups_hold_one_product_at_a_time():
    rows = [
        {"product_id": 1, "scope_id": None, "n": 0},
        {"product_id": 1, "scope_id": 10, "n": 1},
        {"product_id": 1, "scope_id": 11, "n": 2},  # a scope without a group
        {"product_id": 2, "scope_id": None, "n": 3},
    ]
    source = Source(rows)
    groups = GeoGroups(source, {1: (1, "s"), 2: (2, "s")}, {10: (1, "d")})
    assert [g["n"] for g in groups[1, "s"]] == [0]
    assert dict(groups.groups) == {(1, "d"): [rows[1]]}  # the product's other group, kept
    assert [g["n"] for g in groups[1, "d"]] == [1] and source.read == [1]
    assert [g["n"] for g in groups[2, "s"]] == [3] and groups.groups == {}
    assert groups[1, "d"] == [rows[1]] and source.read == [1, 2, 1]  # read again, never stale
    assert groups[3, "x"] == []
    # The full mode's rows, a list: picked by product.
    listed = GeoGroups(rows, {1: (1, "s")}, {10: (1, "d")})
    assert listed[1, "d"] == [rows[1]] and listed[2, "s"] == []


def test_fleet_groups_are_put_back_in_the_groups_order():
    groups = {(1, "a"): {}, (2, "b"): {}, (2, "c"): {}, (1, "c"): {}}
    for (product, cohort), p in groups.items():
        p |= {"product_id": product, "cohort": cohort, "windows": {"24h": {}, "7d": {}}}

    def fill(keys):
        fleet = {}
        for key in keys:
            for label in groups[key]["windows"]:
                fleet.setdefault((key[1], label), []).append((groups[key], label))
        return fleet

    measured = fill([(1, "a"), (1, "c"), (2, "b"), (2, "c")])  # product by product
    assert list(measured) != list(fill(groups))
    assert measured["c", "24h"] != fill(groups)["c", "24h"]
    calculator._in_group_order(measured, groups)
    assert list(measured.items()) == list(fill(groups).items())


@pytest.mark.asyncio
async def test_each_product_reads_its_rows_of_the_full_read(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await fixture(database, tmp_path, end)
    with connect(database) as conn:
        conn.row_factory = dict_row
        products = [r["product_id"] for r in conn.execute("SELECT product_id FROM products")]
        for as_of in (end - 30 * HOUR, end - 29 * HOUR + timedelta(minutes=35)):
            read = scheduled.scheduled_inputs(conn, as_of)
            full = read["geo"]
            geo = rollups.ProductGeo(conn, bounds(as_of), read["overlaps"], "generic_http")
            for product in products:
                found = geo.product(product)
                assert found == [g for g in full if g["product_id"] == product], (as_of, product)
            assert sum(len(geo.product(p)) for p in products) == len(full) > 3
            assert {g["scope_id"] is None for g in full} == {True, False}


@pytest.mark.asyncio
async def test_the_build_reads_geo_rows_per_product(database, tmp_path, monkeypatch):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await fixture(database, tmp_path, end)
    policy, as_of = lenient_policy(tmp_path), end - 30 * HOUR
    config, method_id = load(policy)
    with connect(database) as conn:
        conn.row_factory = dict_row
        read = incremental.read(conn, config, method_id, as_of)
        products = {r["product_id"] for r in read.runs}
    assert type(read.scheduled["geo"]) is rollups.ProductGeo  # no geo row up front
    # The build reads a product's rows outside a batch (scoped()); its census-pulse rows are
    # summed in the database (ProductGeo.sums(), CALC-SCALE-3).
    asked, held, product = [], [], rollups.ProductGeo.scoped

    def spy(geo, product_id):
        asked.append(product_id)
        return product(geo, product_id)

    getitem = inputs.GeoGroups.__getitem__

    def holding(groups, key):
        found = getitem(groups, key)
        held.append({k[0] for k in groups.groups} | {key[0]})
        return found

    monkeypatch.setattr(rollups.ProductGeo, "scoped", spy)
    monkeypatch.setattr(inputs.GeoGroups, "__getitem__", holding)
    assert_equal(database, policy, as_of)
    assert sorted(asked) == sorted(products) and all(len(h) == 1 for h in held), (asked, held)
