"""DAILY-BUDGET-STOP-1 (#1197): a budget stop only when the network pulses in the hour, a
budget-stopped country stored as measured, and an hour's import that never waits for a budgeted
network's cells.

Offline: overtime's stops and pulse file, the budget stop's record, the import of a round the
budget stopped (a real round through the production runner with a fake transport, a fake DB-IP
database and an isolated PostgreSQL), the runner's `ready` and the hour job's two imports.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from campaign_hour_cases import PRE_CREATE, FakeClock, make_root, make_spec
from helpers import FakeTransport
from onboarding_round_cases import make
from test_daily_overtime import StoppingRemote, recorded
from test_onboarding import START, Clock, Proxy

from atlas import RunConfig, run
from atlas.campaign.contracts import budget_stop_path, spec_path
from atlas.campaign.tick import load_hour, save_hour
from atlas.daily import budget, hourly, overtime, runner
from atlas.daily import ingest as daily_ingest
from atlas.daily.ingest import Part
from atlas.onboarding import manifest as campaign
from atlas.onboarding.files import read_json
from atlas.onboarding.rounds import execute_round
from atlas.storage import connect, seed

pytest_plugins = ["test_storage", "test_enrichment_cli"]

BEGINS = datetime(2027, 1, 1, 22, tzinfo=UTC)
BUDGET_STOP = (BEGINS + timedelta(minutes=27), frozenset({"proxy-cheap"}))
SAFETY_STOP = (BEGINS + overtime.SAFETY, None)
CELL_STOP = (BEGINS + timedelta(minutes=45), frozenset({"shifter"}))  # LOWAVAIL-PACE-2


def test_with_its_pulses_paused_a_budgeted_network_has_no_stop_at_hh27():
    networks = ("proxy-cheap", "shifter")
    assert overtime.stops(BEGINS, networks, frozenset({"shifter"})) == [CELL_STOP, SAFETY_STOP]
    assert overtime.stops(BEGINS, networks, frozenset()) == [CELL_STOP, SAFETY_STOP]
    both = [BUDGET_STOP, CELL_STOP, SAFETY_STOP]
    assert overtime.stops(BEGINS, networks, frozenset(networks)) == both
    assert overtime.stops(BEGINS, networks) == both  # pulses unknown


def test_the_pulsed_networks_are_those_of_the_pulse_fleet(tmp_path):
    assert overtime.pulsed(tmp_path) == frozenset()  # no pulse root: no pulses at all
    fleet = tmp_path / "pulse" / "fleet.json"
    fleet.parent.mkdir()
    fleet.write_text(json.dumps({"networks": {"shifter": {}, "geonode": {}}}))
    assert overtime.pulsed(tmp_path) == frozenset({"shifter", "geonode"})
    fleet.write_text("{not json")
    assert overtime.pulsed(tmp_path) is None  # unreadable: every budgeted network stops


async def test_the_budget_stop_records_the_cells_it_stops(tmp_path):
    spec = make_spec()
    root = make_root(tmp_path, spec)
    a, b = spec.networks
    workers = [(a, "lon1", "running"), (a, "fra1", "destroyed"), (b, "fra1", "running")]
    names = recorded(root, spec, workers)
    remote = StoppingRemote(FakeClock(PRE_CREATE))
    assert await overtime.stop_live(root, spec, remote, frozenset({a})) == names[:1]
    assert read_json(budget_stop_path(root, 0)) == {"cells": [f"{a}/lon1"]}
    budget_stop_path(root, 0).unlink()
    assert len(await overtime.stop_live(root, spec, remote)) == 2  # the safety stop: no record
    assert not budget_stop_path(root, 0).exists()


async def stopped_root(tmp_path, monkeypatch):
    """A collected daily test whose tor1 round the budget stopped: CA measured, GL stopped while
    it ran, PM never started."""
    import atlas.runner as runner

    monkeypatch.setattr(runner.time, "time", lambda: START.timestamp() + 1)
    root = tmp_path / "daily"
    path = make(root, due_countries={"tor1": ["CA", "GL", "PM"]})
    spec = make_spec(
        networks=("shifter",),
        regions=("tor1",),
        manifests={"shifter": path.relative_to(root).as_posix()},
        starts_at=campaign.timestamp(START),
    )
    spec_path(root).parent.mkdir(parents=True, exist_ok=True)
    spec_path(root).write_text(spec.to_json())
    clock, stop = Clock(START.timestamp()), asyncio.Event()

    async def measure(config, sink, options):
        country = config.proxy.public()["requested_country"].upper()
        config = RunConfig("generic_http", config.proxy, requests=3, concurrency=1)
        options = replace(options, deadline=None, transport_factory=FakeTransport)
        result = await run(config, sink, options)
        clock.value += 30
        if country != "GL":
            return result
        stop.set()  # the budget stop arrives while GL runs: its finished requests are kept
        return replace(result, status="cancelled", stop_reason="operator")

    await execute_round(path, "tor1", 0, Proxy, clock=clock, stop=stop, run_country=measure)
    return root


async def test_a_budget_stopped_country_is_stored_with_stop_reason_budget(
    tmp_path, monkeypatch, database, fake_database
):
    seed(database)  # db-seed: the runner regions the events reference
    root = await stopped_root(tmp_path, monkeypatch)
    mmdb, _ = fake_database
    manifest_path = root / json.loads(spec_path(root).read_text())["manifests"]["shifter"]
    inputs = daily_ingest.CellInputs(mmdb=mmdb)
    cell, countries, complete = daily_ingest.prepare_cell(
        manifest_path, "tor1", tmp_path / "plain", inputs
    )
    assert cell["issues"] == ["GL: interrupted", "PM: not started"] and not complete
    assert [country.code for country in countries] == ["CA"]  # without the budget's record
    budget_stop_path(root, 0).parent.mkdir(parents=True)
    budget_stop_path(root, 0).write_text(json.dumps({"cells": ["shifter/tor1"]}))
    report = await daily_ingest.ingest_campaign(root, database, mmdb)
    tor1 = report["cells"]["shifter/tor1"]
    assert tor1["status"] == "stopped_budget" and tor1["issues"] == []
    assert tor1["ingested"] == ["CA", "GL"] and tor1["events"] is True and tor1["requests"] == 6
    assert report["complete"] is True  # the hour unit exits 0 (daily.cli._hour)
    with connect(database) as conn:
        runs = conn.execute("SELECT status,summary->>'stop_reason' FROM test_runs ORDER BY 2")
        runs = runs.fetchall()
        finished = "SELECT data->>'status' FROM test_events WHERE kind='daily_test_finished'"
        assert conn.execute(finished).fetchall() == [("done",)]
    assert runs == [("completed", "budget"), ("completed", "requests")]


async def test_an_hour_imported_in_two_parts_audits_both_parts_runs_once(
    tmp_path, monkeypatch, database, fake_database
):
    seed(database)
    root = await stopped_root(tmp_path, monkeypatch)
    mmdb, _ = fake_database
    early = Part(frozenset({"shifter"}), audits=False)
    first = await daily_ingest.ingest_part(root, database, mmdb, early)
    assert sorted(country for country, _ in first["tested"].values()) == ["CA"]
    assert "audits" not in first and first["cells"]["shifter/tor1"]["ingested"] == ["CA"]
    audited = []
    monkeypatch.setattr(daily_ingest.audits, "stored", lambda c, tested, h: audited.append(tested))
    later = Part(frozenset(), earlier=first["tested"])
    rest = await daily_ingest.ingest_part(root, database, mmdb, later)
    assert rest["cells"] == {} and rest["complete"] is True and "tested" not in rest
    assert audited == [{run: tuple(entry) for run, entry in first["tested"].items()}]
    saved = read_json(root / daily_ingest.REPORT)  # the hour's report keeps every part's cells
    assert list(saved["cells"]) == ["shifter/tor1"] and saved["cells"]["shifter/tor1"]["events"]


async def test_a_long_budgeted_cell_does_not_delay_the_other_networks_import(tmp_path, monkeypatch):
    spec = make_spec()
    root = make_root(tmp_path, spec)
    a, b = spec.networks
    recorded(root, spec, [(a, "lon1", "running"), (b, "lon1", "running")])
    record = load_hour(root, spec, 0)
    record.cells = {f"{a}/lon1": "running", f"{b}/lon1": "running"}
    save_hour(root, record)

    class Collecting(FakeClock):
        async def sleep(self, seconds):  # meanwhile the unbudgeted cell is collected, then b's
            await super().sleep(seconds)
            ended = f"{a}/lon1" if record.cells[f"{a}/lon1"] == "running" else f"{b}/lon1"
            record.cells[ended] = "complete"
            save_hour(root, record)

    seen = []

    def ready(found, networks):
        seen.append((found, networks, load_hour(found, spec, 0).cells[f"{b}/lon1"]))

    monkeypatch.setattr(budget, "CONNECTION_BUDGET", {b: 100})
    await runner._ready(runner._Slot(root, spec, (), None, ready=ready), Collecting(PRE_CREATE))
    assert seen == [(root, frozenset({a}), "running")]  # handed while the budgeted cell ran


def hour_root(tmp_path):
    """An hour root beside the template, its spec naming shifter and the budgeted proxy-cheap."""
    root = tmp_path / "campaigns" / "20270101t050000z-0123456789ab"
    spec = make_spec(networks=("proxy-cheap", "shifter"), campaign_id=root.name)
    spec_path(root).parent.mkdir(parents=True)
    spec_path(root).write_text(spec.to_json())
    return root


async def test_the_networks_that_ended_are_imported_first_without_the_audits(tmp_path, monkeypatch):
    root = hour_root(tmp_path)
    parts, threads = [], []

    async def run_hour(inputs, ready):
        ready(root, frozenset({"shifter"}))  # while proxy-cheap still runs
        await asyncio.sleep(0)
        return {"campaign_id": root.name, "state": "done", "leftover": []}

    async def ingest_part(root, database_file, mmdb, part, *, formulas, spool):
        parts.append(part)
        threads.append(threading.current_thread() is threading.main_thread())
        tested = {"run-1": ["GB", "lon1"]} if part.networks == {"shifter"} else {}
        return {"complete": True, "cells": {f"{n}/lon1": {} for n in part.networks}} | (
            {"tested": tested} if not part.audits else {}
        )

    async def spool_dir(database_file):
        return None

    monkeypatch.setattr(hourly, "run_hour", run_hour)
    monkeypatch.setattr(hourly, "ingest_part", ingest_part)
    monkeypatch.setattr(hourly, "spool_dir", spool_dir)
    template = tmp_path / "campaigns" / "template"
    result = await hourly.run_and_ingest(tmp_path, template, tmp_path / "d", None, "next-hour")
    assert parts == [
        Part(frozenset({"shifter"}), audits=False),
        Part(frozenset({"proxy-cheap"}), earlier={"run-1": ["GB", "lon1"]}),
    ]
    assert threads == [False, True]  # the early part in a thread: the run's loop never waits
    assert result["ingest"] == {"complete": True, "cells": 2, "late": ["proxy-cheap"]}


async def test_an_early_import_that_fails_is_imported_again_with_the_rest(tmp_path, monkeypatch):
    root = hour_root(tmp_path)
    parts = []

    async def run_hour(inputs, ready):
        ready(root, frozenset({"shifter"}))
        return {"campaign_id": root.name, "state": "done", "leftover": []}

    async def ingest_part(root, database_file, mmdb, part, *, formulas, spool):
        parts.append(part)
        if not part.audits:
            raise OSError("the database went away")
        return {"complete": True, "cells": {"proxy-cheap/lon1": {}, "shifter/lon1": {}}}

    async def spool_dir(database_file):
        return None

    monkeypatch.setattr(hourly, "run_hour", run_hour)
    monkeypatch.setattr(hourly, "ingest_part", ingest_part)
    monkeypatch.setattr(hourly, "spool_dir", spool_dir)
    template = tmp_path / "campaigns" / "template"
    result = await hourly.run_and_ingest(tmp_path, template, tmp_path / "d", None, "next-hour")
    assert parts == [Part(frozenset({"shifter"}), audits=False), Part()]
    assert result["ingest"] == {"complete": True, "cells": 2, "late": ["proxy-cheap"]}
