"""ESTIMATE-1: the ingest hook that stores each imported country's hours and the orchestrator's
release job and hourly calculation trigger."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from daily_cases import daily_root
from estimates_cases import POLICY, formula, imported, stored
from test_onboarding import START

from atlas.calculated.calculator import calculate as calculate_snapshot
from atlas.campaign.contracts import spec_path
from atlas.daily import ingest as daily_ingest
from atlas.estimates import compute, release
from atlas.orchestrator.db import DEFAULTS, Database, put_setting
from atlas.orchestrator.jobs import (
    _batch_skip,
    _calculate_skip,
    _hourly_skip,
    calculate,
    register_jobs,
)
from atlas.orchestrator.scheduler import Scheduler
from atlas.storage import connect, seed

pytest_plugins = ["test_storage", "calculated_template", "test_enrichment_cli", "estimates_cases"]


@pytest.fixture
def database(calculated_database):
    """Every test here works on the current calculated schema, copied from its template."""
    return calculated_database


async def test_ingest_stores_each_imported_countrys_hours_and_releases_them(
    tmp_path, monkeypatch, database, fake_database
):
    import atlas.runner as runner

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


async def test_a_late_run_counts_for_its_campaigns_hour(
    tmp_path, monkeypatch, database, fake_database
):
    # ESTIMATE-SCHED-1 (#1152): the campaign was due two hours before its run started (a
    # catch-up, or a country that waited on its droplet); its hours still start at the
    # campaign's slot, and the run keeps its real start.
    import atlas.runner as runner

    monkeypatch.setattr(runner.time, "time", lambda: START.timestamp() + 1)
    seed(database)
    root = await daily_root(tmp_path)
    due = START.astimezone(UTC) - timedelta(hours=2)
    spec = json.loads(spec_path(root).read_text())
    spec_path(root).write_text(json.dumps(spec | {"starts_at": due.isoformat()}))
    folder = tmp_path / "formulas"
    folder.mkdir()
    (folder / "shifter.json").write_text(json.dumps(formula(CA={"hour_factors": [1.0] * 24})))
    mmdb, _ = fake_database
    db = Database(str(database), size=1)
    try:
        await daily_ingest.ingest_campaign(root, database, mmdb, database=db, formulas=folder)
    finally:
        await db.close()
    rows = stored(database)
    assert (rows[0]["day"], rows[0]["hour"]) == (due.date(), due.hour) and len(rows) == 24
    assert rows[0]["measured_at"].astimezone(UTC).hour == START.astimezone(UTC).hour


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


async def test_the_hourly_calculation_includes_the_hour_just_released(tmp_path, database, db):
    # The real cadence: the release job runs every minute, so hour H is released by about
    # H+1:00:30; the hourly calculation runs at the default offset and floors as_of to 5 minutes.
    run_id = await imported(tmp_path, database)
    with connect(database) as conn:
        started = conn.execute(
            "SELECT started_at FROM test_runs WHERE run_id=%s", (run_id,)
        ).fetchone()[0]
    hour = started.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    compute.estimate_run(database, run_id, "DE", formula(DE={"hour_factors": [1.0] * 24}))
    await release.release_due(db, hour + timedelta(hours=1, seconds=30))
    offset = timedelta(seconds=DEFAULTS["calculate.hour_offset_seconds"])
    previous = {"last": hour + offset}  # the calculation of hour H's own slot
    minutes = (hour + timedelta(hours=1, minutes=m) for m in range(61))
    now = next(at for at in minutes if _hourly_skip(previous, at, offset) is None)
    windows = calculate_snapshot(database, POLICY, as_of=now)["products"][0]["windows"]
    assert "estimated" in windows["24h"], f"hour {hour} released but not in the {now} calculation"
    assert windows["24h"]["estimated"]["series"][0]["start"] == hour.isoformat()


async def test_an_hourly_offset_too_short_for_the_release_is_raised(database, db, caplog):
    hour = datetime(2026, 10, 1, 12, tzinfo=UTC)
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO job_runs(job,status,started_at) VALUES ('calculate','ok',%s)",
            (hour - timedelta(minutes=53),),
        )
    async with db.connection() as conn:
        await put_setting(conn, "calculate.hour_offset_seconds", 120)
        # 120 s would be due at 12:03; raised to 360 s the calculation waits for 12:06.
        assert (await _calculate_skip(conn, hour + timedelta(minutes=3))).startswith("already")
        assert await _calculate_skip(conn, hour + timedelta(minutes=6)) is None
    assert "would leave out the hour just released" in caplog.text


async def test_calculate_follows_the_trigger_setting(database, db, monkeypatch):
    from atlas.orchestrator import jobs

    async def run_calculation(argv, limit_s):  # the subprocess (test_manager_orch_serve runs it)
        return {"snapshot_id": "s1", "as_of": "now"}

    monkeypatch.setattr(jobs, "run_calculation", run_calculation)
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
