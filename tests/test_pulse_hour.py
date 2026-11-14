"""PULSE-ONDEMAND-1: the hour's on-demand pulse droplets (daily.pulses, `daily pulse-plan`) and
the orchestrator's opening of their pulses (orchestrator/pulse_hour.py). No droplet or request
outside the test is touched; the database is the test's own."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta

import pytest
from daily_cases import formulas
from orchestrator_cases import FRA1, prepare, query, set_setting
from pulse_fleet_cases import make_root

from atlas.cli import main
from atlas.daily.planner import Cell, load_schedules
from atlas.daily.pulses import DEFAULT_CAP, CapError, check_cap, pulse_hour
from atlas.orchestrator import batches
from atlas.orchestrator.nodes import issue_token
from atlas.orchestrator.pulse_hour import Armed, chain, open_hour, window
from atlas.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]

HOUR = datetime(2026, 10, 1, 12, tzinfo=UTC)


# --- the hour's droplets -------------------------------------------------------------------


def test_every_country_of_a_network_is_due_every_hour_one_droplet_per_region(tmp_path):
    plan = pulse_hour(load_schedules(formulas(tmp_path)), 9)
    assert plan.hour == 9
    assert plan.cells == (
        Cell("geonode", "lon1", ("GB", "FR")),
        Cell("geonode", "sgp1", ("SG",)),
        Cell("shifter", "fra1", ("DE",)),
        Cell("shifter", "nyc3", ("US",)),
        Cell("shifter", "tor1", ("CA", "PM")),
    )
    only = pulse_hour(load_schedules(formulas(tmp_path)), 9, networks=("shifter",))
    assert only.networks() == ("shifter",) and only.skipped == ("geonode",)


def test_the_cap_refuses_an_hour_over_it_with_the_droplets_still_up(tmp_path):
    plan = pulse_hour(load_schedules(formulas(tmp_path)), 0)  # 5 droplets
    check_cap(plan, 5)
    check_cap(plan)  # DEFAULT_CAP
    assert DEFAULT_CAP == 144
    with pytest.raises(CapError, match="needs 5 pulse droplets and 1 are still up"):
        check_cap(plan, 5, running=1)
    with pytest.raises(CapError, match="over the cap of 4"):
        check_cap(plan, 4)
    for bad in (0, 501, True, 2.5):
        with pytest.raises(CapError, match="1..500"):
            check_cap(plan, bad)


def test_pulse_plan_cli_prints_the_droplets_writes_nothing_and_exits_3_over_the_cap(
    tmp_path, capsys
):
    folder = formulas(tmp_path)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    args = ["daily", "pulse-plan", "--formulas", str(folder), "--start", "2027-01-01T05:00:00Z"]
    assert main(args) == 0
    plan = json.loads(capsys.readouterr().out)
    assert (plan["droplets"], plan["cap"], plan["hour"]) == (5, 144, 5)
    assert plan["starts_at"].startswith("2027-01-01T05") and "refused" not in plan
    assert main(args + ["--max-droplets", "3"]) == 3
    refused = json.loads(capsys.readouterr().out)
    assert refused["droplets"] == 5 and "over the cap of 3" in refused["refused"]
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_pulse_plan_with_a_root_refuses_a_snapshot_name_before_the_hour_does(tmp_path, capsys):
    folder = formulas(tmp_path)
    root = make_root(tmp_path, image_by_region={"lon1": "atlas-camp-worker-1.0-0123456789ab"})
    args = ["daily", "pulse-plan", "--root", str(root), "--formulas", str(folder)]
    assert main(args + ["--start", "2027-01-01T05:00:00Z"]) == 3
    refused = json.loads(capsys.readouterr().out)["refused"]
    assert "image_by_region lon1: a snapshot id (digits), not a name" in refused
    (root / "fleet.json").write_text(
        (root / "fleet.json").read_text().replace("atlas-camp-worker-1.0-0123456789ab", "123")
    )
    assert main(args + ["--start", "2027-01-01T05:00:00Z"]) == 0
    plan = json.loads(capsys.readouterr().out)  # the fleet's networks only, as the hour plans
    assert plan["networks"] == ["shifter"] and "refused" not in plan


# --- the orchestrator opens the hour ------------------------------------------------------


def test_the_window_ends_at_the_last_start_18_minutes_past_the_hour():
    assert window(HOUR, HOUR + timedelta(minutes=3)) == timedelta(minutes=15)
    assert window(HOUR, HOUR + timedelta(minutes=18)) == timedelta(0)
    assert window(HOUR, HOUR + timedelta(minutes=30)) == timedelta(0)


def test_chain_runs_each_regions_countries_back_to_back_one_step_apart():
    """PULSE-COMPACT-1: no spacing over the hour; a region's countries one step apart."""
    base, step = HOUR + timedelta(seconds=15), timedelta(seconds=12)
    countries = [("FR", 1), ("DE", 2), ("GB", 1), ("AT", 2), ("CH", 2)]
    # Half a step in (the second target); DE at 2.5 steps (30 s) is past a 30 s span.
    assert chain(countries, base, step, 0.5, timedelta(seconds=30)) == {
        ("FR", 1): base + step / 2,
        ("GB", 1): base + step * 1.5,
        ("AT", 2): base + step / 2,
        ("CH", 2): base + step * 1.5,
    }
    # The largest droplet (57 countries): every start within about 11 minutes of the first.
    many = [(f"{chr(65 + i // 26)}{chr(65 + i % 26)}", 3) for i in range(57)]
    starts = chain(many, base, step, 0.0, window(HOUR, base))
    assert len(starts) == 57 and max(starts.values()) - base == step * 56


def hourly(database):
    """The HTTP targets on hourly pulses (the seed keeps configs/targets.toml's 30 minutes)."""
    with connect(database) as conn:
        conn.execute("UPDATE targets SET pulse_interval_minutes=60 WHERE engine='http'")


def _runs(database):
    return query(
        database,
        "SELECT b.target_id,b.country_code,p.provider,r.status FROM batch_runs r "
        "JOIN test_batches b USING(batch_id) JOIN products p USING(product_id) ORDER BY 1,2,3",
    )


async def test_open_hour_packs_the_armed_cells_pulses_and_runs_only_their_products(database, db):
    prepare(database, ("DE", "AT", "GB"))
    hourly(database)
    await set_setting(db, "pulse.on_demand", True)
    for provider in ("alpha", "beta"):
        issue_token(database, provider=provider, product="res", region="fra1")
    issue_token(database, provider="gamma", product="res", region="lon1")  # no droplet now
    cells = [Armed("alpha", "res", "fra1", ("AT", "DE")), Armed("beta", "res", "fra1", ("DE",))]
    now = HOUR + timedelta(minutes=3)

    result = await open_hour(db, HOUR, cells, now)
    assert result == {
        "targets": ["generic_http", "generic_https"],
        "created": 4,
        "runs": 6,
        "not_hourly": [],
    }
    begin = now + timedelta(seconds=15)  # pulse.lead_seconds
    starts = query(
        database,
        "SELECT target_id,country_code,region_id,scheduled_for,start_at FROM test_batches "
        "WHERE kind='pulse' ORDER BY start_at",
    )
    # Back to back: 12 s apart (pulse.step_seconds), generic_https half a step after.
    assert starts == [
        ("generic_http", "AT", FRA1, HOUR, begin),
        ("generic_https", "AT", FRA1, HOUR, begin + timedelta(seconds=6)),
        ("generic_http", "DE", FRA1, HOUR, begin + timedelta(seconds=12)),
        ("generic_https", "DE", FRA1, HOUR, begin + timedelta(seconds=18)),
    ]
    assert _runs(database) == [
        ("generic_http", "AT", "alpha", "queued"),
        ("generic_http", "DE", "alpha", "queued"),
        ("generic_http", "DE", "beta", "queued"),
        ("generic_https", "AT", "alpha", "queued"),
        ("generic_https", "DE", "alpha", "queued"),
        ("generic_https", "DE", "beta", "queued"),
    ]
    # No droplet, no run: gamma gets no no_node failure, alert or event.
    assert query(database, "SELECT 1 FROM alerts") == []
    assert query(database, "SELECT 1 FROM test_events") == []
    # A second call for the hour adds nothing.
    again = await open_hour(db, HOUR, cells, now + timedelta(minutes=1))
    assert (again["created"], again["runs"]) == (0, 0)
    assert len(_runs(database)) == 6
    # A cell armed later gets its runs in the batches still to start, and its new country's;
    # a closed batch (AT https) or one already due (AT http, at `begin`) takes none.
    with connect(database) as conn:
        conn.execute(
            "UPDATE test_batches SET status='done' WHERE country_code='AT' "
            "AND target_id='generic_https'"
        )
    later = [Armed("beta", "res", "fra1", ("AT", "DE")), Armed("gamma", "res", "lon1", ("GB",))]
    added = await open_hour(db, HOUR, later, now + timedelta(seconds=20))
    assert (added["created"], added["runs"]) == (2, 2)
    assert [r for r in _runs(database) if r[2] != "alpha"] == [
        ("generic_http", "DE", "beta", "queued"),
        ("generic_http", "GB", "gamma", "queued"),
        ("generic_https", "DE", "beta", "queued"),
        ("generic_https", "GB", "gamma", "queued"),
    ]


async def test_open_hour_skips_a_target_not_hourly_and_an_hour_with_no_time_left(database, db):
    prepare(database, ("DE",))
    hourly(database)
    issue_token(database, provider="alpha", product="res", region="fra1")
    cells = [Armed("alpha", "res", "fra1", ("DE",))]
    assert (await open_hour(db, HOUR, cells, HOUR))["skipped"] == "pulse.on_demand is off"
    await set_setting(db, "pulse.on_demand", True)
    # From HH:18 no pulse starts: the droplet's hard stop is 25 minutes after its creation.
    late = await open_hour(db, HOUR, cells, HOUR + timedelta(minutes=18))
    assert late == {"targets": [], "created": 0, "skipped": "no time left in the hour"}
    with connect(database) as conn:
        conn.execute("UPDATE targets SET pulse_interval_minutes=30 WHERE target_id='generic_https'")
    result = await open_hour(db, HOUR, cells, HOUR + timedelta(minutes=10))
    assert result["targets"] == ["generic_http"] and result["not_hourly"] == ["generic_https"]
    assert query(database, "SELECT start_at FROM test_batches") == [
        (HOUR + timedelta(minutes=10, seconds=15),)
    ]
    # Called before the hour: its pulses still start in it.
    early = await open_hour(db, HOUR + timedelta(hours=1), cells, HOUR + timedelta(minutes=58))
    assert early["created"] == 1
    assert query(database, "SELECT max(start_at) FROM test_batches") == [
        (HOUR + timedelta(hours=1, seconds=15),)
    ]
    with pytest.raises(ValueError, match="whole UTC hour"):
        await open_hour(db, HOUR + timedelta(minutes=1), cells, HOUR)


async def test_on_demand_turns_the_minute_planners_off(database, db):
    prepare(database, ("DE",), DE=time(12))
    issue_token(database, provider="alpha", product="res", region="fra1")
    await set_setting(db, "pulse.on_demand", True)
    at = HOUR - timedelta(seconds=30)
    census = await batches.census_plan(db, at)
    pulses = await batches.pulse_plan(db, HOUR)
    assert census == {"created": 0, "skipped": "on-demand pulses: no census"}
    assert pulses["skipped"] == "on-demand pulses: the hourly job"
    assert query(database, "SELECT 1 FROM test_batches") == []


async def test_a_cell_may_hold_another_regions_countries_whose_runs_go_from_its_node(database, db):
    """PULSE-REPLACE-1: a lost cell's countries handed to another region's droplet: the batch
    stays in its country's region, the run goes to the node of the cell's region."""
    prepare(database, ("DE",))  # fra1
    hourly(database)
    await set_setting(db, "pulse.on_demand", True)
    issue_token(database, provider="beta", product="res", region="lon1")
    result = await open_hour(db, HOUR, [Armed("beta", "res", "lon1", ("DE",))], HOUR)
    assert (result["created"], result["runs"]) == (2, 2)
    rows = query(
        database,
        "SELECT DISTINCT b.region_id,r.slug FROM batch_runs br JOIN test_batches b "
        "USING(batch_id) JOIN nodes n ON n.node_id=br.node_id JOIN runner_regions r "
        "ON r.region_id=n.region_id",
    )
    assert rows == [(FRA1, "lon1")]
