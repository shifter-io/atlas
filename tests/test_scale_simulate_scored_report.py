"""SCORE-DATA-1b: the report of `simulate.py --add-scored-inputs` (simulate_scored_report.py): per
day and network the batches, runs and rows by kind, the load factor, the outage plan, the uptime
per window, the pulse days and the minutes; Markdown in --out, mode 600, never an IP."""

import importlib
import json
import re
import stat
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "scale"))
model = importlib.import_module("simulate_scored")
report = importlib.import_module("simulate_scored_report")

HTTP = model.Target("generic_http", 30, 100, 25, True, 1000)
HTTPS = model.Target("generic_https", 30, 100, 25, False, 1000)
REFERENCES = {day: {"r1": "AQ", "r2": "BQ"} for day in (1, 2)}
NETWORKS = ("alpha", "beta", "gamma")


def given(more=None, **changes) -> report.Given:
    """Three networks over two days; gamma has no day 2 (unless `more` countries give it one)."""
    countries = {(n, d): ("AQ", "AR", "BQ") for n in NETWORKS for d in (1, 2)}
    del countries["gamma", 2]
    regions = {"AQ": "r1", "AR": "r1", "BQ": "r2"}
    profiles = {n: model.Profile(400 + 50 * i, 900, 0.95) for i, n in enumerate(NETWORKS)}
    targets = (HTTP, HTTPS)
    inputs = model.Inputs(date(2026, 10, 25), 2, targets, countries, regions, profiles, 8000, 30)
    inputs = replace(inputs, countries=inputs.countries | (more or {}))
    plan = model.outages(inputs, "7")
    found = report.Given(inputs, plan, "7", REFERENCES, {}, {})
    return report.Given(**(vars(found) | changes))


def test_the_report_counts_what_the_plan_writes():
    about = given()
    found = report.build(about, "plan only", 1000, None)
    counted = model.counts(about.inputs, about.plan, "7", REFERENCES)
    assert found["rows"] == sum(sum(day["rows"].values()) for day in counted.values())
    assert found["batches"] == sum(day["rows"]["test_batches"] for day in counted.values())
    assert set(found["kinds"]) == {"pulse generic_http", "pulse generic_https", "load"}
    for day, own in counted.items():
        for network, mine in own["networks"].items():
            rows = [r for r in found["days"] if r["day"] == day and r["network"] == network]
            pulses = sum(r["runs"] for r in rows if r["kind"].startswith("pulse"))
            loads = sum(r["runs"] for r in rows if r["kind"] == "load")
            assert (pulses, loads) == (mine["pulse"], mine["load"])
    assert not any(r["network"] == "gamma" and r["day"] == 2 for r in found["days"])
    low, high = model.LOAD_FACTOR
    factors = [f for own in found["load_factor"].values() for f in own.values()]
    assert factors and all(low * 0.97 <= f <= high * 1.03 for f in factors)
    assert len(found["outages"]) == len(about.plan)
    assert [o["open"] for o in found["outages"]].count(True) == 1
    assert found["uptime"]["gamma"]["24h"] is None and found["uptime"]["alpha"]["24h"] < 100
    assert found["pulse_days"]["given"] == "default" and found["pulse_days"]["gamma"] == {1: 1}
    assert found["minutes"] == {
        "estimated": round(found["rows"] / 1000 / 60, 1),
        "rows_per_second": 1000,
        "taken": None,
    }


def test_the_report_is_private_markdown_without_addresses(tmp_path):
    day_two = {("gamma", 2): ("AQ", "AR", "BQ")}  # as --pulse-days gamma=1,2 gives it
    about = given(day_two, source={("gamma", 2): 1}, pulse_days={"gamma": [1, 2]})
    found = report.build(about, "run", None, 2.5)
    summary = report.write(tmp_path, found)
    path = tmp_path / report.NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    for title in ("Batches, runs and rows", "Load factor", "Outage plan", "Expected uptime"):
        assert f"## {title}" in text
    assert "taken 2.5." in text and "Pulse days: {'gamma': [1, 2]}." in text
    assert "| gamma | 2 | 1 |" in text
    assert not re.search(r"\d+\.\d+\.\d+\.\d+|::|/", text.replace("rows/s", ""))
    assert summary["report"] == report.NAME and summary["open"] == 1
    assert summary["minutes"]["estimated"] is None and summary["mode"] == "run"


def test_the_load_rate_is_the_mean_of_the_loads_days(tmp_path):
    assert report.load_rate(tmp_path) is None
    days = [{"rows_per_second": 1000}, {"rows_per_second": 3000}, {"rows_per_second": None}]
    (tmp_path / "report.json").write_text(json.dumps({"days": days}))
    assert report.load_rate(tmp_path) == 2000
