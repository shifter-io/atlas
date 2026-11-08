"""REPLAY-FIX-2: the replay's pulse geo rows and `replay.py --plan-only`, on the fixture slice set
of test_scale_replay (the test database stands in for staging)."""

import importlib
from pathlib import Path

import pytest
from orchestrator_cases import query
from test_scale_replay import FIRST, NETWORK, argv, staging, write_slices  # noqa: F401

from proxy_index.orchestrator.nodes import issue_token
from proxy_index.storage import connect

replay = importlib.import_module("replay")
replay_plan = importlib.import_module("replay_plan")
replay_steps = importlib.import_module("replay_steps")
synthetic = importlib.import_module("synthetic")

pytest_plugins = ["test_storage"]

RUNS = "SELECT b.kind,count(*) FROM batch_runs JOIN test_batches b USING(batch_id) GROUP BY 1"
DAILY = "SELECT count(*) FROM test_runs WHERE summary->>'test_kind'='daily'"
WRITTEN = (
    "SELECT (SELECT count(*) FROM nodes),(SELECT count(*) FROM test_batches),"
    "(SELECT count(*) FROM settings),(SELECT count(*) FROM load_references)"
)


def planned(database) -> dict[str, int]:
    found = dict(query(database, RUNS)) | {"daily": query(database, DAILY)[0][0]}
    return {kind: found.get(kind, 0) for kind in replay_plan.KINDS}


def steps_of(conn, args) -> replay_steps.ReplaySteps:
    names = replay_steps.networks(Path(args.slices))
    return replay_steps.ReplaySteps(conn, args, {"replay_id": "t", "networks": names, "nodes": []})


def test_a_country_without_slice_rows_gets_one_empty_geo_key_and_ingests(
    staging,  # noqa: F811
    tmp_path,
):
    write_slices(tmp_path / "slices", hours=(0,))  # hour 1 has no rows in any country
    args = replay.arguments(argv(staging, tmp_path, 1, 2))
    start = replay.starts(args)[1]
    with connect(staging) as conn:
        conn.autocommit = True
        steps = steps_of(conn, args)
        part, done = steps.pulses(0, start, {})
        assert done and part["failed"] == 0 and part["failures"] == [], part["failures"]
        found = [steps.pulse_geo((0, start), run) for run in steps.pulse_runs(start)]
    assert len(found) == part["planned"] > 0
    assert any(errors for _, errors, _ in found)  # the case the ingest refused as a duplicate
    for _, errors, rows in found:
        requests = synthetic.PULSE_REQUESTS
        assert [row[:4] for row in rows] == [(None, None, requests, requests - errors)]


def test_plan_only_counts_equal_what_the_hours_then_plan(staging, tmp_path):  # noqa: F811
    first = "SELECT min(target_id) FROM targets WHERE pulse_enabled"
    with connect(staging) as conn:  # a 7-minute census target: late slots and load waves
        conn.execute(
            f"UPDATE targets SET pulse_interval_minutes=7,census=true WHERE target_id=({first})"
        )
    args = replay.arguments(argv(staging, tmp_path, 1, 2))
    found = replay.plan_only(args)
    hours = found["hours"]
    assert [hour["start"] for hour in hours] == [s.isoformat() for s in replay.starts(args)]
    assert all(hours[0][kind] > 0 for kind in replay_plan.KINDS)
    with connect(staging) as conn:
        conn.autocommit = True
        steps = steps_of(conn, args)
        for hour, start in zip(hours, replay.starts(args), strict=True):
            before = planned(staging)
            for step in ("daily", "pulses", "browser"):
                assert getattr(steps, step)(0, start, {})[1], step
            after = planned(staging)
            assert {kind: after[kind] - before[kind] for kind in after} == {
                kind: hour[kind] for kind in replay_plan.KINDS
            }
    totals = {kind: sum(hour[kind] for hour in hours) for kind in replay_plan.KINDS}
    assert found["totals"] == totals == planned(staging)


def test_plan_only_prints_the_hours_and_writes_nothing(
    staging,  # noqa: F811
    tmp_path,
    capsys,
    monkeypatch,
):
    slices = write_slices(tmp_path / "slices")
    formula = replay_steps.compute.load_formula(Path(replay.generate.FORMULAS), NETWORK)
    product = formula.get("product", "residential")  # production's browser node: no browser hour
    region = replay_steps.browser_model.bc.BROWSER_REGION
    issue_token(staging, provider=NETWORK, product=product, region=region, host="browser-prod1")
    real, modes = replay_plan.plan, []

    def plan(conn, args, starts):
        modes.append(conn.execute("SHOW transaction_read_only").fetchone()[0])
        return real(conn, args, starts)

    monkeypatch.setattr(replay.replay_plan, "plan", plan)
    written = query(staging, WRITTEN)
    given = ["--database", str(staging), "--slices", str(slices), "--days", "2"]
    given += ["--first-day", FIRST, "--hours-per-day", "3", "--plan-only"]
    assert replay.main(given) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["hour", *replay_plan.KINDS] and len(lines) == 1 + 6 + 1
    assert lines[1].startswith(f"{FIRST}T00:00:00+00:00")
    assert lines[-1].startswith("total of 2 days")
    assert [line.split()[-1] for line in lines[1:]] == ["0"] * 7  # browser
    assert int(lines[-1].split()[4]) > 0 and int(lines[-1].split()[5]) > 0  # daily, pulses
    assert modes == ["on"] and query(staging, WRITTEN) == written
    assert not (tmp_path / "out").exists()
    with pytest.raises(SystemExit):  # without --plan-only, the go and --out are required
        replay.arguments(given[:-1])
