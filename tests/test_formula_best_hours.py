"""Best hours: the UTC hour with the most unique live IPs per country, own or borrowed."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import pytest

from atlas.formula import __main__ as cli
from atlas.formula.best_hours import best_hour, best_hours, build_all, mode_hour, own_hours
from atlas.formula.build import SCHEMA, dumps

PUBLISHED = sorted(Path("formulas").glob("*.json"))


def _curve(ips, requests=None, peak_hour=0, curve="country"):
    requests = requests if requests is not None else [value * 3 for value in ips]
    return {"unique_ips": ips, "requests": requests, "peak_hour": peak_hour, "curve": curve}


def _hours(**at):
    """A 24-hour list, zero except the given hours (h05=7 sets hour 5 to 7)."""
    values = [0] * 24
    for key, value in at.items():
        values[int(key[1:])] = value
    return values


def _formula(network, countries, borrowed=False):
    document = {"schema": SCHEMA, "network": network, "countries": countries}
    if borrowed:
        document["borrowed"] = {"source": "average of all networks"}
    return document


def test_best_hour_takes_the_most_ips_then_requests_then_the_earlier_hour():
    assert best_hour(_curve(_hours(h03=5, h09=8))) == 9
    tied = _hours(h03=8, h09=8)
    assert best_hour(_curve(tied, _hours(h03=1, h09=2))) == 9
    assert best_hour(_curve(tied, _hours(h03=2, h09=2))) == 3
    assert best_hour(_curve([0] * 24)) is None


def test_mode_hour_breaks_ties_by_ip_sum_then_the_earlier_hour():
    assert mode_hour([(4, 1), (4, 1), (7, 100)]) == 4
    assert mode_hour([(4, 10), (7, 30), (7, 1), (4, 5)]) == 7
    assert mode_hour([(7, 10), (4, 10)]) == 4


def test_every_country_is_in_one_hour_and_no_ip_countries_are_listed():
    document = _formula(
        "demo",
        {"US": _curve(_hours(h02=9)), "DE": _curve(_hours(h02=1, h20=4)), "ZZ": _curve([0] * 24)},
    )
    result = best_hours(document, [])
    assert result["schema"] == "atlas-best-hours" and result["schema_version"] == 1
    assert list(result["hours"]) == [f"{hour:02d}" for hour in range(24)]
    assert result["hours"]["02"] == ["US"] and result["hours"]["20"] == ["DE"]
    assert sum(len(codes) for codes in result["hours"].values()) == 2
    assert result["hours"]["00"] == []
    assert result["no_ips"] == ["ZZ"]
    assert result["countries"]["DE"] == {"hour": 20, "unique_ips": 4, "basis": "own", "donors": 0}


def test_a_borrowed_formula_takes_the_donors_mode_or_keeps_its_borrowed_peak():
    donors = [
        _formula("a", {"US": _curve(_hours(h05=10))}),
        _formula("b", {"US": _curve(_hours(h05=3))}),
        _formula("c", {"US": _curve(_hours(h09=50))}),
    ]
    byteful = _formula(
        "byteful",
        {
            "US": _curve(_hours(h16=7), peak_hour=18),
            "XK": _curve(_hours(h16=2), peak_hour=18),
        },
        borrowed=True,
    )
    results = {result["network"]: result for result in build_all([*donors, byteful])}
    countries = results["byteful"]["countries"]
    assert countries["US"] == {"hour": 5, "unique_ips": 0, "basis": "borrowed", "donors": 3}
    assert countries["XK"] == {"hour": 18, "unique_ips": 0, "basis": "borrowed", "donors": 0}
    assert results["c"]["countries"]["US"]["hour"] == 9  # a donor keeps its own hour


def test_the_command_writes_one_file_per_formula(tmp_path):
    formulas = tmp_path / "formulas"
    formulas.mkdir()
    (formulas / "demo.json").write_text(dumps(_formula("demo", {"US": _curve(_hours(h01=1))})))
    (formulas / "notes.json").write_text(json.dumps({"schema": "other"}))
    out = formulas / "best-hours"
    assert cli.main(["best-hours", "--formulas", str(formulas), "--out", str(out)]) == 0
    assert [path.name for path in out.iterdir()] == ["demo.json"]
    assert json.loads((out / "demo.json").read_text())["hours"]["01"] == ["US"]


def test_the_command_fails_without_formulas(tmp_path, capsys):
    args = ["best-hours", "--formulas", str(tmp_path), "--out", str(tmp_path / "out")]
    assert cli.main(args) == 1
    assert "no formula files" in capsys.readouterr().err


@pytest.mark.parametrize("path", PUBLISHED, ids=lambda p: p.stem)
def test_published_best_hours_match_the_own_curve_peak_and_are_current(path):
    document = json.loads(path.read_text())
    if "borrowed" not in document and "spread" not in document:  # a spread curve is borrowed
        hours = own_hours(document)
        for code, entry in document["countries"].items():
            if entry["curve"] == "country":
                assert hours[code][0] == entry["peak_hour"], code
    published = Path("formulas/best-hours") / path.name
    assert published.read_text() == dumps(_expected()[document["network"]])


@cache
def _expected():
    documents = [json.loads(path.read_text()) for path in PUBLISHED]
    return {result["network"]: result for result in build_all(documents)}
