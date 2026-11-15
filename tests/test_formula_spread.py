"""PROXY-CHEAP-BUDGET-1: an even 24-hour schedule from the other networks' best hours."""

from __future__ import annotations

import itertools
import json
from collections import Counter
from functools import cache
from pathlib import Path

import pytest
from formula_cases import _country, _counts

from atlas.formula import __main__ as cli
from atlas.formula import spread
from atlas.formula.aggregate import FormulaError
from atlas.formula.build import build_formula, dumps

PUBLISHED = sorted(Path("formulas").glob("*.json"))


def _doc(name: str, peaks: dict[str, int], sparse: tuple[str, ...] = ()) -> dict:
    """A built formula whose countries peak at `peaks`; `sparse` ones have IPs in 5 hours only,
    so they fall back to the network curve (a fallback reason)."""
    countries = {}
    for code, peak in peaks.items():
        hours = range(5) if code in sparse else range(24)
        countries[code] = _country([100 + 50 * (h == peak) if h in hours else 0 for h in range(24)])
    document = build_formula(_counts([1000] * 24, countries), 2, 1, 20)
    document["network"] = name
    return document


def _cost(flow: dict) -> int:
    return sum(spread.distance(b, h) * n for (b, h), n in flow.items() if b != spread.NONE)


def test_distance_goes_around_the_clock():
    assert [spread.distance(*pair) for pair in ((5, 5), (23, 1), (1, 23), (0, 12), (3, 20))] == [
        0,
        2,
        2,
        12,
        7,
    ]


def test_one_crowded_hour_spreads_to_both_sides_at_the_least_total_shift():
    flow = spread.transport({13: 216}, 9)
    per_hour = Counter()
    for (_, hour), count in flow.items():
        per_hour[hour] += count
    assert per_hour == dict.fromkeys(range(24), 9)
    assert _cost(flow) == 18 * sum(range(1, 12)) + 9 * 12  # 9 stay, 18 at each distance 1..11


@pytest.mark.parametrize("best", [(0, 0, 1, 1, 2), (22, 23, 23, 0, 0, 1), (5, 5, 5, 6, 9)])
def test_the_transport_is_optimal_against_every_assignment(best):
    flow = spread.transport(dict(Counter(best)), 1)
    near = sorted({(b + d) % 24 for b in best for d in range(-4, 5)})
    least = min(
        sum(spread.distance(b, h) for b, h in zip(best, hours, strict=True))
        for hours in itertools.permutations(near, len(best))
    )
    assert _cost(flow) == least and sum(flow.values()) == len(best)


def test_more_countries_than_hours_hold_are_refused():
    with pytest.raises(FormulaError, match="do not fit"):
        spread.transport({3: 49}, 2)


def test_the_most_agreed_country_keeps_its_hour_and_the_rest_fill_the_quiet_hours():
    best = {"AA": (5, 1), "BB": (5, 4), "CC": None, "DD": (5, 2)} | {
        f"Z{i}": (h, 1) for i, h in enumerate(range(6, 24))
    }
    schedule = spread.assign(best, 1)
    assert sorted(schedule) == sorted(best) and len(set(schedule.values())) == len(best)
    assert schedule["BB"] == 5 and schedule["DD"] == 4  # 4 donors, then 2: nearest first
    assert schedule["AA"] in (3, 6) and all(schedule[f"Z{i}"] == i + 6 for i in range(18))
    assert schedule == spread.assign(best, 1)  # deterministic


def test_best_hours_count_only_the_donors_own_curves():
    document = _doc("cheap", {"DE": 0, "FR": 0, "JP": 0})
    donors = [
        _doc("a", {"DE": 7, "FR": 2}),
        _doc("b", {"DE": 7, "FR": 9}, sparse=("FR",)),
        _doc("c", {"DE": 3, "FR": 2}),
    ]
    assert donors[1]["countries"]["FR"]["fallback_reason"]
    assert spread.best_hours(document, donors) == {"DE": (7, 2), "FR": (2, 2), "JP": None}


def test_spread_borrows_the_curves_and_writes_the_even_schedule():
    peaks = {code: 13 for code in ("AD", "DE", "FR", "JP", "US")}
    donors = [_doc(name, {"DE": 13, "FR": 13, "US": 14}) for name in ("a", "b", "c")]
    own = _doc("cheap", peaks)
    days = {c: e["unique_ips"][13] * e["daily_factor"] for c, e in own["countries"].items()}
    built = {"daily_factor": own["network_curve"]["daily_factor"], "countries": {}}
    built["countries"] = {c: e["daily_factor"] for c, e in own["countries"].items()}
    document = spread.spread(own, donors)
    assert document["spread"]["built"] == built  # all five on their own curve when built
    for code, entry in document["countries"].items():  # the day estimate stays the formula's
        day = entry["estimated_unique_ips"][entry["peak_hour"]] * entry["daily_factor"]
        assert day == pytest.approx(days[code]) and entry["hour_basis"] == ["borrowed"] * 24
    network = document["network_curve"]
    day = network["estimated_unique_ips"][network["peak_hour"]] * network["daily_factor"]
    assert day == pytest.approx(network["day_unique_ips"])
    assert document["formula_version"] == 3 and document["spread"]["capacity"] == 1
    assert document["schedule"] == {c: e["test_hour"] for c, e in document["countries"].items()}
    assert len(set(document["schedule"].values())) == 5
    assert document["spread"]["best_hours"] == {
        "AD": None,
        "DE": 13,
        "FR": 13,
        "JP": None,
        "US": 14,
    }
    assert document["spread"]["total_shift"] == 1 and document["spread"]["max_shift"] == 1
    note = "borrowed: average of all networks; cheap is tested on an even 24-hour schedule"
    assert document["borrowed"]["note"] == note
    assert document["countries"]["DE"]["fallback_reason"].startswith(note)
    lines = spread.report(document)
    assert lines[0] == "cheap: 5 countries, at most 1 an hour" and len(lines) == 28
    assert "total shift 1 hours over 3 countries" in lines[25]
    assert lines[26] in ("moved most: DE 13->12", "moved most: FR 13->12") and "AD JP" in lines[27]
    with pytest.raises(FormulaError, match="already borrowed"):
        spread.spread(document, donors)


def test_the_command_rewrites_the_formula_and_the_summary(tmp_path, capsys):
    for document in (_doc("a", {"DE": 4}), _doc("b", {"DE": 4}), _doc("cheap", {"DE": 9})):
        (tmp_path / f"{document['network']}.json").write_text(dumps(document))
    assert cli.main(["spread", "--formulas", str(tmp_path), "--network", "cheap"]) == 0
    written = json.loads((tmp_path / "cheap.json").read_text())
    assert written["schedule"] == {"DE": 4} and written["formula_version"] == 3
    assert "| cheap (borrowed curve) | 3 |" in (tmp_path / "SUMMARY.md").read_text()
    assert "total shift 0 hours" in capsys.readouterr().out
    assert cli.main(["spread", "--formulas", str(tmp_path), "--network", "none"]) == 1
    assert "no formula of none" in capsys.readouterr().err


@cache
def _published() -> dict[str, dict]:
    return {path.stem: json.loads(path.read_text()) for path in PUBLISHED}


def test_proxy_cheap_has_an_even_valid_schedule_from_the_current_donors():
    published = _published()
    document = published["proxy-cheap"]
    schedule = document["schedule"]
    assert sorted(schedule) == sorted(document["countries"]) and len(schedule) == 216
    assert Counter(schedule.values()) == dict.fromkeys(range(24), 9)  # every hour, 9 countries
    assert document["formula_version"] == 3 and document["spread"]["capacity"] == 9
    donors = [d for name, d in published.items() if name != "proxy-cheap" and "borrowed" not in d]
    best = spread.best_hours(document, donors)
    assert sum(1 for chosen in best.values() if chosen) == 180
    assert document["spread"]["best_hours"] == {c: b[0] if b else None for c, b in best.items()}
    assert schedule == spread.assign(best, 9)  # current and deterministic
    assert document["borrowed"]["networks"] == sorted(d["network"] for d in donors)
