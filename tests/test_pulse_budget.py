"""PROXY-CHEAP-BUDGET-1: a budgeted network's pulses share its connections and come after its
daily runs: the node's connection pool, the fleet's node settings, the daily-done gate and the
late slot. Offline fakes; the database tests use the test's own PostgreSQL."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from orchestrator_cases import prepare, query, set_setting
from pulse_fleet_cases import FakeClock, Remote, bundle, make_root, reconcile, world
from test_pulse_fleet_config import NODE_JSON, arm_script
from test_pulse_ondemand import HOUR, HourClock, hour, ready

from atlas import node as node_module
from atlas.campaign import pulse_fleet, pulse_late, pulse_ondemand, pulse_wait
from atlas.daily import budget
from atlas.daily.planner import HourPlan, load_schedules, plan_hour
from atlas.daily.pulses import pulse_hour
from atlas.node import Active, NodeAgent, NodeSettings
from atlas.node_pool import ConnectionPool
from atlas.orchestrator.nodes import issue_token
from atlas.orchestrator.pulse_hour import Armed, open_hour
from atlas.regions import load_regions
from atlas.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]

FORMULAS = Path(__file__).resolve().parents[1] / "formulas"


# --- the budget over the hour ---------------------------------------------------------------


def test_a_busy_hour_and_its_pulses_never_exceed_the_budget():
    regions = load_regions().regions
    pairs = zip(regions[:4], (3, 2, 2, 2), strict=True)  # 9 countries in 4 regions
    busy = {code: 5 for region, n in pairs for code in region.countries[:n]}
    schedules = {"proxy-cheap": busy, "decodo": {"DE": 5}}
    daily = budget.daily(plan_hour(schedules, 5), {"proxy-cheap": 500})["proxy-cheap"]
    every = {"proxy-cheap": {region.countries[0]: 5 for region in regions}, "decodo": {"DE": 1}}
    pulses = budget.pulse(pulse_hour(every, 5))
    assert pulses == {"proxy-cheap": 11} and len(regions) == 8  # 8 droplets x 11 = 88
    assert ConnectionPool(pulses["proxy-cheap"]).share(25) == 5  # each pulse at most 5
    # The phases never overlap (pulse_late), so the hour's peak is the larger one + the reserve.
    peak = max(daily["total"], 8 * pulses["proxy-cheap"]) + budget.BROWSER_RESERVE
    assert daily == {"droplets": 4, "concurrency": 22, "total": 88, "budget": 100}
    assert peak <= 100


def test_every_hour_of_proxy_cheaps_formula_keeps_daily_and_pulses_within_100():
    """PROXY-CHEAP-BUDGET-2: each UTC hour of the real formula, daily and pulse droplets each
    within the cap of 30, and the hour's peak never above the budget of 100: the phases never
    overlap (daily.overtime stops the daily runs at HH:35, pulse_late opens the pulses by HH:40)."""
    schedules = load_schedules(FORMULAS)
    proxy_cheap = {"proxy-cheap": schedules["proxy-cheap"]}
    pulses = budget.pulse(pulse_hour(proxy_cheap, 0))["proxy-cheap"]
    for at in range(24):
        found = budget.daily(plan_hour(proxy_cheap, at), {"proxy-cheap": 500})
        daily = found.get("proxy-cheap", {"droplets": 0, "concurrency": 0, "total": 0})
        assert daily["concurrency"] <= 30 and pulses <= 30
        assert max(daily["total"], 8 * pulses) + budget.BROWSER_RESERVE <= 100, at


# --- the node's connection pool -------------------------------------------------------------


async def test_the_pool_never_holds_more_than_its_limit_and_a_stop_ends_a_wait():
    pool, stop = ConnectionPool(30), asyncio.Event()
    assert (pool.share(25), pool.share(10), ConnectionPool(1).share(25)) == (15, 10, 1)
    assert await pool.acquire(15, stop) and await pool.acquire(15, stop)
    third = asyncio.create_task(pool.acquire(15, stop))
    await asyncio.sleep(0.01)
    assert not third.done() and pool.used == 30
    await pool.release(15)
    assert await third and pool.used == 30
    late = asyncio.create_task(pool.acquire(15, stop))
    await asyncio.sleep(0.01)
    stop.set()
    assert await late is False and pool.used == 30
    with pytest.raises(ValueError, match="positive"):
        ConnectionPool(0)


@dataclass(frozen=True)
class _Config:
    concurrency: int


async def test_a_node_with_max_connections_runs_each_pulse_at_its_share(tmp_path, monkeypatch):
    agent = NodeAgent(_settings(tmp_path, max_connections=30))
    seen, release = [], asyncio.Event()

    async def run(config, sink, options):
        seen.append((config.concurrency, agent.pool.used))
        await release.wait()
        return SimpleNamespace(status="completed", pool_eligible=False)

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(node_module, "run", run)
    monkeypatch.setattr(agent, "config", lambda assignment: _Config(assignment["concurrency"]))
    monkeypatch.setattr(agent, "targets", lambda assignment: None)
    monkeypatch.setattr(agent, "result_sink", lambda assignment: SimpleNamespace(directory=None))
    monkeypatch.setattr(agent, "_progress", nothing)
    monkeypatch.setattr(agent, "upload", nothing)
    now = datetime.now(UTC).isoformat()
    work = {"kind": "pulse", "target_id": "generic_http", "country_code": "DE", "start_at": now}
    runs = [Active(work | {"batch_id": i, "concurrency": 25}) for i in (1, 2, 3)]
    tasks = [asyncio.create_task(agent.execute(active)) for active in runs]
    await asyncio.sleep(0.05)
    assert sorted(seen) == [(15, 15), (15, 30)] and agent.pool.used == 30  # the third waits
    release.set()
    await asyncio.gather(*tasks)
    assert len(seen) == 3 and max(used for _, used in seen) <= 30 and agent.pool.used == 0


def _settings(tmp_path, **extra) -> NodeSettings:
    return NodeSettings(
        "http://o",
        "t",
        provider="p",
        product="q",
        region="fra1",
        proxy_factory=None,
        work_dir=tmp_path,
        **extra,
    )


def test_a_node_without_max_connections_keeps_its_assignment(tmp_path):
    assert NodeAgent(_settings(tmp_path)).pool is None


# --- the fleet's node settings --------------------------------------------------------------


async def test_a_budgeted_networks_node_is_told_its_share_and_others_are_not(tmp_path):
    root = make_root(tmp_path)
    remote = Remote()
    await reconcile(world(), root, remote)
    ((_, plain, _),) = remote.pushed
    assert "max_connections" not in json.loads(bundle(plain)["pulse/node.json"][1])
    config = pulse_fleet.load_config(root)
    assert config.max_connections == {}
    _, units, _ = arm_script(tmp_path, NODE_JSON | {"max_connections": 30})
    assert "--max-connections 30" in units["atlas-pulse-node.service"]
    _, units, _ = arm_script(tmp_path / "plain", NODE_JSON)
    assert "--max-connections" not in units["atlas-pulse-node.service"]


# --- the daily-done gate --------------------------------------------------------------------


def _daily(campaigns, stamp, *, hours=1, networks=("proxy-cheap",), record=None, done=False):
    root = campaigns / f"{stamp}0000z-{len(list(campaigns.iterdir())):012x}"
    root.mkdir(parents=True)
    spec = {"hours": hours, "networks": list(networks)}
    (root / "campaign-spec.json").write_text(json.dumps(spec))
    if record is not None:
        (root / "ledger" / "00").mkdir(parents=True)
        (root / "ledger" / "00" / "hour.json").write_text(json.dumps(record))
    if done:
        (root / "daily.json").write_text("{}")
    return root


def test_the_daily_runs_of_the_hour_are_read_from_its_campaigns(tmp_path):
    campaigns, nets = tmp_path / "campaigns", {"proxy-cheap"}
    campaigns.mkdir()
    at = datetime(2026, 11, 15, 9, tzinfo=UTC)
    assert pulse_late.daily_running(campaigns, at, nets) == []  # no daily run this hour
    _daily(campaigns, "20261115t09", hours=24)  # the template: never a daily hour
    _daily(campaigns, "20261115t09", networks=("decodo",))  # another network's hour
    _daily(campaigns, "20261115t08")  # another hour
    assert pulse_late.daily_running(campaigns, at, nets) == []
    root = _daily(campaigns, "20261115t09")
    assert pulse_late.daily_running(campaigns, at, nets) == ["proxy-cheap/launching"]
    cells = {"proxy-cheap/lon1": "running", "proxy-cheap/fra1": "complete", "decodo/x": "running"}
    record = {"state": "running", "cells": cells}
    (root / "ledger" / "00").mkdir(parents=True)
    (root / "ledger" / "00" / "hour.json").write_text(json.dumps(record))
    assert pulse_late.daily_running(campaigns, at, nets) == ["proxy-cheap/lon1"]
    (root / "ledger" / "00" / "hour.json").write_text(json.dumps({"state": "launching"}))
    assert pulse_late.daily_running(campaigns, at, nets) == ["proxy-cheap/launching"]
    (root / "ledger" / "00" / "hour.json").write_text(json.dumps(record | {"state": "done"}))
    assert pulse_late.daily_running(campaigns, at, nets) == []
    (root / "ledger" / "00" / "hour.json").write_text(json.dumps(record))
    (root / "daily.json").write_text("{}")
    assert pulse_late.daily_running(campaigns, at, nets) == []
    (tmp_path / "campaigns" / "20261115t090000z-broken").mkdir()  # no spec yet
    assert pulse_late.daily_running(campaigns, at, nets) == []


async def test_the_gate_opens_when_the_daily_runs_end_or_at_minute_40(tmp_path):
    campaigns, at = tmp_path / "campaigns", datetime(2026, 11, 15, 9, tzinfo=UTC)
    campaigns.mkdir()
    root = _daily(campaigns, "20261115t09", record={"state": "running", "cells": {}})
    record = {"state": "running", "cells": {"proxy-cheap/lon1": "running"}}
    (root / "ledger" / "00" / "hour.json").write_text(json.dumps(record))
    clock = FakeClock()
    clock.t = (at + timedelta(minutes=15)).timestamp()
    gate = await pulse_late.wait_for_daily(clock, campaigns, at, {"proxy-cheap"})
    assert gate["gate"] == "latest" and gate["daily_still_running"] == ["proxy-cheap/lon1"]
    assert gate["at"] == (at + timedelta(minutes=40)).isoformat()

    class Ends(FakeClock):
        async def sleep(self, seconds):
            self.t += seconds
            (root / "daily.json").write_text("{}")

    clock = Ends()
    clock.t = (at + timedelta(minutes=15)).timestamp()
    gate = await pulse_late.wait_for_daily(clock, campaigns, at, {"proxy-cheap"})
    done = at + timedelta(minutes=15, seconds=15)
    assert gate == {"gate": "daily_done", "at": done.isoformat()}


def test_the_late_slot_is_a_minute_of_the_hour_never_its_start(tmp_path):
    at = datetime(2026, 11, 15, 9, tzinfo=UTC)
    assert pulse_late.slot(at, at - timedelta(minutes=3)) == at + timedelta(minutes=1)
    assert pulse_late.slot(at, at + timedelta(minutes=17, seconds=40)) == at + timedelta(minutes=17)
    daily = tmp_path / "daily"
    (tmp_path / "campaigns" / "t").mkdir(parents=True)
    daily.mkdir()
    (daily / "template").symlink_to(tmp_path / "campaigns" / "t")
    assert pulse_late.campaigns_dir(daily / "formulas") == (tmp_path / "campaigns").resolve()


# --- the database: the late slot and a budgeted hour ----------------------------------------


async def test_a_late_slot_has_its_own_batches_after_the_hours_first(database, db):
    prepare(database, ("DE",))
    with connect(database) as conn:
        conn.execute("UPDATE targets SET pulse_interval_minutes=60 WHERE engine='http'")
    await set_setting(db, "pulse.on_demand", True)
    for provider in ("alpha", "beta"):
        issue_token(database, provider=provider, product="res", region="fra1")
    first = await open_hour(db, HOUR, [Armed("alpha", "res", "fra1", ("DE",))], HOUR)
    slot = HOUR + timedelta(minutes=17)
    cells = [Armed("beta", "res", "fra1", ("DE",))]
    late = await open_hour(db, HOUR, cells, slot, slot=slot, last_start=timedelta(minutes=50))
    assert (first["created"], late["created"], late["runs"]) == (2, 2, 2)
    rows = query(
        database,
        "SELECT p.provider,b.scheduled_for,min(b.start_at) FROM batch_runs br JOIN test_batches b "
        "USING(batch_id) JOIN products p USING(product_id) GROUP BY 1,2 ORDER BY 1",
    )
    assert [(r[0], r[1]) for r in rows] == [("alpha", HOUR), ("beta", slot)]
    assert rows[1][2] == slot + timedelta(seconds=15)
    for bad in (HOUR + timedelta(hours=1), HOUR + timedelta(minutes=3, seconds=1)):
        with pytest.raises(ValueError, match="whole minute of its hour"):
            await open_hour(db, HOUR, cells, slot, slot=bad)


async def test_a_budgeted_network_pulses_after_its_daily_runs_in_its_own_slot(
    tmp_path, database, db, monkeypatch
):
    monkeypatch.setattr(budget, "CONNECTION_BUDGET", {"shifter": 250})
    await ready(database, db)
    fake, clock, remote = world(), HourClock(database), Remote()
    campaigns = tmp_path / "campaigns"
    campaigns.mkdir()
    monkeypatch.setattr(pulse_late, "campaigns_dir", lambda formulas: campaigns)
    root, outcome = await hour(tmp_path, fake, db, clock, remote=remote)
    assert outcome["state"] == "done" and outcome["leftover"] == []
    assert outcome["opened"] == {"created": 0}  # nothing at HH:00: shifter is held back
    late = outcome["late"]
    assert late["gate"] == "daily_done"
    assert (late["opened"]["created"], late["still_open"]) == (4, 0)
    assert late["opened"]["slot"] == (HOUR + timedelta(minutes=1)).isoformat()
    assert query(database, "SELECT DISTINCT scheduled_for FROM test_batches") == [
        (HOUR + timedelta(minutes=1),)
    ]
    nodes = [json.loads(bundle(payload)["pulse/node.json"][1]) for _, payload, _ in remote.pushed]
    assert {node["max_connections"] for node in nodes} == {120}  # 2 droplets: 240 / 2
    assert outcome["removed_when_done"] == 2


async def test_pulses_wait_for_the_daily_runs_until_minute_40_then_alert(
    tmp_path, database, db, monkeypatch
):
    monkeypatch.setattr(budget, "CONNECTION_BUDGET", {"shifter": 250})
    await ready(database, db)
    campaigns = tmp_path / "campaigns"
    campaigns.mkdir()
    record = {"state": "running", "cells": {"shifter/lon1": "running"}}
    _daily(campaigns, f"{HOUR:%Y%m%dt%H}", networks=("shifter",), record=record)
    monkeypatch.setattr(pulse_late, "campaigns_dir", lambda formulas: campaigns)
    clock = HourClock(database)
    _, outcome = await hour(tmp_path, world(), db, clock)
    late = outcome["late"]
    assert late["gate"] == "latest" and late["daily_still_running"] == ["shifter/lon1"]
    assert late["opened"]["slot"] == (HOUR + timedelta(minutes=40)).isoformat()
    alerts = query(database, "SELECT kind,severity FROM alerts")
    assert alerts == [("pulse_budget_overlap", "major")]
    assert outcome["state"] == "done"


# --- the other networks keep their timing ---------------------------------------------------

EARLY, LATE = "atlas-pulse-lon1-decodo", "atlas-pulse-lon1-shifter"
CELLS = {
    EARLY: Armed("decodo", "residential", "lon1", ("GB",)),
    LATE: Armed("shifter", "residential", "lon1", ("GB",)),
}


async def test_the_others_nodes_still_busy_at_their_deadline_go_before_the_late_phase(
    tmp_path, monkeypatch
):
    calls = []
    held = {"replaced": [], "handed": [], "lost": [], "held": [CELLS[LATE]]}

    async def arm(*args):
        return {"unknown": []}, {}, dict(CELLS), {"created": 2, "runs": 2}, held

    async def wait(hour_root, at, nodes, s, limits, stalls=None):
        calls.append(("wait", sorted(nodes), limits))
        # the decodo node's runs are still going at its deadline
        return pulse_wait.NOTHING | {"still_open": 1, "open_nodes": ["decodo/lon1"]}

    async def remove(hour_root, done, s):
        calls.append(("remove", sorted(done)))
        return dict.fromkeys(done)

    async def late(hour_root, at, networks, cells, s, where):
        calls.append(("late", sorted(cells[0])))
        return {"gate": "daily_done", "opened": {}} | pulse_wait.NOTHING | {"removed": [LATE]}

    for name, fake in (("_arm_and_open", arm), ("wait_and_remove", wait)):
        monkeypatch.setattr(pulse_ondemand, name, fake)
    monkeypatch.setattr(pulse_ondemand, "remove_done", remove)
    monkeypatch.setattr(pulse_ondemand, "_late", late)
    networks = {n: SimpleNamespace(provider=n, product="residential") for n in ("decodo",)}
    networks["shifter"] = SimpleNamespace(provider="shifter", product="residential")
    config = SimpleNamespace(max_connections={"shifter": 120}, networks=networks)
    times = (pulse_wait.Deadlines(0.0, 2000.0), tmp_path)
    outcome = await pulse_ondemand._pulses(tmp_path, config, HourPlan(12, ()), HOUR, None, times)
    # With a late phase the others' uploads are not waited for past their run limit.
    runs_only = pulse_wait.Deadlines(0.0, 0.0)
    assert calls == [("wait", [EARLY], runs_only), ("remove", [EARLY]), ("late", [LATE])]
    assert (outcome["still_open"], outcome["removed_when_done"]) == (1, 1)


class _Db:
    """BUSY_SQL's rows: another network's run still queued in the hour."""

    def __init__(self, rows):
        self.rows = rows

    @contextlib.asynccontextmanager
    async def connection(self):
        yield self

    async def execute(self, sql, params):
        return self

    async def fetchall(self):
        return self.rows


async def test_the_late_wait_ends_with_its_own_nodes_runs_not_the_others(tmp_path, monkeypatch):
    removed = []

    async def remove(hour_root, done, s):
        removed.extend(done)
        return dict.fromkeys(done)

    monkeypatch.setattr(pulse_wait, "remove_done", remove)
    other = {"batch_id": 1, "status": "queued", "provider": "decodo", "product": "residential"}
    s = SimpleNamespace(db=_Db([other | {"region": "lon1"}]), clock=FakeClock())
    nodes = {LATE: CELLS[LATE]}
    limits = pulse_wait.Deadlines(s.clock.now() + 600, s.clock.now() + 600)
    found = await pulse_wait.wait_and_remove(tmp_path, HOUR, nodes, s, limits)
    assert found == pulse_wait.NOTHING | {"removed": [LATE]}
    assert removed == [LATE] and s.clock.t == 1000.0  # no wait
