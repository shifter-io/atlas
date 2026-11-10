"""PROD-AS-STAGING-1: simulate.py --real-hour-override gives one network its own real hours
(owner, 2026-11-10: byteful=16,none). Here alpha=0,none: alpha's day 1 is its real 00:00 hour
and it has no test on day 2; the plan and the report name the override. The fixture is
test_scale_real_hours' (reserved addresses only)."""

import importlib
import json
from dataclasses import replace

import pytest
import test_scale_real_hours as real
from test_scale_real_hours import inputs, source, staging  # noqa: F401 (the fixtures)

simulate_real = importlib.import_module("simulate_real")
simulate_report = importlib.import_module("simulate_report")
sim = importlib.import_module("test_scale_simulate")

pytest_plugins = ["test_storage"]

OVERRIDE = ("--real-hour-override", "alpha=0,none")


def moved() -> dict:
    """expected() with alpha's day-2 records (its real 00:00 hour) on day 1 and none on day 2."""
    found = {key: ips for key, ips in real.expected().items() if key[0] != "alpha"}
    for (network, code, day), ips in real.expected().items():
        if network == "alpha" and day == 2:
            found["alpha", code, 1] = ips
    return found


def test_an_override_is_a_network_and_an_hour_or_none_a_day():
    assert simulate_real.override("byteful=16,none") == ("byteful", (16, None))
    assert simulate_real.override("alpha=0,7") == ("alpha", (0, 7))
    for wrong in ("byteful", "=16,none", "byteful=24,none", "byteful=16,never"):
        with pytest.raises(ValueError):
            simulate_real.override(wrong)


def test_a_day_one_only_hour_has_one_day_of_tests_and_none_on_day_two(
    tmp_path,
    source,  # noqa: F811
):
    given = simulate_real.Source.read(source, real.HOURS, real.formulas(), {"alpha": (0, None)})
    plan = replace(real.real_plan(tmp_path, source), real=given)
    found = real.daily_of(simulate_real.model(real.profile(), plan, 7))
    assert found == moved()
    assert not [key for key in found if key[0] == "alpha" and key[2] == 2]
    assert given.notes["overrides"] == {"alpha": [0, None]}
    assert given.notes["missing"] == {"1": ["gamma"], "2": []}  # alpha's none is not missing
    lines = simulate_report.real_lines(given.notes)
    assert "- override alpha: day 1: 00, day 2: none" in lines
    with pytest.raises(simulate_real.FormulaError, match="no formula for"):
        simulate_real.Source.read(source, real.HOURS, real.formulas(), {"nobody": (0, None)})


def test_the_options_are_checked(staging, inputs, source, tmp_path, capsys):  # noqa: F811
    out = tmp_path / "out"
    for wrong, message in (
        (["--real-hour-override", "alpha=0"], "one hour (or none) for each of the --days"),
        ([*OVERRIDE, *OVERRIDE], "names a network once"),
    ):
        with pytest.raises(SystemExit):
            sim.simulate.main(real.argv(staging, inputs, out, source, *wrong))
        assert message in capsys.readouterr().err
    with pytest.raises(SystemExit):
        sim.simulate.main([*sim.argv(staging, inputs, out, 2), *OVERRIDE])
    assert "goes with --real-hours" in capsys.readouterr().err


def test_an_overridden_load_stores_one_day_and_names_it(
    staging,  # noqa: F811
    inputs,  # noqa: F811
    source,  # noqa: F811
    tmp_path,
    capsys,
):
    out = tmp_path / "out"
    assert sim.simulate.main(real.argv(staging, inputs, out, source, *OVERRIDE, "--plan-only")) == 0
    text = capsys.readouterr().out
    assert "day 1: daily 4, browser" in text and "day 2: daily 4, browser" in text
    assert "- override alpha: day 1: 00, day 2: none" in text
    assert sim.simulate.main(real.argv(staging, inputs, out, source, *OVERRIDE)) == 0
    assert json.loads(capsys.readouterr().out)["hours_done"] == 5
    assert real.stored(staging) == moved()
    state = sim.state(out)
    assert state["real_hour_overrides"] == {"alpha": [0, None]}
    assert state["real"]["overrides"] == {"alpha": [0, None]}
    assert "- override alpha: day 1: 00, day 2: none" in (out / "report.md").read_text()
    again = real.argv(staging, inputs, out, source)  # the same load without the override
    assert sim.simulate.main(again) == 2 and "is the load of" in capsys.readouterr().err
