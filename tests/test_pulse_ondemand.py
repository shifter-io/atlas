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
from atlas.campaign.contracts import PULSE_TAG
from atlas.campaign.pulse_fleet import FleetError, load_nodes
from atlas.campaign.pulse_ondemand import HourInputs, Services, run_hour
from atlas.cli import main
from atlas.daily.pulses import CapError
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


async def hour(tmp_path, fake, db, clock, *, cap=144, regions=("lon1", "fra1"), **fleet):
    root = make_root(tmp_path, regions=regions, **fleet)
    inputs = HourInputs(root, formulas(tmp_path, SCHEDULES), "next-hour", cap)
    async with running(fake, per_minute=10**6) as cloud:
        outcome = await run_hour(inputs, Services(cloud, Remote(), Issuer(), clock, db), NOW)
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
    names = ("pi-pulse-fra1-shifter", "pi-pulse-lon1-shifter")
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
        for rid in ids_named(fake, "pi-pulse-lon1-shifter"):
            fake.faults.append(Fault("DELETE", f"/v2/droplets/{rid}", 500, times=10**6))
        return await real(*args)

    monkeypatch.setattr(pulse_ondemand, "open_hour", failing_then_opened)
    _, outcome = await hour(tmp_path, fake, db, HourClock(database))
    [left] = outcome["leftover"]
    assert left["name"] == "pi-pulse-lon1-shifter" and left["reason"]
    assert ids_named(fake, "pi-pulse-lon1-shifter") and not ids_named(fake, "pi-pulse-fra1-shifter")
    assert query(database, "SELECT kind,severity,dedupe_key FROM alerts") == [
        ("pulse_teardown_failed", "major", "pulse_teardown_failed:20261001T12")
    ]


async def test_a_cell_whose_name_an_earlier_droplet_holds_is_failed_not_done(
    tmp_path, database, db
):
    await ready(database, db)
    fake = world()
    old = fake.add("pi-pulse-fra1-shifter", [PULSE_TAG], project=DEV)  # an earlier hour's
    _, outcome = await hour(tmp_path, fake, db, HourClock(database))
    assert outcome["failed"] == [{"name": "pi-pulse-fra1-shifter", "status": "not_created"}]
    assert outcome["armed"] == 1 and [u["id"] for u in outcome["unknown"]] == [old]
    assert old in fake.droplets  # never ours to remove: not in the hour's ledger
    assert not ids_named(fake, "pi-pulse-lon1-shifter")


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
    names = {"pi-pulse-fra1-shifter", "pi-pulse-lon1-shifter"}
    left = {item["name"] for item in outcome["leftover"]}
    up = {n for n in names if ids_named(fake, n)}
    assert up == left  # every droplet still up is left over (and alerted), none forgotten
    ledger = load_nodes(root / "hours" / "20261001T12")
    # The cancel came during fra1's create (regions in order), so lon1 was never requested;
    # fra1's droplet was taken by its exact name.
    assert sorted(ledger) == ["pi-pulse-fra1-shifter"]
    assert ledger["pi-pulse-fra1-shifter"].droplet_id
    alerts = query(database, "SELECT kind FROM alerts")
    assert alerts == ([("pulse_teardown_failed",)] if left else [])
    assert json.loads((root / "hours/20261001T12/pulse.json").read_text())["leftover"] is not None


async def test_the_cap_counts_pulse_droplets_still_up_and_creates_nothing(tmp_path, database, db):
    await ready(database, db)
    fake = world()
    fake.add("pi-pulse-syd1-shifter", [PULSE_TAG], project=DEV)
    before = set(fake.droplets)
    with pytest.raises(CapError, match="needs 2 pulse droplets and 1 are still up"):
        await hour(tmp_path, fake, db, HourClock(database), cap=2)
    assert set(fake.droplets) == before and not fake.deleted


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
    images = {"lon1": "pi-camp-worker-1.0-0123456789ab", "fra1": "77"}
    with pytest.raises(FleetError, match="image_by_region lon1: a snapshot id"):
        await hour(tmp_path, fake, db, HourClock(database), image_by_region=images)
    assert set(fake.droplets) == before
    [(kind, text)] = query(database, "SELECT kind,message FROM alerts")
    assert kind == "pulse_hour_failed" and text.startswith("pulse hour 2026-10-01 12:00 UTC: 0 ")


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


async def test_with_on_demand_off_nothing_opens_and_the_nodes_still_go(tmp_path, database, db):
    await ready(database, db)
    await set_setting(db, "pulse.on_demand", False)
    fake = world()
    _, outcome = await hour(tmp_path, fake, db, HourClock(database))
    assert outcome["state"] == "not_opened" and len(fake.deleted) == 2


@pytest.mark.parametrize(
    "outcome, code",
    [
        ({"state": "done", "failed": [], "leftover": []}, 0),
        ({"state": "done", "failed": [{"name": "x"}], "leftover": []}, 3),
        ({"state": "timed_out", "failed": [], "leftover": []}, 3),
        ({"state": "done", "failed": [], "leftover": [{"name": "x"}]}, 3),
        (CapError("over the cap of 1"), 3),
    ],
)
def test_pulse_hour_cli_exits_3_unless_clean(tmp_path, monkeypatch, capsys, outcome, code):
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
