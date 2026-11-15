"""PULSE-ONDEMAND-1: one hour of on-demand pulses (campaign.pulse_ondemand, `daily
pulse-hour`): create and arm the hour's nodes, open their pulses, wait, remove them all, alert on
one left. Fake cloud and fake SSH; the database is the test's own; nothing leaves the machine."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fakes import Fault, running
from daily_cases import formulas
from orchestrator_cases import prepare, query, set_setting
from pulse_fleet_cases import DEV, FakeClock, Issuer, Remote, ids_named, make_root, world

from atlas.campaign import pulse_fleet, pulse_ondemand
from atlas.campaign.contracts import PULSE_TAG, DropletRecord
from atlas.campaign.pulse_fleet import FleetError, load_nodes
from atlas.campaign.pulse_ondemand import HourInputs, Services, run_hour
from atlas.cli import main
from atlas.daily.pulses import CapError
from atlas.orchestrator import nodework
from atlas.orchestrator.nodes import issue_token
from atlas.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]

HOUR = datetime(2026, 10, 1, 12, tzinfo=UTC)
NOW = HOUR - timedelta(minutes=2)
SCHEDULES = {"shifter": {"GB": 5, "DE": 9}}  # lon1 and fra1: two droplets


class HourClock(FakeClock):
    """The test's wall clock; each wait closes the hour's batches (the nodes ran them)."""

    def __init__(self, database):
        super().__init__()
        self.t, self.database, self.sleeps = NOW.timestamp(), database, 0

    async def sleep(self, seconds):
        self.t, self.sleeps = self.t + seconds, self.sleeps + 1
        with connect(self.database) as conn:
            conn.execute("UPDATE test_batches SET status='done' WHERE kind='pulse'")


async def ready(database, db):
    prepare(database, ("DE", "GB"))
    with connect(database) as conn:
        conn.execute("UPDATE targets SET pulse_interval_minutes=60 WHERE engine='http'")
    await set_setting(db, "pulse.on_demand", True)
    for region in ("lon1", "fra1"):  # the node rows arming makes (the issuer here is a fake)
        issue_token(database, provider="shifter", product="residential", region=region)


async def hour(
    tmp_path,
    fake,
    db,
    clock,
    *,
    cap=144,
    regions=("lon1", "fra1"),
    ledger=None,
    remote=None,
    **fleet,
):
    root = make_root(tmp_path, regions=regions, **fleet)
    for hour_name, records in (ledger or {}).items():  # earlier hours' ledgers (text: raw)
        (root / "hours" / hour_name).mkdir(parents=True)
        if isinstance(records, str):
            (root / "hours" / hour_name / "nodes.json").write_text(records)
        else:
            pulse_fleet.save_nodes(root / "hours" / hour_name, records)
    inputs = HourInputs(root, formulas(tmp_path, SCHEDULES), "next-hour", cap)
    async with running(fake, per_minute=10**6) as cloud:
        outcome = await run_hour(
            inputs, Services(cloud, remote or Remote(), Issuer(), clock, db), NOW
        )
    return root, outcome


async def test_an_hour_arms_its_nodes_opens_their_pulses_and_removes_every_node(
    tmp_path, database, db
):
    await ready(database, db)
    fake, clock = world(), HourClock(database)
    root, outcome = await hour(tmp_path, fake, db, clock)
    assert (outcome["state"], outcome["armed"], outcome["failed"]) == ("done", 2, [])
    assert outcome["leftover"] == [] and outcome["plan"]["droplets"] == 2
    assert (outcome["created"], outcome["create_failed"]) == (2, {})
    assert query(database, "SELECT kind FROM alerts") == []
    assert (outcome["opened"]["created"], outcome["opened"]["runs"]) == (4, 4)
    # Each retired as its droplet went, when its runs were stored (so the hour's final retire
    # finds none left): the offline job raises nothing for them.
    assert outcome["removed_when_done"] == 2 and "retired" not in outcome  # shown only if not done
    assert (outcome["still_open"], outcome["waiting_to_store"]) == (0, 0)
    assert "retire_error" not in outcome
    assert query(database, "SELECT DISTINCT status FROM nodes") == [("retired",)]
    later = HOUR + timedelta(hours=1)
    assert await nodework.mark_offline(db, later) == {"offline": [], "retired": []}
    assert query(database, "SELECT kind FROM alerts") == []
    names = ("atlas-pulse-fra1-shifter", "atlas-pulse-lon1-shifter")
    assert sorted(kind for kind, _ in fake.deleted) == ["droplet", "droplet"]
    assert not any(ids_named(fake, name) for name in names)
    ledger = load_nodes(root / "hours" / "20261001T12")
    assert sorted(ledger) == list(names) and {r.status for r in ledger.values()} == {"destroyed"}
    written = json.loads((root / "hours" / "20261001T12" / "pulse.json").read_text())
    assert written["state"] == "done" and clock.sleeps >= 1
    assert query(database, "SELECT min(start_at)>=%s FROM test_batches", (HOUR,)) == [(True,)]


async def test_a_node_not_removed_opens_one_alert_and_is_listed(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)
    fake, real = world(), pulse_ondemand.open_hour

    async def failing_then_opened(*args):  # once armed, the cloud refuses lon1's deletion
        for rid in ids_named(fake, "atlas-pulse-lon1-shifter"):
            fake.faults.append(Fault("DELETE", f"/v2/droplets/{rid}", 500, times=10**6))
        return await real(*args)

    monkeypatch.setattr(pulse_ondemand, "open_hour", failing_then_opened)
    _, outcome = await hour(tmp_path, fake, db, HourClock(database))
    [left] = outcome["leftover"]
    assert left["name"] == "atlas-pulse-lon1-shifter" and left["reason"]
    assert ids_named(fake, "atlas-pulse-lon1-shifter") and not ids_named(
        fake, "atlas-pulse-fra1-shifter"
    )
    assert query(database, "SELECT kind,severity,dedupe_key FROM alerts") == [
        ("pulse_teardown_failed", "major", "pulse_teardown_failed:20261001T12")
    ]


async def test_a_cell_whose_name_an_earlier_droplet_holds_is_failed_not_done(
    tmp_path, database, db
):
    await ready(database, db)
    fake = world()
    old = fake.add("atlas-pulse-fra1-shifter", [PULSE_TAG], project=DEV)  # an earlier hour's
    _, outcome = await hour(tmp_path, fake, db, HourClock(database))
    assert outcome["failed"] == [{"name": "atlas-pulse-fra1-shifter", "status": "not_created"}]
    assert outcome["armed"] == 1 and [u["id"] for u in outcome["unknown"]] == [old]
    assert old in fake.droplets  # never ours to remove: not in the hour's ledger
    assert not ids_named(fake, "atlas-pulse-lon1-shifter")


async def test_a_signal_during_the_create_still_removes_or_alerts_the_droplets(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)
    real = pulse_fleet.Fleet._create_call

    async def cancelled_after_the_post(self, names, region):  # created, ids never recorded
        await real(self, names, region)
        raise asyncio.CancelledError

    monkeypatch.setattr(pulse_fleet.Fleet, "_create_call", cancelled_after_the_post)
    fake = world()
    root, outcome = await hour(tmp_path, fake, db, HourClock(database))
    assert outcome["error"] == "cancelled by a signal"
    names = {"atlas-pulse-fra1-shifter", "atlas-pulse-lon1-shifter"}
    left = {item["name"] for item in outcome["leftover"]}
    up = {n for n in names if ids_named(fake, n)}
    assert up == left  # every droplet still up is left over (and alerted), none forgotten
    ledger = load_nodes(root / "hours" / "20261001T12")
    # The cancel came during fra1's create (regions in order), so lon1 was never requested;
    # fra1's droplet was taken by its exact name.
    assert sorted(ledger) == ["atlas-pulse-fra1-shifter"]
    assert ledger["atlas-pulse-fra1-shifter"].droplet_id
    alerts = dict(query(database, "SELECT kind,message FROM alerts"))
    assert sorted(alerts) == ["pulse_hour_failed"] + (["pulse_teardown_failed"] if left else [])
    assert alerts["pulse_hour_failed"].endswith("UTC: stopped: cancelled by a signal")  # exit 3
    assert json.loads((root / "hours/20261001T12/pulse.json").read_text())["leftover"] is not None


async def test_the_cap_counts_pulse_droplets_still_up_and_creates_nothing(tmp_path, database, db):
    await ready(database, db)
    fake = world()
    fake.add("atlas-pulse-syd1-shifter", [PULSE_TAG], project=DEV)
    before = set(fake.droplets)
    with pytest.raises(CapError, match="needs 2 pulse droplets and 1 are still up"):
        await hour(tmp_path, fake, db, HourClock(database), cap=2)
    assert set(fake.droplets) == before and not fake.deleted
    [(kind, message)] = query(database, "SELECT kind,message FROM alerts")
    assert kind == "pulse_hour_failed" and "0 droplets created: " in message  # exit 3 alerts


LON1, FRA1 = "atlas-pulse-lon1-shifter", "atlas-pulse-fra1-shifter"


class StagedClock(HourClock):
    """The nodes finish one after the other: lon1's runs are stored at the first wait, fra1's
    (and the hour's batches) at the second. Each wait notes which droplets and nodes are up."""

    def __init__(self, database, fake):
        super().__init__(database)
        self.fake, self.polls, self.seen = fake, 0, []

    async def sleep(self, seconds):
        self.t += seconds
        if seconds != pulse_ondemand.WAIT_POLL_SECONDS:
            return
        self.polls += 1
        up = tuple(bool(ids_named(self.fake, name)) for name in (LON1, FRA1))
        nodes = query(
            self.database,
            "SELECT r.slug,n.status FROM nodes n JOIN runner_regions r USING(region_id)",
        )
        self.seen.append((up, dict(nodes)))
        with connect(self.database) as conn:
            conn.execute(
                "UPDATE batch_runs br SET status='done' FROM test_batches b, runner_regions r "
                "WHERE b.batch_id=br.batch_id AND r.region_id=b.region_id AND r.slug=%s",
                ("lon1" if self.polls == 1 else "fra1",),
            )
            if self.polls > 1:
                conn.execute("UPDATE test_batches SET status='done' WHERE kind='pulse'")


async def test_each_node_is_retired_and_removed_the_moment_its_runs_are_stored(
    tmp_path, database, db
):
    """PULSE-COMPACT-1: a droplet goes as soon as its own work is stored, not at the hour's
    end: lon1 is gone (its node retired) while fra1 still runs."""
    await ready(database, db)
    fake = world()
    clock = StagedClock(database, fake)
    _, outcome = await hour(tmp_path, fake, db, clock)
    assert clock.seen[0][0] == (True, True)  # both run
    up, nodes = clock.seen[1]
    assert up == (False, True) and nodes["lon1"] == "retired" and nodes["fra1"] != "retired"
    assert (outcome["state"], outcome["removed_when_done"], outcome["leftover"]) == ("done", 2, [])
    assert not ids_named(fake, LON1) and not ids_named(fake, FRA1)
    assert query(database, "SELECT kind FROM alerts") == []


class StuckClock(FakeClock):
    """No node ever stores its runs: the hour's batches stay open."""

    def __init__(self):
        super().__init__()
        self.t = NOW.timestamp()


async def test_a_droplet_never_outlives_its_hard_stop_25_minutes_after_the_job_began(
    tmp_path, database, db
):
    await ready(database, db)
    await set_setting(db, "pulse.stall_minutes", 10**6)  # the hard stop alone (no stall move)
    fake, clock = world(), StuckClock()
    _, outcome = await hour(tmp_path, fake, db, clock)
    assert outcome["state"] == "timed_out" and outcome["removed_when_done"] == 0
    assert len(fake.deleted) == 2 and outcome["leftover"] == []
    # Not stored and never uploaded: a real loss, with one line naming the nodes.
    assert (outcome["still_open"], outcome["waiting_to_store"], outcome["retired"]) == (4, 0, 2)
    assert outcome["open_nodes"] == ["shifter/fra1", "shifter/lon1"]
    # Their droplets gone, no run of theirs stays running: each failed `node_removed`.
    assert outcome["closed_at_removal"] == 4
    assert query(database, "SELECT DISTINCT status,error FROM batch_runs") == [
        ("failed", "node_removed")
    ]
    assert query(database, "SELECT DISTINCT status FROM test_batches") == [("failed",)]
    assert query(database, "SELECT kind,message FROM alerts") == [
        (
            "pulse_runs_open",
            "pulse hour 2026-10-01 12:00 UTC: 4 country pulses not run or not uploaded by the "
            "limit, on shifter/fra1, shifter/lon1",
        )
    ]
    # The wait ended REMOVE_LEAD before the hard stop, and the removal finished in it.
    stop = (NOW + pulse_ondemand.HARD_STOP).timestamp()
    assert stop - pulse_ondemand.REMOVE_LEAD.total_seconds() - 60 < clock.t <= stop


async def test_a_leftover_of_a_finished_hour_never_blocks_the_next_one(tmp_path, database, db):
    """PULSE-COMPACT-1: a pulse droplet older than 30 minutes is a leftover: it is not counted
    toward the cap, it is removed through its hour's ledger (the guarded path), and the
    leftovers share one alert. One in no ledger is listed, never deleted."""
    await ready(database, db)
    fake = world()
    old = (NOW - timedelta(minutes=61)).isoformat().replace("+00:00", "Z")
    rid = fake.add("atlas-pulse-syd1-shifter", [PULSE_TAG], project=DEV, created=old)
    stray = fake.add("atlas-pulse-blr1-shifter", [PULSE_TAG], project=DEV, created=old)
    record = DropletRecord("shifter", "syd1", 11, "atlas-pulse-syd1-shifter", rid, status="armed")
    ledger = {"20261001T10": "{not json", "20261001T11": {record.name: record}}  # one corrupt
    root, outcome = await hour(tmp_path, fake, db, HourClock(database), cap=2, ledger=ledger)
    assert outcome["state"] == "done" and outcome["armed"] == 2  # the cap of 2 held the hour
    assert rid not in fake.droplets and stray in fake.droplets
    assert load_nodes(root / "hours" / "20261001T11")[record.name].status == "destroyed"
    [(kind, key, message, data)] = query(
        database, "SELECT kind,dedupe_key,message,data FROM alerts WHERE kind='pulse_leftover'"
    )
    assert (kind, key) == ("pulse_leftover", "pulse_leftover:20261001T12")
    assert message == (
        "pulse hour 2026-10-01 12:00 UTC: 2 pulse droplets older than 30 minutes were still "
        "up; 1 removed"
    )
    assert [(n["name"], n["removed"], n["reason"]) for n in data["nodes"]] == [
        ("atlas-pulse-blr1-shifter", False, "in no hour ledger"),
        ("atlas-pulse-syd1-shifter", True, None),
    ]


async def test_a_region_without_an_image_is_refused_before_any_create(tmp_path, database, db):
    await ready(database, db)
    fake = world()
    before = set(fake.droplets)
    with pytest.raises(FleetError, match="no image for fra1"):
        await hour(tmp_path, fake, db, HourClock(database), regions=("lon1",))
    assert set(fake.droplets) == before


async def test_an_hour_whose_every_create_fails_says_why_and_opens_one_alert(
    tmp_path, database, db
):
    await ready(database, db)
    message = "You specified an invalid image for Droplet creation."
    body = {"id": "unprocessable_entity", "message": message}
    fake = world()
    fake.faults.append(Fault("POST", "/v2/droplets", 422, times=10**6, body=body))
    _, outcome = await hour(tmp_path, fake, db, HourClock(database))
    assert (outcome["armed"], outcome["created"]) == (0, 0)
    assert outcome["create_failed"] == {"unprocessable_entity": 2}
    assert [(f["status"], f["error_class"]) for f in outcome["failed"]] == [
        ("create_failed", "unprocessable_entity")
    ] * 2
    assert all(f["message"].endswith(message) for f in outcome["failed"])
    assert query(database, "SELECT kind,severity,dedupe_key,message FROM alerts") == [
        (
            "pulse_hour_failed",
            "major",
            "pulse_hour_failed:20261001T12",
            "pulse hour 2026-10-01 12:00 UTC: 0 of 2 droplets created: unprocessable_entity (2)",
        )
    ]


async def test_a_snapshot_name_in_fleet_json_creates_nothing_and_opens_one_alert(
    tmp_path, database, db
):
    await ready(database, db)
    fake = world()
    before = set(fake.droplets)
    images = {"lon1": "atlas-camp-worker-1.0-0123456789ab", "fra1": "77"}
    with pytest.raises(FleetError, match="image_by_region lon1: a snapshot id"):
        await hour(tmp_path, fake, db, HourClock(database), image_by_region=images)
    assert set(fake.droplets) == before
    [(kind, text)] = query(database, "SELECT kind,message FROM alerts")
    assert kind == "pulse_hour_failed" and text.startswith("pulse hour 2026-10-01 12:00 UTC: 0 ")


async def test_the_hour_job_starts_on_a_refused_fleet_json_so_its_alert_opens(tmp_path, database):
    images = {"lon1": "atlas-camp-worker-1.0-0123456789ab", "fra1": "77"}
    root = make_root(
        tmp_path, regions=("lon1", "fra1"), image_by_region=images, database_file=str(database)
    )
    built = pulse_ondemand.services(root)  # no fleet checks: they are run_hour's, with the alert
    inputs = HourInputs(root, formulas(tmp_path, SCHEDULES), "next-hour", 144)
    try:
        with pytest.raises(FleetError, match="image_by_region lon1"):
            await run_hour(inputs, built, NOW)  # refused before any cloud call
    finally:
        await built.cloud.close()
        await built.db.close()
    assert query(database, "SELECT kind FROM alerts") == [("pulse_hour_failed",)]


async def test_an_error_after_the_create_still_removes_every_node(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)

    async def broken(*args):
        raise RuntimeError("orchestrator down")

    monkeypatch.setattr(pulse_ondemand, "open_hour", broken)
    fake = world()
    with pytest.raises(RuntimeError, match="orchestrator down"):
        await hour(tmp_path, fake, db, HourClock(database))
    assert len(fake.deleted) == 2
    written = json.loads((tmp_path / "fleet/hours/20261001T12/pulse.json").read_text())
    assert (written["state"], written["error"], written["leftover"]) == (
        "failed",
        "RuntimeError",
        [],
    )


async def test_a_failed_retire_still_removes_every_node(tmp_path, database, db, monkeypatch):
    await ready(database, db)

    async def broken(*args):
        raise RuntimeError("database down")

    monkeypatch.setattr(pulse_ondemand, "retire_nodes", broken)
    fake = world()
    _, outcome = await hour(tmp_path, fake, db, HourClock(database))
    assert outcome["retire_error"] == "RuntimeError" and "retired" not in outcome
    assert len(fake.deleted) == 2 and outcome["leftover"] == []


async def test_with_on_demand_off_nothing_opens_and_the_nodes_still_go(tmp_path, database, db):
    await ready(database, db)
    await set_setting(db, "pulse.on_demand", False)
    fake = world()
    _, outcome = await hour(tmp_path, fake, db, HourClock(database))
    assert outcome["state"] == "not_opened" and len(fake.deleted) == 2
    [(kind, message)] = query(database, "SELECT kind,message FROM alerts")
    assert kind == "pulse_hour_failed" and "no pulse opened: pulse.on_demand is off" in message


@pytest.mark.parametrize(
    "outcome, code",
    [
        ({"state": "done", "failed": [], "leftover": []}, 0),
        ({"state": "done", "failed": [{"name": "x"}], "leftover": []}, 0),  # handed, not lost
        ({"state": "partial", "failed": [{"name": "x"}], "leftover": []}, 3),
        ({"state": "timed_out", "failed": [], "leftover": []}, 3),
        ({"state": "done", "failed": [], "leftover": [{"name": "x"}]}, 3),
        (CapError("over the cap of 1"), 3),
        (FleetError("fleet.json: invalid image_by_region lon1"), 3),
    ],
)
def test_pulse_hour_cli_exits_3_only_for_an_hour_not_done(
    tmp_path, monkeypatch, capsys, outcome, code
):
    closed = []

    class Closing:
        async def close(self):
            closed.append(1)

    async def fake_run(inputs, built, now):
        assert (inputs.root, inputs.cap, inputs.start) == (tmp_path, 7, "now-hour")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(pulse_ondemand, "run_hour", fake_run)
    monkeypatch.setattr(
        pulse_ondemand, "services", lambda root: Services(Closing(), None, None, None, Closing())
    )
    args = ["daily", "pulse-hour", "--root", str(tmp_path), "--formulas", str(tmp_path)]
    assert main(args + ["--start", "now-hour", "--max-droplets", "7"]) == code
    assert len(closed) == 2 and capsys.readouterr().out


def test_the_hour_ships_the_manager_current_wheel(tmp_path):
    """IMAGE-AUTO-1: `<env>/wheels/current.json` beside the pulse root replaces fleet.json's
    wheel for the hour; without it the hour keeps fleet.json's."""
    from test_current_wheel import COMMIT, current

    from atlas.campaign.current_wheel import load
    from atlas.daily.planner import load_schedules
    from atlas.daily.pulses import pulse_hour

    root = make_root(tmp_path, regions=("lon1", "fra1"))
    config = pulse_fleet.load_config(root)
    plan = pulse_hour(load_schedules(formulas(tmp_path, SCHEDULES)), 12, networks=("shifter",))
    assert pulse_ondemand.hour_config(config, plan).wheel == config.wheel
    _, name = current(tmp_path / "wheels")
    code = load(tmp_path / "wheels")
    assert code.commit == COMMIT
    assert pulse_ondemand.hour_config(config, plan, code).wheel == tmp_path / "wheels" / name
