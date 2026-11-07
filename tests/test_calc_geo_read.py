"""CALC-MEM-1: the incremental calculation reads its census-pulse geo rows aggregated in SQL (one
row per geo key, its bins in arrays) through a server-side cursor, keeps them as tuples
(scheduled.GeoRow), and its fingerprint hashes the window's geo rollup marks instead of the rows.

On test_calc_rollups' 3-day fixture: the streamed read in small batches equals the full mode's
rows; the fingerprint hashes the marks, stays the same when nothing changed (the calculation
reuses its build), and changes with a late run in an hour rolled up already, whose mark rises.
"""

from array import array
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from calculated_cases import lenient_policy
from psycopg.rows import dict_row
from test_calc_rollups import fixture, seed_geo
from test_calculated_hourly_reads import HOUR, floor_hour

from proxy_index.calculated import incremental, rollups, scheduled
from proxy_index.calculated.calculator import calculate
from proxy_index.calculated.policy import load
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]


def test_a_geo_row_reads_like_the_dict_it_replaces():
    place, network = ("DE", "Germany"), (3320, "DTAG", "isp")
    bins = array("q", [20, 40, 3, 5])  # the ms, then the counts
    row = scheduled.GeoRow((None, 7, 24, place, network, None, 9, 8, 1, 0, bins))
    assert (row["product_id"], row["hours"], row["location"]) == (7, 24, place)
    assert row["network"] == network and row["scope_id"] is None
    assert (row["completed"], row["successes"], row["challenges"], row["blocks"]) == (9, 8, 1, 0)
    assert row["histogram"] == [(20, 3), (40, 5)] and row[1] == 7
    assert row.get("fallback") is None and row.get("missing", "x") == "x"
    assert row.plain() == [None, 7, 24, place, network, None, 9, 8, 1, 0, [(20, 3), (40, 5)]]
    empty = scheduled.GeoRow((*row[:-1], array("q")))
    assert empty["histogram"] == [] and empty != row


@pytest.mark.asyncio
async def test_the_geo_read_streams_one_row_per_key(database, tmp_path, monkeypatch):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await fixture(database, tmp_path, end)
    streamed, iterate = [], psycopg.ServerCursor.__iter__

    def spy(cursor):
        streamed.append((cursor.name, cursor.itersize))
        return iterate(cursor)

    monkeypatch.setattr(psycopg.ServerCursor, "__iter__", spy)
    monkeypatch.setattr(rollups, "STREAM", 3)  # several batches
    with connect(database) as conn:
        conn.row_factory = dict_row
        for as_of in (end - 30 * HOUR, end - 29 * HOUR + timedelta(minutes=35)):
            full = scheduled.scheduled_inputs(conn, as_of)["geo"]
            found = rollups.scheduled_inputs(conn, as_of, "generic_http")["geo"]
            assert found == full and all(type(g) is scheduled.GeoRow for g in found)
            assert len(found) > 3 and any(g["histogram"] for g in found)
    assert ("geo_rows", 3) in streamed


def fingerprint(database, policy, as_of, monkeypatch):
    """The incremental fingerprint at as_of, the geo inputs it hashed and the window's marks."""
    hashed, digest = [], incremental.digest
    monkeypatch.setattr(incremental, "digest", lambda value: hashed.append(value) or digest(value))
    config, method_id = load(policy)
    with connect(database) as conn:
        conn.row_factory = dict_row
        inputs = incremental.read(conn, config, method_id, as_of)
        marks = rollups.marks(conn, as_of)
    return inputs.fingerprint, hashed[-1]["scheduled"]["geo"], dict(marks)


@pytest.mark.asyncio
async def test_the_fingerprint_hashes_the_rollup_marks(database, tmp_path, monkeypatch):
    end = floor_hour(datetime.now(UTC)) + HOUR
    h, rng, _ = await fixture(database, tmp_path, end)
    policy, as_of = lenient_policy(tmp_path), end - 30 * HOUR
    first, geo, marks = fingerprint(database, policy, as_of, monkeypatch)
    assert len(marks) == 720
    assert geo == [[at.astimezone(UTC).isoformat(), run] for at, run in marks.items()]
    # Nothing changed: the same fingerprint, and the calculation reuses its build.
    assert fingerprint(database, policy, as_of, monkeypatch)[0] == first
    built = calculate(database, policy, as_of=as_of, mode="incremental")
    again = calculate(database, policy, as_of=as_of, mode="incremental")
    assert again["snapshot_id"] == built["snapshot_id"]
    # A late run in an hour rolled up already raises its mark and changes the fingerprint.
    late = await h.scheduled("pulse", "beta", 60 * HOUR)
    with connect(database) as conn:
        seed_geo(conn, rng, late)
    later, _, moved = fingerprint(database, policy, as_of, monkeypatch)
    hour = end - 60 * HOUR  # the hour its buckets fall in (test_calc_rollups)
    assert later != first and moved[hour] >= late > marks[hour], (hour, late)
    rebuilt = calculate(database, policy, as_of=as_of, mode="incremental")
    assert rebuilt["snapshot_id"] != built["snapshot_id"]
