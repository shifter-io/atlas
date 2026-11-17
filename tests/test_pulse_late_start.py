"""PULSE-LATE-START-1: a budgeted network's pulse droplets are created when its daily cells of
the hour end, or at pulse_late.GATE_LATEST (HH:30), not with the others at HH:57. Fake cloud and
fake SSH; the database is the test's own."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from orchestrator_cases import query
from pulse_fleet_cases import world
from test_pulse_budget import _daily
from test_pulse_ondemand import HOUR, LON1, NOW, HourClock, hour, ready
from test_pulse_replace import FlakyRemote

from atlas.campaign import pulse_late, pulse_replace
from atlas.daily import budget
from atlas.daily.planner import Cell, HourPlan

pytest_plugins = ["test_storage", "orchestrator_cases"]

SHIFTER = (("shifter", "fra1"), ("shifter", "lon1"))
RUNNING = {"state": "running", "cells": {"shifter/lon1": "running"}}


@dataclass(frozen=True)
class _Config:
    cells: tuple
    regions: tuple
    max_connections: dict = field(default_factory=lambda: {"shifter": 120})


def _arms(monkeypatch, clock) -> list[tuple[datetime, tuple]]:
    """Each fleet pass of the hour: when it ran (the job's clock) and the cells it created."""
    arms, real = [], pulse_replace.arm

    async def arm(hour_root, config, s):
        arms.append((datetime.fromtimestamp(clock.now(), UTC), tuple(sorted(config.cells))))
        return await real(hour_root, config, s)

    monkeypatch.setattr(pulse_replace, "arm", arm)
    return arms


def _budgeted(monkeypatch, tmp_path, record=None):
    """shifter on a budget; its daily hour campaign (`record`: its ledger, None: no daily)."""
    monkeypatch.setattr(budget, "CONNECTION_BUDGET", {"shifter": 250})
    campaigns = tmp_path / "campaigns"
    campaigns.mkdir()
    if record is not None:
        root = _daily(campaigns, f"{HOUR:%Y%m%dt%H}", networks=("shifter",), record=record)
    monkeypatch.setattr(pulse_late, "campaigns_dir", lambda formulas: campaigns)
    return root if record is not None else None


class EndsAt(HourClock):
    """The daily hour writes its daily.json at `end`."""

    def __init__(self, database, daily_root, end):
        super().__init__(database)
        self.daily_root, self.end = daily_root, end.timestamp()

    async def sleep(self, seconds):
        await super().sleep(seconds)
        if self.t >= self.end:
            (self.daily_root / "daily.json").write_text("{}")


async def test_droplets_are_created_when_the_daily_cells_end_early(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)
    root = _budgeted(monkeypatch, tmp_path, RUNNING)
    clock = EndsAt(database, root, HOUR + timedelta(minutes=10))
    arms = _arms(monkeypatch, clock)
    _, outcome = await hour(tmp_path, world(), db, clock)
    ended = HOUR + timedelta(minutes=10)
    assert arms == [(NOW, ()), (ended, SHIFTER)]  # nothing at HH:58: both cells are held
    late = outcome["late"]
    assert (late["gate"], late["at"]) == ("daily_done", ended.isoformat())
    assert late["opened"]["slot"] == ended.isoformat()
    assert (outcome["state"], outcome["armed"], outcome["created"]) == ("done", 2, 2)
    assert outcome["removed_when_done"] == 2 and outcome["leftover"] == []
    assert query(database, "SELECT kind FROM alerts") == []


async def test_droplets_are_created_at_hh30_when_the_daily_cells_run_long(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)
    _budgeted(monkeypatch, tmp_path, RUNNING)
    clock = HourClock(database)
    arms = _arms(monkeypatch, clock)
    _, outcome = await hour(tmp_path, world(), db, clock)
    latest = HOUR + timedelta(minutes=30)
    assert pulse_late.GATE_LATEST == timedelta(minutes=30)  # BUDGET-STOP-EARLIER-1
    assert arms == [(NOW, ()), (latest, SHIFTER)]
    late = outcome["late"]
    assert late["gate"] == "latest" and late["daily_still_running"] == ["shifter/lon1"]
    # The runs start by HH:50 (LAST_START), so the droplets' window is HH:30 to about HH:52.
    assert latest <= datetime.fromisoformat(late["opened"]["slot"]) < HOUR + pulse_late.LAST_START
    assert query(database, "SELECT kind,severity FROM alerts") == [
        ("pulse_budget_overlap", "major")
    ]
    assert (outcome["state"], outcome["created"], outcome["lost"]) == ("done", 2, [])


async def test_without_a_daily_test_the_droplets_are_created_at_once(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)
    _budgeted(monkeypatch, tmp_path)
    clock = HourClock(database)
    arms = _arms(monkeypatch, clock)
    _, outcome = await hour(tmp_path, world(), db, clock)
    assert arms == [(NOW, ()), (NOW, SHIFTER)]  # nothing to wait for
    assert outcome["late"]["gate"] == "daily_done" and outcome["state"] == "done"


async def test_a_network_without_a_budget_is_created_with_the_others_at_hh57(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)
    clock = HourClock(database)
    arms = _arms(monkeypatch, clock)
    _, outcome = await hour(tmp_path, world(), db, clock)
    assert arms == [(NOW, SHIFTER)] and "late" not in outcome
    assert (outcome["state"], outcome["armed"]) == ("done", 2)


async def test_a_late_create_that_fails_loses_its_cells_and_alerts_once(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)
    _budgeted(monkeypatch, tmp_path)
    real = pulse_replace.arm

    async def arm(hour_root, config, s):
        if config.cells:
            raise RuntimeError("cloud down")
        return await real(hour_root, config, s)

    monkeypatch.setattr(pulse_replace, "arm", arm)
    _, outcome = await hour(tmp_path, world(), db, HourClock(database))
    late = outcome["late"]
    assert late["arm_error"] == "RuntimeError" and late["opened"] == {"created": 0}
    assert outcome["lost"] == ["shifter/fra1", "shifter/lon1"]
    assert outcome["state"] == "partial" and outcome["armed"] == 0
    # No droplet of the hour was created: the one hour alert, not one per cell.
    assert query(database, "SELECT kind FROM alerts") == [("pulse_hour_failed",)]


async def test_a_late_droplet_that_never_boots_is_replaced_once_then_handed_off(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)
    _budgeted(monkeypatch, tmp_path)
    remote = FlakyRemote({LON1: 2})  # its replacement never boots either
    _, outcome = await hour(tmp_path, world(), db, HourClock(database), remote=remote)
    assert [(r["name"], r["armed"]) for r in outcome["replaced"]] == [(LON1, False)]
    assert outcome["handed"] == [{"cell": "shifter/lon1", "to": "shifter/fra1"}]
    assert (outcome["lost"], outcome["state"], outcome["armed"]) == ([], "done", 1)
    assert outcome["late"]["opened"]["created"] == 4  # GB and DE, both from fra1
    assert query(database, "SELECT kind FROM alerts") == []


def test_split_holds_back_only_the_budgeted_networks_cells():
    cells = (Cell("decodo", "lon1", ("GB",)), Cell("shifter", "fra1", ("DE",)))
    plan = HourPlan(12, cells)
    config = _Config(cells=(("decodo", "lon1"), ("shifter", "fra1")), regions=("lon1", "fra1"))
    (first, first_plan), (held, held_plan) = pulse_late.split(config, plan)
    assert (first.cells, first.regions) == ((("decodo", "lon1"),), ("lon1",))
    assert (held.cells, held.regions) == ((("shifter", "fra1"),), ("fra1",))
    assert (first_plan.cells, held_plan.cells) == (cells[:1], cells[1:])
    assert held.max_connections == {"shifter": 120} and held_plan.hour == 12
