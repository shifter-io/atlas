"""LOWAVAIL-PACE-2 (#1247): a network without a connection budget has its daily cells stopped at
HH:45, so one slow cell no longer holds the hour's import and its check.

The cell keeps the countries it finished, stores the one it was running as measured (stop_reason
`cell_stop`) and lists those it never started as `not_reached`: in its import report, in its
daily_test_finished event and in the hour's check, never as failed tests or missing ones.
"""

from __future__ import annotations

import json
from datetime import timedelta

from campaign_hour_cases import ended, harvest_setup, harvested
from test_daily_budget_stop import stopped_root
from test_onboarding import START

from atlas.campaign.contracts import budget_stop_path
from atlas.daily import ingest as daily_ingest
from atlas.daily import overtime, verify
from atlas.onboarding.files import read_json
from atlas.storage import connect, seed

pytest_plugins = ["test_storage", "test_enrichment_cli"]


def test_the_stop_record_keeps_both_stops_and_names_the_cell_stops(tmp_path):
    budget_stop_path(tmp_path, 0).parent.mkdir(parents=True)
    overtime.record_stop(tmp_path, ["proxy-cheap/lon1"])  # HH:27
    assert read_json(budget_stop_path(tmp_path, 0)) == {"cells": ["proxy-cheap/lon1"]}
    overtime.record_stop(tmp_path, ["shifter/fra1"], cell_stop=True)  # HH:45
    assert read_json(budget_stop_path(tmp_path, 0)) == {
        "cells": ["proxy-cheap/lon1", "shifter/fra1"],
        "cell_stop": ["shifter/fra1"],
    }
    assert overtime.CELL_STOP == timedelta(minutes=45)


async def test_a_cell_stop_is_stopped_cell_not_partial(tmp_path):
    spec, root, fake, record, remote, clock = await harvest_setup(tmp_path)
    stopped = {"cells": ["net00/tor1"], "cell_stop": ["net00/tor1"]}
    budget_stop_path(root, 0).write_text(json.dumps(stopped))
    remote.inspected[record.droplets[0].name] = ended(130, "stopped")
    await harvested(spec, record, root, fake, remote, clock)
    assert record.cells == {"net00/tor1": "stopped_cell"} and record.state == "done"


async def test_a_cell_stopped_at_hh45_imports_and_names_the_countries_not_reached(
    tmp_path, monkeypatch, database, fake_database
):
    seed(database)
    root = await stopped_root(tmp_path, monkeypatch)  # CA measured, GL stopped, PM not started
    mmdb, _ = fake_database
    budget_stop_path(root, 0).parent.mkdir(parents=True)
    stopped = {"cells": ["shifter/tor1"], "cell_stop": ["shifter/tor1"]}
    budget_stop_path(root, 0).write_text(json.dumps(stopped))
    report = await daily_ingest.ingest_campaign(root, database, mmdb)
    tor1 = report["cells"]["shifter/tor1"]
    assert (tor1["status"], tor1["issues"], tor1["not_reached"]) == ("stopped_cell", [], ["PM"])
    assert tor1["ingested"] == ["CA", "GL"] and report["complete"] is True
    with connect(database) as conn:
        runs = conn.execute("SELECT status,summary->>'stop_reason' FROM test_runs ORDER BY 2")
        assert runs.fetchall() == [("completed", "cell_stop"), ("completed", "requests")]
        finished = "SELECT data FROM test_events WHERE kind='daily_test_finished'"
        (data,) = conn.execute(finished).fetchone()
        assert data["status"] == "done" and data["not_reached"] == ["PM"]
        window = (START - timedelta(hours=1), START + timedelta(hours=2))
        unreached = conn.execute(verify.NOT_REACHED_SQL, window).fetchall()
    assert unreached == [("shifter", "PM")]


def test_the_hour_check_reads_not_reached_as_neither_failed_nor_missing():
    state = {"CA": "done", "GL": "done", "PM": verify.NOT_REACHED}
    check = verify.tally("daily tests", "shifter", ["CA", "GL", "PM"], state, True)
    assert check.ok is True and check.detail == "2 of 3 stored, 1 not reached (PM)"
