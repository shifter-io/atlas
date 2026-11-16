"""DAILY-IMPORT-1 (#1249): each network's daily runs are imported when its own cells end, so one
slow network never holds the hour's import, and an hour's verify counts a late network late.

Offline: the runner's `ready` on a fake hour record, the hour job's parts with a fake import,
and the hour roots `runner.waiting` reads; the verify checks run against an isolated PostgreSQL
(test_daily_verify's fixture rows).
"""

from __future__ import annotations

import asyncio
import threading
from datetime import timedelta

from campaign_hour_cases import PRE_CREATE, SCHEDULED, FakeClock, make_root, make_spec
from test_daily_overtime import recorded
from test_daily_verify import NOW, H, stock

from atlas.campaign.contracts import HourRecord, spec_path
from atlas.campaign.tick import load_hour, save_hour
from atlas.daily import hourly, runner, verify
from atlas.daily.ingest import Part
from atlas.daily.verify import Hour

pytest_plugins = ["test_storage", "calculated_template", "test_daily_verify"]


async def test_each_network_is_handed_when_its_own_cells_end(tmp_path):
    """The 16 Nov 13:00 hour: one network's cells ran 73 minutes; the others are handed at
    their own end, the slow one never (the import once the run ends takes it)."""
    spec = make_spec(networks=("fast", "slow", "later"), regions=("lon1", "syd1"))
    root = make_root(tmp_path, spec)
    cells = [(n, r) for n in spec.networks for r in ("lon1", "syd1")]
    recorded(root, spec, [(n, r, "running") for n, r in cells])
    record = load_hour(root, spec, 0)
    record.cells = {f"{n}/{r}": "running" for n, r in cells}
    save_hour(root, record)
    ends = [["fast/lon1"], ["fast/syd1", "later/lon1"], [], ["later/syd1"], ["slow/lon1"]]
    ends += [["slow/syd1"]]

    class Collecting(FakeClock):
        async def sleep(self, seconds):  # one poll's worth of cells collected each time
            await super().sleep(seconds)
            for key in ends.pop(0) if ends else []:
                record.cells[key] = "complete"
            save_hour(root, record)

    seen = []

    def ready(found, networks):
        seen.append((networks, load_hour(found, spec, 0).cells["slow/syd1"]))

    await runner._ready(runner._Slot(root, spec, (), None, ready=ready), Collecting(PRE_CREATE))
    assert seen == [(frozenset({"fast"}), "running"), (frozenset({"later"}), "running")]


async def test_an_hour_whose_cells_all_end_together_is_imported_once_at_the_end(tmp_path):
    spec = make_spec()
    root = make_root(tmp_path, spec)
    record = load_hour(root, spec, 0)
    record.cells = {f"{n}/lon1": "complete" for n in spec.networks}
    save_hour(root, record)
    seen = []
    await runner._ready(runner._Slot(root, spec, (), None, ready=seen.append), FakeClock(0))
    assert seen == []  # nothing still runs: the import once the run ends takes them all


def hour_root(tmp_path, networks=("fast", "later", "slow")):
    root = tmp_path / "campaigns" / "20270101t220000z-0123456789ab"
    spec_path(root).parent.mkdir(parents=True)
    spec_path(root).write_text(make_spec(networks=networks, campaign_id=root.name).to_json())
    return root


def fake_parts(monkeypatch, root, fails=frozenset()):
    """The hour job's run (fast then later end while slow runs) and import stand-ins; returns
    the parts imported, in order, each with whether another part was importing meanwhile."""
    parts, busy = [], threading.Lock()

    async def run_hour(inputs, ready):
        ready(root, frozenset({"fast"}))
        ready(root, frozenset({"later"}))
        await asyncio.sleep(0)
        return {"campaign_id": root.name, "state": "done", "leftover": []}

    async def ingest_part(found, database_file, mmdb, part, *, formulas, spool):
        alone = busy.acquire(blocking=False)
        await asyncio.sleep(0.01)
        parts.append((part, alone))
        if alone:
            busy.release()
        if not part.audits and part.networks & fails:
            raise OSError("the database went away")
        networks = part.networks if part.networks is not None else frozenset({"*"})
        tested = {f"run-{n}": [n, "lon1"] for n in sorted(networks)}
        cells = {f"{n}/lon1": {} for n in networks}
        return {"complete": True, "cells": cells} | ({} if part.audits else {"tested": tested})

    async def spool_dir(database_file):
        return None

    monkeypatch.setattr(hourly, "run_hour", run_hour)
    monkeypatch.setattr(hourly, "ingest_part", ingest_part)
    monkeypatch.setattr(hourly, "spool_dir", spool_dir)
    return parts


async def test_each_network_is_imported_at_its_own_end_one_part_at_a_time(tmp_path, monkeypatch):
    root = hour_root(tmp_path)
    parts = fake_parts(monkeypatch, root)
    template = tmp_path / "campaigns" / "template"
    result = await hourly.run_and_ingest(tmp_path, template, tmp_path / "d", None, "next-hour")
    earlier = {"run-fast": ["fast", "lon1"], "run-later": ["later", "lon1"]}
    assert parts == [
        (Part(frozenset({"fast"}), audits=False), True),
        (Part(frozenset({"later"}), audits=False), True),  # after fast's, never beside it
        (Part(frozenset({"slow"}), earlier=earlier), True),  # the late one, imported once
    ]
    assert result["ingest"] == {"complete": True, "cells": 3, "late": ["slow"]}


async def test_a_failed_early_part_is_imported_again_with_the_late_network(tmp_path, monkeypatch):
    root = hour_root(tmp_path)
    parts = fake_parts(monkeypatch, root, fails=frozenset({"fast"}))
    template = tmp_path / "campaigns" / "template"
    result = await hourly.run_and_ingest(tmp_path, template, tmp_path / "d", None, "next-hour")
    rest = Part(frozenset({"fast", "slow"}), earlier={"run-later": ["later", "lon1"]})
    assert [part for part, _ in parts][1:] == [Part(frozenset({"later"}), audits=False), rest]
    assert result["ingest"] == {"complete": True, "cells": 3, "late": ["slow"]}


def test_the_waiting_networks_are_read_from_the_hours_roots(tmp_path):
    begins = H.replace(hour=22)
    template = tmp_path / "campaigns" / "template"
    assert runner.waiting(template, begins) == frozenset()  # no hour root yet
    root = hour_root(tmp_path)
    assert runner.waiting(template, begins) == frozenset()  # not launched yet
    cells = {"fast/lon1": "complete", "slow/lon1": "running", "later/lon1": "pending"}
    save_hour(root, HourRecord(root.name, 0, SCHEDULED, cells=cells))
    assert runner.waiting(template, begins) == frozenset({"slow", "later"})
    assert runner.waiting(template, begins + timedelta(hours=1)) == frozenset()  # another hour
    (root / "daily.json").write_text('{"state": "failed", "error": "cancelled by a signal"}')
    assert runner.waiting(template, begins) == frozenset()  # the run ended: its cells never will
    (root / "daily.json").unlink()
    spec_path(root).write_text("{not json")
    assert runner.waiting(template, begins) == frozenset()


def _checks(found, network):
    return {c.section: (c.ok, c.detail) for c in found.checks if c.subject == network}


def test_a_late_network_is_late_not_missing_until_its_run_may_end(calculated_database, formulas):
    """beta stored nothing in the hour while its cells still run: late, not a failure; alpha's
    tests count as stored. Once its run's safety stop has passed, the same hour fails."""
    stock(calculated_database, beta_daily=False)
    found = verify.report(calculated_database, formulas, Hour(H, NOW, frozenset({"beta"})))
    beta = _checks(found, "beta")
    late = "late, cells still running: "
    assert beta["daily tests"] == (None, late + "0 of 1 stored, 1 missing (DE)")
    assert beta["events"] == (None, late + "0 of 1 daily_test_finished")
    assert _checks(found, "alpha")["daily tests"] == (True, "1 of 1 stored")
    assert found.ok
    ended = H + timedelta(hours=7)
    stale = verify.report(calculated_database, formulas, Hour(H, ended, frozenset({"beta"})))
    assert _checks(stale, "beta")["daily tests"] == (False, "0 of 1 stored, 1 missing (DE)")
    done = verify.report(calculated_database, formulas, Hour(H, NOW))
    assert _checks(done, "beta")["daily tests"][0] is False  # its cells ended: missing
