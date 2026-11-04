"""MANAGER-ORCH-1: the daily test on the manager: `daily check` (formulas against the template
for all 24 hours) and `daily hour` (run the hour, then ingest it locally). Offline, with the
DAILY-1 fixtures; the raw spool setting reads a real PostgreSQL."""

from __future__ import annotations

import json
import shutil

import pytest
from test_daily import formulas, hours, make, make_spec, template

from proxy_index.cli import main
from proxy_index.daily import hourly
from proxy_index.orchestrator.db import Database, put_setting

pytest_plugins = ["test_storage"]


def test_formulas_that_fit_the_template_pass_for_every_hour(tmp_path):
    root = template(tmp_path)
    result = hourly.check(formulas(tmp_path, {"shifter": {"GB": 5, "DE": 6}}), root)
    assert result["ok"] is True and result["issues"] == []
    assert result["networks"] == ["shifter"] and result["countries"] == 2
    assert result["droplets_by_hour"] == {str(h): {5: 1, 6: 1}.get(h, 0) for h in range(24)}
    assert hours(root) == []  # nothing written beside the template


@pytest.mark.parametrize(
    "schedules, issue",
    [
        ({"shifter": {"CA": 22}}, "hour 22: the template has no region tor1"),
        ({"shifter": {"GB": 5}, "geonode": {"DE": 7}}, "hour 7: the template cannot test geonode"),
        ({"shifter": {"ZZ": 3}}, "hour 3: shifter: countries in no region: ZZ"),
        ({"shifter": {"GB": 24}}, "ValueError: shifter.json: GB needs a test hour 0..23"),
    ],
)
def test_formulas_that_do_not_fit_name_every_issue(tmp_path, schedules, issue):
    result = hourly.check(formulas(tmp_path, schedules), template(tmp_path))
    assert result["ok"] is False and issue in result["issues"]


def test_an_empty_formula_folder_or_a_parallel_template_is_refused(tmp_path):
    root = template(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert hourly.check(empty, root)["issues"] == ["no formula"]
    path = root / make_spec(networks=("shifter",)).manifests["shifter"]
    shutil.copyfile(make(tmp_path / "parallel", execution_mode="parallel"), path)
    result = hourly.check(formulas(tmp_path, {"shifter": {"GB": 5}}), root)
    assert result["issues"] == ["shifter: the template manifest is not an ephemeral campaign"]
    missing = hourly.check(formulas(tmp_path), tmp_path / "no-template")
    assert missing["ok"] is False and missing["issues"][0].startswith("FileNotFoundError")


def test_the_check_cli_prints_one_line_and_exits_0(tmp_path, capsys):
    root = template(tmp_path)
    folder = formulas(tmp_path, {"shifter": {"GB": 5}, "geonode": {"DE": 7}})
    assert main(["daily", "check", "--formulas", str(folder), "--template", str(root)]) == 0
    (line,) = capsys.readouterr().out.splitlines()
    assert line.startswith('{"ok":false,') and json.loads(line)["issues"]


def fake_hour(monkeypatch, outcome, complete=True):
    """run_hour and ingest_campaign stand-ins; returns the calls they saw."""
    calls = {}

    async def run_hour(inputs):
        calls["run"] = inputs
        return outcome

    async def ingest_campaign(root, database_file, mmdb, *, formulas, spool):
        calls["ingest"] = (root, database_file, mmdb, formulas, spool)
        return {"complete": complete, "cells": {"shifter/lon1": {}}}

    async def spool_dir(database_file):
        return database_file.parent / "spool"

    monkeypatch.setattr(hourly, "run_hour", run_hour)
    monkeypatch.setattr(hourly, "ingest_campaign", ingest_campaign)
    monkeypatch.setattr(hourly, "spool_dir", spool_dir)
    return calls


async def test_the_hour_is_ingested_on_the_manager_from_its_own_root(tmp_path, monkeypatch):
    outcome = {"campaign_id": "20270101t050000z-0123456789ab", "state": "done", "leftover": []}
    calls = fake_hour(monkeypatch, outcome)
    root, db_file = tmp_path / "campaigns" / "template", tmp_path / "database.toml"
    result = await hourly.run_and_ingest(tmp_path / "f", root, db_file, None, "next-hour")
    assert calls["run"].template == root and calls["run"].start == "next-hour"
    assert calls["ingest"] == (
        tmp_path / "campaigns" / outcome["campaign_id"],
        db_file,
        None,
        tmp_path / "f",
        tmp_path / "spool",
    )
    assert result == outcome | {"ingest": {"complete": True, "cells": 1}}


async def test_an_hour_with_nothing_due_imports_nothing(tmp_path, monkeypatch):
    calls = fake_hour(monkeypatch, {"state": "nothing_due", "plan": {}})
    result = await hourly.run_and_ingest(tmp_path, tmp_path, tmp_path / "d", None, "next-hour")
    assert result["ingest"] is None and "ingest" not in calls


@pytest.mark.parametrize(
    "state, leftover, complete, code",
    [
        ("done", [], True, 0),
        ("done", [], False, 3),
        ("done", ["7"], True, 3),
        ("failed", [], True, 3),
        ("nothing_due", None, True, 0),
    ],
)
def test_the_hour_cli_resolves_the_template_link_and_exits_3_unless_clean(
    tmp_path, monkeypatch, capsys, state, leftover, complete, code
):
    real = tmp_path / "campaigns" / "20270101t050000z-0123456789ab"
    real.mkdir(parents=True)
    link = tmp_path / "template"
    link.symlink_to(real)
    seen = []

    async def run_and_ingest(formulas_dir, template_root, database_file, mmdb, start):
        seen.append((template_root, start))
        ingest = None if state == "nothing_due" else {"complete": complete, "cells": 1}
        return {"state": state, "leftover": leftover, "ingest": ingest}

    monkeypatch.setattr(hourly, "run_and_ingest", run_and_ingest)
    argv = ["daily", "hour", "--formulas", str(tmp_path), "--template", str(link)]
    assert main(argv + ["--database-file", str(tmp_path / "db.toml")]) == code
    assert seen == [(real, "next-hour")]
    assert json.loads(capsys.readouterr().out)["state"] == state


async def test_the_spool_is_the_orchestrators_raw_spool_setting(database, tmp_path):
    assert await hourly.spool_dir(database) is None
    db = Database(str(database))
    try:
        async with db.connection() as conn:
            await put_setting(conn, "raw.spool_dir", str(tmp_path / "spool"))
    finally:
        await db.close()
    assert await hourly.spool_dir(database) == tmp_path / "spool"
