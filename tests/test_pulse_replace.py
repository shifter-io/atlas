"""PULSE-REPLACE-1: a pulse droplet that never boots is replaced once before its region opens;
a cell still without a node is lost (one grouped alert), and the hour's state says so. Fake
cloud and fake SSH; the database is the test's own; nothing leaves the machine."""

from __future__ import annotations

from orchestrator_cases import query
from pulse_fleet_cases import FakeClock, Remote, ids_named, world
from test_pulse_ondemand import FRA1, LON1, NOW, HourClock, hour, ready

from atlas.campaign import pulse_ondemand, pulse_replace
from atlas.campaign.pulse_fleet import load_nodes
from atlas.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]


class FlakyRemote(Remote):
    """SSH never answers on the first `fails[name]` droplets of a name (boot_failed)."""

    def __init__(self, fails):
        super().__init__()
        self.fails, self.asked, self.refused = dict(fails), [], set()

    async def wait_ready(self, droplet, limit_seconds):
        self.asked.append(droplet.name)
        if self.fails.get(droplet.name, 0) > 0:
            self.fails[droplet.name] -= 1
            self.refused.add(droplet.id)
            return False
        return True

    def ready_failure(self, droplet):
        return "host_key_changed" if droplet.id in self.refused else None


async def run(tmp_path, fake, db, clock, remote, monkeypatch):
    """The hour with `remote`; returns the ledger root, the outcome and each open_hour's regions."""
    real, calls = pulse_ondemand.open_hour, []

    async def recorded(db_, hour_, cells, now):
        calls.append(sorted({cell.region for cell in cells}))
        return await real(db_, hour_, cells, now)

    monkeypatch.setattr(pulse_ondemand, "open_hour", recorded)
    root, outcome = await hour(tmp_path, fake, db, clock, remote=remote)
    return root, outcome, calls


async def test_a_droplet_that_never_boots_is_replaced_once_before_its_region_opens(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)
    fake, remote = world(), FlakyRemote({LON1: 1})
    root, outcome, calls = await run(tmp_path, fake, db, HourClock(database), remote, monkeypatch)
    [replaced] = outcome["replaced"]
    assert (replaced["name"], replaced["reason"]) == (LON1, "boot_failed")
    assert replaced["removed"] and replaced["armed"]
    assert ("droplet", replaced["old_id"]) in fake.deleted  # the guarded removal of the old one
    ledger = load_nodes(root / "hours" / "20261001T12")
    assert ledger[LON1].attempts == 2 and ledger[LON1].droplet_id != replaced["old_id"]
    # fra1 opened at once; lon1 only after its replacement armed, with its runs.
    assert calls == [["fra1"], ["lon1"]]
    assert (outcome["opened"]["created"], outcome["opened"]["runs"]) == (4, 4)
    assert (outcome["state"], outcome["armed"]) == ("done", 2)
    assert outcome["lost"] == [] and outcome["handed"] == []
    assert remote.asked.count(LON1) == 2 and remote.asked.count(FRA1) == 1
    assert query(database, "SELECT kind FROM alerts") == []
    assert not ids_named(fake, LON1) and not ids_named(fake, FRA1)


def _gb_runs_from(database):
    """The region of the node each GB (lon1) pulse run was given to."""
    return query(
        database,
        "SELECT DISTINCT r.slug FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "JOIN nodes n ON n.node_id=br.node_id JOIN runner_regions r ON r.region_id=n.region_id "
        "WHERE b.country_code='GB'",
    )


async def test_a_replacement_that_fails_too_hands_its_countries_to_another_region(
    tmp_path, database, db, monkeypatch
):
    """The cell's countries go to the same network's armed droplet in another region (fra1):
    GB is pulsed from fra1 in its own (lon1) batches; nothing is lost, so no alert."""
    await ready(database, db)
    fake, remote = world(), FlakyRemote({LON1: 2})
    _, outcome, calls = await run(tmp_path, fake, db, HourClock(database), remote, monkeypatch)
    assert [(r["name"], r["armed"]) for r in outcome["replaced"]] == [(LON1, False)]
    assert outcome["handed"] == [{"cell": "shifter/lon1", "to": "shifter/fra1"}]
    assert (outcome["lost"], outcome["state"]) == ([], "done")
    assert calls == [["fra1"], ["fra1"]]  # fra1 at once, then fra1's node with GB
    assert _gb_runs_from(database) == [("fra1",)]
    assert (outcome["opened"]["created"], outcome["opened"]["runs"]) == (4, 4)
    assert query(database, "SELECT kind FROM alerts") == []
    assert outcome["leftover"] == [] and not ids_named(fake, LON1)  # both lon1 droplets gone
    assert remote.asked.count(LON1) == 2  # replaced once, never twice


async def test_a_network_with_no_droplet_left_loses_its_cells_with_one_grouped_alert(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)
    fake, remote = world(), FlakyRemote({LON1: 2, FRA1: 2})
    _, outcome, _ = await run(tmp_path, fake, db, HourClock(database), remote, monkeypatch)
    assert outcome["lost"] == ["shifter/fra1", "shifter/lon1"] and outcome["handed"] == []
    assert outcome["state"] == "partial" and outcome["armed"] == 0
    assert query(database, "SELECT kind,dedupe_key,message FROM alerts") == [
        (
            "pulse_cells_lost",
            "pulse_cells_lost:20261001T12",
            "pulse hour 2026-10-01 12:00 UTC: cells lost: shifter/fra1, shifter/lon1",
        )
    ]
    assert outcome["leftover"] == [] and not ids_named(fake, LON1) and not ids_named(fake, FRA1)
    ledger = load_nodes(tmp_path / "fleet" / "hours" / "20261001T12")
    assert ledger[LON1].error_detail == {"reason": "host_key_changed"}  # PULSE-KNOWNHOSTS-1


async def test_an_error_during_the_replacement_loses_only_its_cell(
    tmp_path, database, db, monkeypatch
):
    """DigitalOcean failing during the replacement's pass: fra1's pulses still run, lon1's
    countries are handed to fra1, and the hour goes on (not failed)."""
    await ready(database, db)
    real, passes = pulse_replace.arm, []

    async def second_fails(hour_root, config, s):
        passes.append(config.cells)
        if len(passes) == 2:
            raise RuntimeError("api down")
        return await real(hour_root, config, s)

    monkeypatch.setattr(pulse_replace, "arm", second_fails)
    fake, remote = world(), FlakyRemote({LON1: 1})
    _, outcome, calls = await run(tmp_path, fake, db, HourClock(database), remote, monkeypatch)
    assert passes[1] == (("shifter", "lon1"),)  # the replacement's pass: its cell only
    [replaced] = outcome["replaced"]
    assert (replaced["armed"], replaced["error"]) == (False, "RuntimeError")
    assert (outcome["state"], outcome["lost"]) == ("done", [])
    assert outcome["handed"] == [{"cell": "shifter/lon1", "to": "shifter/fra1"}]
    assert outcome["opened"]["runs"] == 4 and "error" not in outcome
    assert query(database, "SELECT kind FROM alerts") == []
    assert outcome["leftover"] == [] and not ids_named(fake, FRA1)


class UploadingClock(FakeClock):
    """The nodes ran every run, but the manager holds their uploads past the deadline."""

    def __init__(self, database):
        super().__init__()
        self.t, self.database = NOW.timestamp(), database

    async def sleep(self, seconds):
        self.t += seconds
        with connect(self.database) as conn:
            conn.execute("UPDATE batch_runs SET status='uploading'")


async def test_runs_only_uploading_at_the_deadline_are_not_timed_out(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)
    _, outcome, _ = await run(
        tmp_path, world(), db, UploadingClock(database), FlakyRemote({}), monkeypatch
    )
    assert (outcome["state"], outcome["still_open"], outcome["replaced"]) == ("done", 0, [])
    assert outcome["removed_when_done"] == 0 and outcome["leftover"] == []  # gone at the stop


def test_merge_opened_sums_the_steps_and_keeps_a_skip_only_alone():
    skipped = {"targets": [], "created": 0, "skipped": "pulse.on_demand is off"}
    one = {"targets": ["generic_http"], "created": 2, "runs": 2, "not_hourly": []}
    two = {"targets": ["generic_https"], "created": 1, "runs": 3, "not_hourly": []}
    assert pulse_replace.merge_opened([]) == {"created": 0}
    assert pulse_replace.merge_opened([skipped]) == skipped
    assert pulse_replace.merge_opened([one, skipped, two]) == {
        "targets": ["generic_http", "generic_https"],
        "created": 3,
        "runs": 5,
        "not_hourly": [],
    }
