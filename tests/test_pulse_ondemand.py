"""PULSE-ONDEMAND-1: one hour of on-demand pulses (campaign.pulse_ondemand, `daily
pulse-hour`): create and arm the hour's nodes, open their pulses, wait, remove them all, alert on
one left. Fake cloud and fake SSH; the database is the test's own; nothing leaves the machine."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fakes import PROD_PROJECT, running
from daily_cases import formulas
from orchestrator_cases import prepare, query, set_setting
from pulse_fleet_cases import DEV, FakeClock, Issuer, Remote, ids_named, make_root, world

from atlas.campaign import pulse_ondemand
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

    def __init__(self, database, on_sleep=None):
        super().__init__()
        self.t, self.database, self.on_sleep, self.sleeps = NOW.timestamp(), database, on_sleep, 0

    async def sleep(self, seconds):
        self.t, self.sleeps = self.t + seconds, self.sleeps + 1
        with connect(self.database) as conn:
            conn.execute("UPDATE test_batches SET status='done' WHERE kind='pulse'")
        if self.on_sleep:
            self.on_sleep()


async def ready(database, db):
    prepare(database, ("DE", "GB"))
    with connect(database) as conn:
        conn.execute("UPDATE targets SET pulse_interval_minutes=60 WHERE engine='http'")
    await set_setting(db, "pulse.on_demand", True)
    for region in ("lon1", "fra1"):  # the node rows arming makes (the issuer here is a fake)
        issue_token(database, provider="shifter", product="residential", region=region)


async def hour(tmp_path, fake, db, clock, *, cap=144, regions=("lon1", "fra1")):
    root = make_root(tmp_path, regions=regions)
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
    assert (outcome["opened"]["created"], outcome["opened"]["runs"]) == (4, 4)
    names = ("pi-pulse-fra1-shifter", "pi-pulse-lon1-shifter")
    assert sorted(kind for kind, _ in fake.deleted) == ["droplet", "droplet"]
    assert not any(ids_named(fake, name) for name in names)
    ledger = load_nodes(root / "hours" / "20261001T12")
    assert sorted(ledger) == list(names) and {r.status for r in ledger.values()} == {"destroyed"}
    written = json.loads((root / "hours" / "20261001T12" / "pulse.json").read_text())
    assert written["state"] == "done" and clock.sleeps >= 1
    assert query(database, "SELECT min(start_at)>=%s FROM test_batches", (HOUR,)) == [(True,)]


async def test_a_node_not_removed_opens_one_alert_and_is_listed(tmp_path, database, db):
    await ready(database, db)
    fake = world()

    def move_one():  # once the node exists: out of Dev Resources, so the guard refuses it
        for rid in ids_named(fake, "pi-pulse-lon1-shifter"):
            fake.move(rid, PROD_PROJECT)

    _, outcome = await hour(tmp_path, fake, db, HourClock(database, move_one))
    [left] = outcome["leftover"]
    assert (left["name"], left["reason"]) == ("pi-pulse-lon1-shifter", "OwnershipError")
    assert ids_named(fake, "pi-pulse-lon1-shifter") and not ids_named(fake, "pi-pulse-fra1-shifter")
    assert query(database, "SELECT kind,severity,dedupe_key FROM alerts") == [
        ("pulse_teardown_failed", "major", "pulse_teardown_failed:20261001T12")
    ]


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
