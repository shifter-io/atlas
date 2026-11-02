"""ESTIMATE-1: the estimated hours of a daily test, their hourly release and the windows.

The arithmetic (measured x factor) against the committed formulas, the stored rows and their
release against an isolated PostgreSQL, the calculator's `estimated` blocks, the ingest hook and
the hourly calculation trigger. No database outside the test is touched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from psycopg.rows import dict_row
from test_daily import daily_root
from test_onboarding import START
from test_storage import measurement

from proxy_index.calculated import store
from proxy_index.calculated.calculator import calculate as calculate_snapshot
from proxy_index.daily import ingest as daily_ingest
from proxy_index.estimates import compute, release, window
from proxy_index.orchestrator.db import Database, put_setting
from proxy_index.orchestrator.jobs import _batch_skip, _hourly_skip, calculate, register_jobs
from proxy_index.orchestrator.scheduler import Scheduler
from proxy_index.storage import connect, ingest, seed

pytest_plugins = ["test_storage", "test_enrichment_cli"]

FORMULAS = Path("formulas")
POLICY = Path("configs/methodology.toml")
T1 = datetime(2026, 10, 1, 15, 20, tzinfo=UTC)
T2 = T1 + timedelta(days=1)


def formula(version=1, **countries):
    """A small formula: hour factors 1..24 / 24 (none at hour 3), daily factor 3."""
    factors = [(h + 1) / 24 for h in range(24)]
    factors[3] = None
    entry = {"curve": "country", "hour_factors": factors, "daily_factor": 3.0}
    entry["requests"] = [100 * (h + 1) for h in range(24)]
    return {
        "schema": compute.SCHEMA,
        "schema_version": 1,
        "formula_version": version,
        "network_curve": {"requests": [50] * 24},
        "countries": {code: entry | extra for code, extra in countries.items()},
    }


# --- arithmetic ----------------------------------------------------------------------------


@pytest.mark.parametrize("network", sorted(p.stem for p in FORMULAS.glob("*.json")))
def test_every_committed_formula_gives_measured_times_factor_at_its_test_hour(network):
    f = compute.load_formula(FORMULAS, network)
    for code, entry in f["countries"].items():
        if entry["daily_factor"] is None or entry["hour_factors"][entry["test_hour"]] != 1.0:
            continue  # no estimate possible, or a test hour off the peak: covered below
        at = T1.replace(hour=entry["test_hour"], minute=7)
        rows = compute.hours(f, code, at, 200, 1000)
        assert rows[0]["hour"] == entry["test_hour"] and rows[0]["unique_ips"] == 200
        for row in rows:
            assert row["unique_ips"] == pytest.approx(200 * entry["hour_factors"][row["hour"]])
            assert row["day_unique_ips"] == pytest.approx(200 * entry["daily_factor"])
            assert row["curve"] == entry["curve"]
        network = entry["curve"] == "network"
        curve = f["network_curve"]["requests"] if network else entry["requests"]
        if curve[entry["test_hour"]]:  # else requests follow the IP factors
            last = rows[-1]
            expected = 1000 * curve[last["hour"]] / curve[entry["test_hour"]]
            assert last["requests"] == pytest.approx(expected)


def test_hours_roll_into_the_next_day_and_skip_hours_without_a_factor():
    f = formula(DE={}, FR={"curve": "network"})
    rows = compute.hours(f, "DE", datetime(2026, 10, 1, 21, 59, tzinfo=UTC), 30, 600)
    assert [(r["day"].day, r["hour"]) for r in rows[:4]] == [(1, 21), (1, 22), (1, 23), (2, 0)]
    assert len(rows) == 23 and 3 not in {r["hour"] for r in rows}  # hour 3 has no factor
    assert rows[0]["hour_factor"] == 1.0 and rows[0]["requests"] == 600
    assert rows[1]["unique_ips"] == pytest.approx(30 * 23 / 22)
    assert rows[1]["requests"] == pytest.approx(600 * 2300 / 2200)
    assert rows[0]["day_unique_ips"] == pytest.approx(30 * 3.0 / (22 / 24))
    network = compute.hours(f, "FR", datetime(2026, 10, 1, 21, tzinfo=UTC), 30, 600)
    assert {r["requests"] for r in network} == {600} and network[0]["curve"] == "network"
    local = datetime(2026, 10, 2, 1, 30, tzinfo=UTC).astimezone(timezone(timedelta(hours=4)))
    assert compute.hours(f, "DE", local, 1, 1)[0]["hour"] == 1  # UTC hours, whatever the zone
    with pytest.raises(ValueError, match="hour 3 has no factor"):
        compute.hours(f, "DE", datetime(2026, 10, 1, 3, tzinfo=UTC), 1, 1)
    with pytest.raises(ValueError, match="not in the formula"):
        compute.hours(f, "XX", T1, 1, 1)


def test_only_a_version_1_hour_formula_loads(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"schema": compute.SCHEMA, "schema_version": 2}))
    with pytest.raises(ValueError, match="not a version 1"):
        compute.load_formula(tmp_path, "a")


def rows_of(run_key, product=1, country="DE", measured=2, day_unique=10.0, version=1):
    return {
        "run_key": run_key,
        "product_id": product,
        "country_code": country,
        "measured_ips": measured,
        "day_unique_ips": day_unique,
        "unique_ips": measured / 2,
        "requests": 7.0,
        "formula_version": version,
        "hour_start": T1 + timedelta(hours=run_key),
    }


def test_window_unique_ips_count_an_ip_seen_on_several_days_once():
    # Two days of tests of the same 2 IPs: the sum counts them twice, distinct once.
    rows = [rows_of(1), rows_of(1), rows_of(2)]
    assert window.window_unique("sum", rows, 2) == 20.0
    assert window.window_unique("distinct", rows, 2) == 10.0
    assert window.window_unique("distinct", rows, 3) == 15.0  # one new IP on day 2
    assert window.window_unique("distinct", [rows_of(1, measured=0)], 0) == 0.0
    with pytest.raises(ValueError, match="window_unique"):
        window.window_unique("average", rows, 2)


def product_window(start):
    return {
        "period": {"start": start},
        "pool": {"unique_ips": 4, "datacenter_asn_share": 0.25},
        "overlap": {"shared_share": 0.5},
    }


def test_attach_flags_every_estimated_figure_with_its_formula_version():
    groups = {
        (1, "c"): {
            "product_id": 1,
            "windows": {"24h": product_window(T1 + timedelta(hours=2)), "7d": product_window(T1)},
        }
    }
    hours = [rows_of(1, version=1), rows_of(2, version=2), rows_of(3, country="FR")]
    estimates = {
        "hours": hours + [rows_of(9, product=2)],  # a run outside the calculation: ignored
        "distinct": {"7d": [{"product_id": 1, "country_code": None, "ips": 2}]},
    }
    run_map = {key: {"group": (1, "c")} for key in (1, 2, 3)}
    window.attach(groups, run_map, estimates, {"estimates": {"window_unique": "sum"}})
    week = groups[(1, "c")]["windows"]["7d"]["estimated"]
    day = groups[(1, "c")]["windows"]["24h"]["estimated"]
    assert week["estimated"] is True and week["formula_versions"] == [1, 2] and week["hours"] == 3
    assert week["unique_ips"] == {
        "value": 30.0,
        "estimated": True,
        "formula_version": 2,
        "method": "sum",
    }
    assert week["requests"]["estimated"] and week["requests"]["value"] == 21.0
    assert [(c["country_code"], c["formula_version"]) for c in week["countries"]] == [
        ("DE", 2),
        ("FR", 1),
    ]
    assert all(point["estimated"] for point in week["series"]) and len(week["series"]) == 3
    assert week["breakdown_scale"] == 30.0 / 4
    assert week["carried"] == {
        "shared_share": 0.5,
        "datacenter_asn_share": 0.25,
        "latency": "measured",
    }
    assert day["hours"] == 2  # the 24h window starts at the second hour
    groups[(1, "c")]["windows"]["24h"]["period"]["start"] = T2
    del groups[(1, "c")]["windows"]["24h"]["estimated"]
    window.attach(groups, run_map, estimates, {})
    assert "estimated" not in groups[(1, "c")]["windows"]["24h"]
    assert groups[(1, "c")]["windows"]["7d"]["estimated"]["unique_ips"]["method"] == "distinct"
    window.attach(groups, run_map, None, {})  # nothing released: no change


# --- stored rows and release ---------------------------------------------------------------


async def imported(tmp_path, database, started_at=None):
    """One imported fixture run (1 IP, 3 requests) measured at started_at; returns its id."""
    directory = await measurement(tmp_path / f"run{len(list(tmp_path.glob('run*')))}")
    ingest(directory, database)
    run_id = json.loads((directory / "summary.json").read_text())["run_id"]
    if started_at:
        with connect(database) as conn:
            conn.execute("UPDATE test_runs SET started_at=%s WHERE run_id=%s", (started_at, run_id))
    return run_id


def stored(database):
    with connect(database) as conn:
        conn.row_factory = dict_row
        return conn.execute(
            "SELECT day,hour,unique_ips,requests,day_unique_ips,formula_version,released_at,"
            "measured_at FROM estimated_hours ORDER BY day,hour"
        ).fetchall()


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()


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
            "SELECT data->>'hour' FROM test_events WHERE kind='estimate_hour_released' ORDER BY id"
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
        compute.estimate_run(database, "no-such-run", "DE", formula(DE={}))


async def test_week_unique_ips_are_distinct_measured_ips_not_a_sum_of_days(tmp_path, database, db):
    seed(database)
    for at in (T1, T2):
        compute.estimate_run(database, await imported(tmp_path, database, at), "DE", formula(DE={}))
    await release.release_due(db, T2 + timedelta(days=1))
    with connect(database) as conn:
        conn.row_factory = dict_row
        fetched = window.fetch(conn, T2 + timedelta(days=1))
    week = [d for d in fetched["distinct"]["7d"] if d["country_code"] is None]
    assert [d["ips"] for d in week] == [1]  # the same IP both days
    rows = fetched["hours"]
    day_unique = 3.0 / (16 / 24)
    assert window.window_unique("distinct", rows, 1) == pytest.approx(day_unique)
    assert window.window_unique("sum", rows, 1) == pytest.approx(2 * day_unique)
    assert all(isinstance(r["day"], str) for r in rows)  # JSON-ready for the fingerprint


async def test_the_snapshot_carries_released_estimates_with_their_flags(tmp_path, database, db):
    store.initialize(database)
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
    assert estimated["series"][0]["start"] == hour and estimated["series"][0]["estimated"]
    assert measured["pool"]["unique_ips"] == 1  # the measured figures are unchanged


# --- ingest hook and orchestrator ----------------------------------------------------------


async def test_ingest_stores_each_imported_countrys_hours_and_releases_them(
    tmp_path, monkeypatch, database, fake_database
):
    import proxy_index.runner as runner

    monkeypatch.setattr(runner.time, "time", lambda: START.timestamp() + 1)
    seed(database)
    root = await daily_root(tmp_path)
    folder = tmp_path / "formulas"
    folder.mkdir()
    (folder / "shifter.json").write_text(json.dumps(formula(CA={})))  # PM has no curve
    mmdb, _ = fake_database
    db = Database(str(database), size=1)
    try:
        report = await daily_ingest.ingest_campaign(
            root, database, mmdb, database=db, formulas=folder
        )
    finally:
        await db.close()
    tor1 = report["cells"]["shifter/tor1"]
    assert tor1["status"] == "done" and tor1["estimated"] == ["CA"]
    assert tor1["estimate_issues"] == ["PM: no estimates (ValueError)"]
    # The test ran at START, in the future: none of its hours has ended yet.
    assert report["complete"] is False and report["released_hours"] == 0
    assert {r["formula_version"] for r in stored(database)} == {1}


def test_ingest_reports_a_network_without_a_formula(tmp_path):
    formula_dir = tmp_path / "none"
    formula_dir.mkdir()
    assert daily_ingest._formula(formula_dir, "shifter")[1] == [
        "shifter: no formula (FileNotFoundError)"
    ]
    assert daily_ingest._formula(None, "shifter") == (None, [])


def test_the_hourly_trigger_runs_once_per_hour_after_the_release():
    offset = timedelta(seconds=120)
    now = datetime(2026, 10, 1, 12, 2, 30, tzinfo=UTC)
    assert _hourly_skip({"last": None}, now, offset) is None
    assert _hourly_skip({"last": now - timedelta(minutes=3)}, now, offset) is None
    assert _hourly_skip({"last": now - timedelta(seconds=10)}, now, offset).startswith("already")
    early = datetime(2026, 10, 1, 12, 1, tzinfo=UTC)  # before 12:02: the 11:02 slot
    assert _hourly_skip({"last": early - timedelta(minutes=50)}, early, offset) is not None
    assert _hourly_skip({"last": early - timedelta(minutes=70)}, early, offset) is None
    finished = {"last": now - timedelta(minutes=10), "finished": now - timedelta(minutes=1)}
    assert _batch_skip(finished, now, timedelta(seconds=300)) is None
    assert _batch_skip(finished, now, timedelta(hours=1)) == "debounced"


async def test_calculate_follows_the_trigger_setting(database, db, monkeypatch):
    import sys
    import types

    module = types.ModuleType("proxy_index.calculated.calculator")
    module.calculate = lambda *args, publish: {"snapshot_id": "s1", "as_of": "now"}
    monkeypatch.setitem(sys.modules, "proxy_index.calculated.calculator", module)
    assert await calculate(db, database, "m.toml") == {"snapshot_id": "s1", "as_of": "now"}
    with connect(database) as conn:
        conn.execute("INSERT INTO job_runs(job,status,started_at) VALUES ('calculate','ok',now())")
    assert (await calculate(db, database, "m.toml"))["skipped"].startswith("already")
    async with db.connection() as conn:
        await put_setting(conn, "calculate.trigger", "every second")
    with pytest.raises(ValueError, match="calculate.trigger"):
        await calculate(db, database, "m.toml")


async def test_the_release_job_is_scheduled(database, db):
    scheduler = Scheduler(db)
    register_jobs(scheduler, db, str(database))
    assert scheduler.tasks["estimates_release"].every_s == 60
    assert await scheduler.run_now("estimates_release") == {"released": 0}
