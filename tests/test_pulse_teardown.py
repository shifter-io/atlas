"""PULSE-TEARDOWN-1 and PULSE-STARTFAIL-1: no pulse droplet of our own create is left up past its
hour. A create whose answer was lost is adopted by its own create's proof (exact name, pulse tag,
created after its request, recorded nowhere else), never by a name alone; a droplet stuck outside
Dev Resources goes on the guard's own-unassigned path; a droplet whose start failed is replaced
at once; one stuck leftover never blocks the next hour. Fake cloud and fake SSH; the database is
the test's own; nothing leaves the machine."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from campaign_fakes import PROD_PROJECT, running
from orchestrator_cases import query
from pulse_fleet_cases import DEV, NODE, FakeClock, Remote, ids_named, make_root, records, world
from pulse_fleet_cases import reconcile as fleet_reconcile
from test_pulse_ondemand import FRA1, LON1, NOW, HourClock, hour, ready
from test_pulse_replace import run

from atlas.campaign import pulse_adopt, pulse_fleet, pulse_removal
from atlas.campaign.contracts import PULSE_TAG, DropletRecord
from atlas.daily.planner import HourPlan
from atlas.daily.pulses import PULSE_ROOM, CapError, check_cap
from atlas.onboarding.files import lock

pytest_plugins = ["test_storage", "orchestrator_cases"]

BEFORE = "2026-01-01T00:00:00Z"  # an earlier hour's droplet: before every request below
REQUESTED = "2026-06-01T00:00:00+00:00"  # the fake creates at 2027-01-01T21:58:30Z


def unsure(name: str, region: str = "lon1", status: str = "create_failed") -> DropletRecord:
    """A record of a create whose answer was lost (as on 2026-11-23 at 19:57)."""
    error = "uncertain" if status == "create_failed" else None
    return DropletRecord(
        "shifter", region, 20, name, status=status, requested_at=REQUESTED, error_class=error
    )


def audit(root):
    return [json.loads(line) for line in (root / "audit.jsonl").read_text().splitlines()]


async def test_a_lost_create_answer_whose_droplet_is_listed_late_is_adopted_and_armed(tmp_path):
    root = make_root(tmp_path)
    fake, clock = world(), FakeClock()
    fake.create_then_fail, fake.list_lag = 1, 2  # the two listings right after miss it
    report = await fleet_reconcile(fake, root, clock=clock)
    [rid] = ids_named(fake, NODE)
    record = records(root)[NODE]
    assert report["created"] == [NODE] and not report["failed"]
    assert (record.droplet_id, record.status, record.error_class) == (rid, "armed", None)
    assert rid in fake.projects[DEV] and not fake.deleted


async def test_a_lost_create_never_listed_in_its_minute_is_removed_with_its_hour(tmp_path):
    """The 20:00 hour of 2026-11-23: the droplet exists, the minute of reads never shows it, the
    record stays create_failed (uncertain); the hour's removal, in another process, adopts and
    removes it on the own-unassigned path (it never reached Dev Resources)."""
    root = make_root(tmp_path)
    fake, clock = world(), FakeClock()
    fake.create_then_fail, fake.list_lag = 1, 10
    report = await fleet_reconcile(fake, root, clock=clock)
    [rid] = ids_named(fake, NODE)
    assert report["failed"] == [{"name": NODE, "status": "create_failed"}]
    assert records(root)[NODE].error_class == "uncertain" and records(root)[NODE].droplet_id is None
    assert clock.t - 1000 >= sum(pulse_adopt.ADOPT_DELAYS)
    fake.unlisted_for.clear()  # listed by now
    async with running(fake, per_minute=10**6) as cloud:
        left = await pulse_removal.remove_all(root, cloud, FakeClock())
    assert left == [] and ("droplet", rid) in fake.deleted and not ids_named(fake, NODE)
    record = records(root)[NODE]
    assert (record.droplet_id, record.status) == (rid, "destroyed") and record.assign_failed_at
    assert (audit(root)[-1]["path"], audit(root)[-1]["result"]) == ("own_unassigned", "deleted")


async def test_a_same_name_droplet_made_before_the_request_is_never_adopted(tmp_path):
    fake = world()
    earlier = fake.add(NODE, [PULSE_TAG], project=DEV, created=BEFORE)  # an earlier hour's
    ours = fake.add(NODE, [PULSE_TAG], project=PROD_PROJECT)  # the lost create's
    pulse_fleet.save_nodes(tmp_path, {NODE: unsure(NODE)})
    async with running(fake, per_minute=10**6) as cloud:
        left = await pulse_removal.remove_all(tmp_path, cloud, FakeClock())
        assert earlier not in cloud.created_ids
    assert left == [] and earlier in fake.droplets and ours not in fake.droplets
    assert fake.deleted == [("droplet", ours)]


async def test_an_unsure_record_with_two_candidates_adopts_neither(tmp_path):
    fake = world()
    first = fake.add(NODE, [PULSE_TAG], project=PROD_PROJECT)
    second = fake.add(NODE, [PULSE_TAG], project=PROD_PROJECT)
    pending = unsure(NODE, status="requested")  # a stopped process's create
    pulse_fleet.save_nodes(tmp_path, {NODE: pending})
    async with running(fake, per_minute=10**6) as cloud:
        left = await pulse_removal.remove_all(tmp_path, cloud, FakeClock())
    assert left == [{"name": NODE, "id": None, "reason": "in-flight create: not found"}]
    assert first in fake.droplets and second in fake.droplets and not fake.deleted


async def test_the_sweep_adopts_an_unrecorded_leftover_but_never_another_hours_droplet(
    tmp_path,
):
    """The next hour's sweep finds a droplet no ledger records: the ledgers' unsure creates
    adopt theirs first. A droplet another ledger records is never taken by a name."""
    fake = world()
    kept = fake.add(NODE, [PULSE_TAG], project=DEV)  # hour 21's, recorded and working
    stray = fake.add(FRA1, [PULSE_TAG], project=PROD_PROJECT)  # hour 20's lost create
    hours = tmp_path / "hours"
    armed = DropletRecord("shifter", "lon1", 21, NODE, kept, status="armed")
    pulse_fleet.save_nodes(hours / "20261123T21", {NODE: armed})
    pulse_fleet.save_nodes(hours / "20261123T20", {NODE: unsure(NODE), FRA1: unsure(FRA1)})
    async with running(fake, per_minute=10**6) as cloud:
        old = [await cloud.get(stray)]
        swept = await pulse_removal.remove_leftovers(tmp_path, old, cloud, FakeClock())
    assert [(s["name"], s["removed"], s["reason"]) for s in swept] == [(FRA1, True, None)]
    assert kept in fake.droplets and fake.deleted == [("droplet", stray)]
    hour20 = pulse_fleet.load_nodes(hours / "20261123T20")
    assert hour20[NODE].droplet_id is None and hour20[FRA1].status == "destroyed"


async def test_a_marked_pulse_droplet_made_before_its_request_is_refused(tmp_path):
    fake = world()
    rid = fake.add(NODE, [PULSE_TAG], project=PROD_PROJECT, created=BEFORE)
    record = unsure(NODE, status="destroy_refused")
    record.droplet_id, record.assign_failed_at = rid, REQUESTED
    records_ = {NODE: record}
    async with running(fake, per_minute=10**6) as cloud:
        reason = await pulse_fleet.retire_recorded(
            tmp_path, records_, NODE, DEV, cloud, FakeClock()
        )
    assert reason == "OwnershipError" and rid in fake.droplets and not fake.deleted
    [line] = audit(tmp_path)
    assert (line["result"], line["path"], line["checks"]["project"]) == ("refused", "normal", False)


async def test_a_young_leftover_its_hour_gave_up_never_blocks_the_next_hour(tmp_path, database, db):
    """The 12:00 hour of 2026-11-23: an 11:00 droplet whose project assignment never showed
    (destroy_refused) was younger than 30 minutes and held the cap. Its ledger gave it up: the
    sweep removes it on the own-unassigned path, and the hour runs."""
    await ready(database, db)
    fake = world()
    young = (NOW - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    rid = fake.add("atlas-pulse-syd1-shifter", [PULSE_TAG], project=PROD_PROJECT, created=young)
    record = DropletRecord("shifter", "syd1", 11, "atlas-pulse-syd1-shifter", rid)
    record.status, record.assign_failed_at = "destroy_refused", young
    record.requested_at = (NOW - timedelta(minutes=11)).isoformat()
    ledger = {"20261001T11": {record.name: record}}
    root, outcome = await hour(tmp_path, fake, db, HourClock(database), cap=2, ledger=ledger)
    assert outcome["state"] == "done" and outcome["armed"] == 2  # not refused at the cap
    assert rid not in fake.droplets
    assert load(root, "20261001T11")[record.name].status == "destroyed"
    [message] = query(database, "SELECT message FROM alerts WHERE kind='pulse_leftover'")
    assert message[0] == (
        "pulse hour 2026-10-01 12:00 UTC: 1 pulse droplets older than 30 minutes or left by "
        "an earlier hour were still up; 1 removed"
    )


def load(root, name):
    return pulse_fleet.load_nodes(root / "hours" / name)


def test_leftovers_still_up_count_only_toward_the_accounts_room():
    plan = HourPlan(hour=12, cells=())
    check_cap(plan, 144, running=144, leftovers=PULSE_ROOM - 144)  # over the cap, not the room
    over = PULSE_ROOM - 143
    with pytest.raises(CapError, match=f"{over} leftovers: {PULSE_ROOM + 1} is over the account"):
        check_cap(plan, 144, running=144, leftovers=over)


class FailingStart(Remote):
    """Pushing and starting the node fails on the first `fails[name]` droplets of a name."""

    def __init__(self, fails):
        super().__init__()
        self.fails = dict(fails)

    async def push_start(self, droplet, payload, command):
        if self.fails.get(droplet.name, 0) > 0:
            self.fails[droplet.name] -= 1
            raise RuntimeError("push and start exit 255")
        await super().push_start(droplet, payload, command)


async def test_a_droplet_whose_start_failed_is_replaced_at_once(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)
    fake = world()
    remote = FailingStart({LON1: 1})
    _, outcome, calls = await run(tmp_path, fake, db, HourClock(database), remote, monkeypatch)
    [replaced] = outcome["replaced"]
    assert (replaced["name"], replaced["reason"]) == (LON1, "start_failed")
    assert replaced["removed"] and replaced["armed"]
    assert calls == [["fra1"], ["lon1"]] and outcome["state"] == "done"
    assert fake.deleted[0] == ("droplet", replaced["old_id"])  # before any droplet of the hour


async def test_a_replacement_whose_start_failed_too_goes_at_once_not_at_the_hours_end(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)
    fake = world()
    remote = FailingStart({LON1: 2})
    root, outcome, calls = await run(tmp_path, fake, db, HourClock(database), remote, monkeypatch)
    [replaced] = outcome["replaced"]
    assert (replaced["name"], replaced["armed"]) == (LON1, False)
    assert outcome["handed"] == [{"cell": "shifter/lon1", "to": "shifter/fra1"}]
    # Both lon1 droplets went before fra1's, which stays up until its runs are stored.
    second = load(root, "20261001T12")[LON1].droplet_id
    assert fake.deleted[:2] == [("droplet", replaced["old_id"]), ("droplet", second)]
    assert not ids_named(fake, LON1) and outcome["leftover"] == []
    assert calls == [["fra1"], ["fra1"]]


async def test_an_adoption_error_never_keeps_the_hours_recorded_nodes_up(tmp_path, monkeypatch):
    fake = world()
    rid = fake.add(NODE, [PULSE_TAG], project=DEV)
    armed = DropletRecord("shifter", "lon1", 20, NODE, rid, status="armed")
    pulse_fleet.save_nodes(tmp_path, {NODE: armed, FRA1: unsure(FRA1, "fra1")})

    async def down(*args, **kwargs):
        raise ConnectionError("api down")

    monkeypatch.setattr(pulse_adopt, "adopt", down)
    async with running(fake, per_minute=10**6) as cloud:
        left = await pulse_removal.remove_all(tmp_path, cloud, FakeClock())
    assert left == [{"name": "unsure creates", "id": None, "reason": "ConnectionError"}]
    assert fake.deleted == [("droplet", rid)]


async def test_a_ledger_held_by_a_running_hour_is_skipped_by_the_sweep(tmp_path):
    fake = world()
    stray = fake.add(FRA1, [PULSE_TAG], project=PROD_PROJECT)
    busy = tmp_path / "hours" / "20261123T20"
    pulse_fleet.save_nodes(busy, {FRA1: unsure(FRA1, "fra1")})
    async with running(fake, per_minute=10**6) as cloud:
        old = [await cloud.get(stray)]
        with lock(busy / ".lock"):
            swept = await pulse_removal.remove_leftovers(tmp_path, old, cloud, FakeClock())
    assert [(s["removed"], s["reason"]) for s in swept] == [(False, "in no hour ledger")]
    assert stray in fake.droplets and not fake.deleted


async def test_a_reset_hours_armed_droplets_never_block_the_next_hour(tmp_path, database, db):
    """2026-11-25 21:00: a reset of the manager killed the 20:00 job before its removal; seven
    of its late-armed droplets, younger than 30 minutes, held the next hour at the cap and it
    created none. An earlier hour's droplets are leftovers whatever their age: removed through
    their ledger (the normal guarded path) and never counted."""
    await ready(database, db)
    fake = world()
    young = (NOW - timedelta(minutes=12)).isoformat().replace("+00:00", "Z")
    rid = fake.add("atlas-pulse-syd1-shifter", [PULSE_TAG], project=DEV, created=young)
    record = DropletRecord("shifter", "syd1", 11, "atlas-pulse-syd1-shifter", rid, status="armed")
    ledger = {"20261001T11": {record.name: record}}
    root, outcome = await hour(tmp_path, fake, db, HourClock(database), cap=2, ledger=ledger)
    assert outcome["state"] == "done" and outcome["armed"] == 2
    assert rid not in fake.droplets
    assert load(root, "20261001T11")[record.name].status == "destroyed"
    [(data,)] = query(database, "SELECT data FROM alerts WHERE kind='pulse_leftover'")
    assert [(n["name"], n["removed"]) for n in data["nodes"]] == [(record.name, True)]


async def test_the_coming_hours_own_droplets_still_count_toward_the_cap(tmp_path, database, db):
    """A rerun of the same hour: its own ledger's droplets may still be working."""
    await ready(database, db)
    fake = world()
    young = (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    rid = fake.add("atlas-pulse-syd1-shifter", [PULSE_TAG], project=DEV, created=young)
    record = DropletRecord("shifter", "syd1", 12, "atlas-pulse-syd1-shifter", rid, status="armed")
    ledger = {"20261001T12": {record.name: record}}
    with pytest.raises(CapError, match="needs 2 pulse droplets and 1 are still up"):
        await hour(tmp_path, fake, db, HourClock(database), cap=2, ledger=ledger)
    assert rid in fake.droplets


@pytest.mark.parametrize("readable", [True, False])
async def test_the_sweep_never_adopts_an_always_on_fleet_node(tmp_path, readable):
    """A droplet only the pulse root's own ledger records (`pulse-fleet reconcile`) is never
    taken by a stale hour record; with that ledger unreadable, nothing is adopted at all."""
    fake = world()
    always = fake.add(NODE, [PULSE_TAG], project=DEV)
    stray = fake.add(FRA1, [PULSE_TAG], project=PROD_PROJECT)
    if readable:
        pulse_fleet.save_nodes(tmp_path, {NODE: DropletRecord("shifter", "lon1", 0, NODE, always)})
    else:
        (tmp_path / "nodes.json").write_text("{not json")
    hour20 = tmp_path / "hours" / "20261123T20"
    pulse_fleet.save_nodes(hour20, {NODE: unsure(NODE), FRA1: unsure(FRA1, "fra1")})
    async with running(fake, per_minute=10**6) as cloud:
        old = [await cloud.get(always), await cloud.get(stray)]
        await pulse_removal.remove_leftovers(tmp_path, old, cloud, FakeClock())
    assert always in fake.droplets
    assert (stray in fake.droplets) is not readable  # the stray goes only when adoption ran
