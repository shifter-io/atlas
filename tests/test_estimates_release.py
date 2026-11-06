"""ESTIMATE-1: the stored estimated hours against an isolated PostgreSQL, their hourly release,
the windows' distinct IPs, the snapshot's `estimated` blocks and retention."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, timedelta

import pytest
from census_pulse_cases import product
from estimates_cases import POLICY, T1, T2, formula, imported, product_window, stored
from psycopg.rows import dict_row

from proxy_index.calculated.calculator import calculate as calculate_snapshot
from proxy_index.detect import list_events
from proxy_index.estimates import compute, release, window
from proxy_index.orchestrator import events as tape_events
from proxy_index.orchestrator.db import DEFAULTS
from proxy_index.orchestrator.jobs import retention
from proxy_index.storage import connect, seed

pytest_plugins = ["test_storage", "calculated_template", "estimates_cases"]


@pytest.fixture
def database(calculated_database):
    """Every test here works on the current calculated schema, copied from its template."""
    return calculated_database


async def test_stored_hours_are_released_in_order_and_never_rewritten(tmp_path, database, db):
    seed(database)
    first = await imported(tmp_path, database, T1)
    assert compute.estimate_run(database, first, "DE", formula(DE={})) == 23
    rows = stored(database)
    assert rows[0]["hour"] == 15 and rows[0]["unique_ips"] == 1 and rows[0]["requests"] == 3
    assert all(r["day_unique_ips"] == pytest.approx(3.0 / (16 / 24)) for r in rows)
    assert all(r["released_at"] is None for r in rows)
    assert await release.release_due(db, T1 + timedelta(minutes=30)) == []  # hour 15 not ended
    due = await release.release_due(db, T1.replace(hour=18, minute=0))
    assert [(e.day.day, e.hour, e.formula_version, e.unique_ips) for e in due] == [
        (1, 15, "1", 1),
        (1, 16, "1", 1),
        (1, 17, "1", 1),
    ]
    assert await release.release_due(db, T1.replace(hour=18, minute=30)) == []
    with connect(database) as conn:
        events = conn.execute(
            "SELECT data->>'hour' FROM test_events WHERE kind='estimate_hour_released'"
            " ORDER BY event_id"
        ).fetchall()
    assert [int(e[0]) for e in events] == [15, 16, 17]
    # A new formula version rewrites only the unreleased hours.
    assert compute.estimate_run(database, first, "DE", formula(2, DE={})) == 20
    versions = {(r["hour"], r["formula_version"]) for r in stored(database)}
    assert (15, 1) in versions and (18, 2) in versions
    # An older test imported late never replaces the hours of a newer one.
    older = await imported(tmp_path, database, T1 - timedelta(hours=1))
    assert compute.estimate_run(database, older, "DE", formula(3, DE={})) == 1  # only hour 14
    assert {r["formula_version"] for r in stored(database) if r["hour"] == 20} == {2}
    with pytest.raises(ValueError, match="not imported"):
        compute.estimate_run(database, str(uuid.uuid4()), "DE", formula(DE={}))


COUNTRIES = [a + b for a in "ABCDEFGH" for b in "VWXYZ"]  # 40 codes


async def test_a_full_hour_writes_one_release_event_per_network_not_per_country(
    tmp_path, database, db
):
    """TAPE-2: 18 networks x 40 countries released for one hour are 18 tape events, each with
    its country count, and a pulse_down written beside them stays on the first page."""
    seed(database)
    run_key_sql = "SELECT run_key FROM test_runs WHERE run_id=%s"
    with connect(database) as conn:
        run_key = conn.execute(run_key_sql, (await imported(tmp_path, database, T1),)).fetchone()[0]
        networks = [product(conn, f"net{n:02d}") for n in range(18)]
        conn.cursor().executemany(
            "INSERT INTO estimated_hours(product_id,country_code,day,hour,run_key,measured_at,"
            "measured_ips,measured_requests,hour_factor,unique_ips,requests,day_unique_ips,curve,"
            "formula_version) VALUES (%s,%s,%s,15,%s,%s,1,1,1,2.5,1,1,'country',1)",
            [(p, c, T1.date(), run_key, T1) for p in networks for c in COUNTRIES],
        )
    due = await release.release_due(db, T1.replace(hour=16, minute=0))
    assert len(due) == 18 and {(e.countries, e.unique_ips, e.hour) for e in due} == {(40, 100, 15)}
    async with db.transaction() as conn:
        await tape_events.emit_pulse_down(
            conn, T1.replace(hour=16, minute=1), pulse_window(networks[0])
        )
    with connect(database) as conn:
        listed = list_events(conn)
    assert [e["kind"] for e in listed].count("estimate_hour_released") == 18
    assert listed[0]["kind"] == "pulse_down" and len(listed) == 19
    release_data = {(e["product"], json.dumps(e["data"], sort_keys=True)) for e in listed[1:]}
    assert len(release_data) == 18
    assert all('"countries": 40' in data for _, data in release_data)


def pulse_window(product_id):
    return tape_events.PulseWindow(
        product_id=product_id,
        window_start=T1.replace(hour=15, minute=55),
        window_minutes=5,
        errors=640,
        requests=800,
        regions=8,
        threshold=0.5,
    )


async def test_week_unique_ips_are_distinct_measured_ips_not_a_sum_of_days(tmp_path, database, db):
    seed(database)
    for at in (T1, T2):
        compute.estimate_run(database, await imported(tmp_path, database, at), "DE", formula(DE={}))
    await release.release_due(db, T2 + timedelta(days=1))
    with connect(database) as conn:
        conn.row_factory = dict_row
        rows = window.fetch(conn, T2 + timedelta(days=1))
        groups = {(1, "c"): {"windows": {"7d": product_window(T1 - timedelta(days=1))}}}
        run_group = dict.fromkeys({r["run_key"] for r in rows}, (1, "c"))
        found = window.windows_with(groups, run_group, rows)
        assert window.distinct(conn, found) == [{"DE": 1, None: 1}]  # the same IP both days
    day_unique = 3.0 / (16 / 24)
    assert window.window_unique("distinct", rows, 1) == pytest.approx(day_unique)
    assert window.window_unique("sum", rows, 1) == pytest.approx(2 * day_unique)
    assert all(isinstance(r["day"], str) for r in rows)  # JSON-ready for the fingerprint


async def test_the_snapshot_carries_released_estimates_with_their_flags(tmp_path, database, db):
    run_id = await imported(tmp_path, database)
    with connect(database) as conn:
        started = conn.execute(
            "SELECT started_at FROM test_runs WHERE run_id=%s", (run_id,)
        ).fetchone()[0]
    hour = started.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    f = formula(DE={"hour_factors": [1.0] * 24})
    compute.estimate_run(database, run_id, "DE", f)
    as_of = hour + timedelta(hours=1, minutes=5)
    unreleased = calculate_snapshot(database, POLICY, as_of=as_of)
    assert "estimated" not in unreleased["products"][0]["windows"]["24h"]
    await release.release_due(db, hour + timedelta(hours=1))
    snapshot = calculate_snapshot(database, POLICY, as_of=as_of)
    measured = snapshot["products"][0]["windows"]["24h"]
    estimated = measured["estimated"]
    assert estimated["estimated"] is True and estimated["hours"] == 1
    assert estimated["unique_ips"]["formula_version"] == 1
    assert estimated["unique_ips"]["value"] == pytest.approx(3.0)  # 1 IP x daily factor 3
    assert (
        estimated["series"][0]["start"] == hour.isoformat() and estimated["series"][0]["estimated"]
    )
    assert measured["pool"]["unique_ips"] == 1  # the measured figures are unchanged


async def test_distinct_ips_count_only_each_windows_own_tests(tmp_path, database, db):
    # One product's tests in two cohorts; the second test also saw a second IP. Each cohort's
    # window counts its own test's IPs, never the union of both.
    seed(database)
    runs = [await imported(tmp_path, database, at) for at in (T1, T2)]
    for run_id in runs:
        compute.estimate_run(database, run_id, "DE", formula(DE={}))
    await release.release_due(db, T2 + timedelta(days=1))
    with connect(database) as conn:
        conn.row_factory = dict_row
        conn.execute(
            "WITH ip AS (INSERT INTO ip_addresses(ip) VALUES ('192.0.2.77') RETURNING ip_id) "
            "INSERT INTO ip_observations(bucket_start,run_key,ip_id,count,first_seen,last_seen) "
            "SELECT o.bucket_start,o.run_key,ip.ip_id,1,o.first_seen,o.last_seen "
            "FROM ip_observations o JOIN test_runs r USING(run_key), ip WHERE r.run_id=%s LIMIT 1",
            (runs[1],),
        )
        rows = window.fetch(conn, T2 + timedelta(days=1))
        keys = sorted({r["run_key"] for r in rows})
        groups = {
            (1, cohort): {"windows": {"30d": product_window(T1 - timedelta(days=1))}}
            for cohort in "ab"
        }
        run_group = dict(zip(keys, [(1, "a"), (1, "b")], strict=True))
        found = window.windows_with(groups, run_group, rows)
        assert window.distinct(conn, found) == [{"DE": 1, None: 1}, {"DE": 2, None: 2}]
        assert window.distinct(conn, []) == []


async def test_retention_deletes_estimated_hours_past_the_longest_window(tmp_path, database, db):
    seed(database)
    compute.estimate_run(database, await imported(tmp_path, database, T1), "DE", formula(DE={}))
    # The default keeps 35 days: the longest window (30) and the days until the raw archive
    # takes the run's day (ARCHIVE-1).
    assert DEFAULTS["retention.estimates_days"] == 35
    result = await retention(db, T2 + timedelta(days=35))  # keeps day 2 onwards
    assert result["estimated_hours_deleted"] == 9  # day 1, hours 15..23
    assert {r["day"].day for r in stored(database)} == {2}
