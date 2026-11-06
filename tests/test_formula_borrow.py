"""Hour formulas: the published formulas and the curve a network borrows from the others."""

from __future__ import annotations

import json
import statistics
from pathlib import Path

import pytest
from formula_cases import _country, _counts

from proxy_index.formula import __main__ as cli
from proxy_index.formula.aggregate import FormulaError
from proxy_index.formula.build import build_formula

PUBLISHED = sorted(Path("formulas").glob("*.json"))


def _entries(document):
    return [("network", document["network_curve"]), *document["countries"].items()]


def test_no_published_factor_is_zero():
    zero = [
        (path.stem, code, hour)
        for path in PUBLISHED
        for code, entry in _entries(json.loads(path.read_text()))
        for hour, factor in enumerate(entry["hour_factors"])
        if factor is not None and factor <= 0
    ]
    assert zero == []  # REAL-CYCLE-1: shifter US hour 08 was 0.0; byteful had 23 such hours


@pytest.mark.parametrize("path", PUBLISHED, ids=lambda p: p.stem)
def test_published_hours_are_measured_borrowed_or_no_data(path):
    document = json.loads(path.read_text())
    network = document["network_curve"]
    covered = [state == "present" for state in network["status"]]
    assert [reason is None for reason in network["no_data_reasons"]] == covered
    borrowed = "borrowed" in document
    for code, entry in _entries(document):
        for hour, factor in enumerate(entry["hour_factors"]):
            # A covered (or borrowed) hour has a factor; any other hour is no data (null).
            assert (factor is not None) == (covered[hour] or borrowed), (code, hour, factor)
            assert factor is None or factor <= 1.0, (code, hour, factor)
        if code == "network" or borrowed:
            continue
        peak = entry["unique_ips"][entry["peak_hour"]]
        for hour, basis in enumerate(entry["hour_basis"]):
            if basis in ("country", "country_low_sample"):
                assert round(entry["hour_factors"][hour] * peak) == entry["unique_ips"][hour]


def test_byteful_borrows_the_average_curve_and_keeps_its_measured_hour():
    document = json.loads(Path("formulas/byteful.json").read_text())
    for code, entry in _entries(document):
        assert all(factor > 0 for factor in entry["hour_factors"]), code
    assert document["borrowed"]["note"] == (
        "borrowed: average of all networks, temporary until byteful data is fixed"
    )
    assert "byteful" not in document["borrowed"]["networks"]
    assert len(document["borrowed"]["networks"]) == len(PUBLISHED) - 1
    for code, entry in _entries(document):
        measured = entry["unique_ips"]
        assert entry["estimated_unique_ips"][16] == measured[16] > 0, code  # the measured hour
        if code != "network":
            assert entry["hour_basis"] == ["borrowed"] * 24
            assert entry["fallback_reason"].startswith(document["borrowed"]["note"])
            assert document["schedule"][code] == entry["test_hour"]
    assert "| byteful (borrowed curve) |" in Path("formulas/SUMMARY.md").read_text()


def _donor(name, factors, daily, countries):
    def entry(values, own=True):
        curve_used = "country" if own else "network"
        return {"curve": curve_used, "hour_factors": values, "daily_factor": daily}

    return {
        "network": name,
        "network_curve": entry(factors),
        "countries": {code: entry(values, own) for code, (values, own) in countries.items()},
    }


def _one_hour_network():
    hour_ips = [0] * 24
    hour_ips[16] = 50
    status = ["requests_no_ips"] * 24
    status[16] = "present"
    countries = {"US": _country([40 if h == 16 else 0 for h in range(24)])}
    countries["FR"] = _country([10 if h == 16 else 0 for h in range(24)])
    return build_formula(_counts(hour_ips, countries, status=status), 2, 1, 20)


def test_borrow_gives_the_average_shape_anchored_on_the_measured_hour():
    from proxy_index.formula import borrow  # new module: the base code fails on import here

    rising = [(h + 1) / 24 for h in range(24)]
    falling = [1 - h / 48 for h in range(24)]
    flat = [1.0] * 24
    us = {"US": (falling, True)}
    donors = [
        _donor("a", rising, 3.0, us | {"FR": (rising, True)}),
        _donor("b", falling, 2.0, us),
        _donor("c", flat, 1.5, {"US": (falling, True), "FR": (flat, False)}),
    ]
    document = borrow.apply(_one_hour_network(), donors)

    normal = [[f / (sum(c) / 24) for f in c] for c in (rising, falling, flat)]
    s = [sum(n[h] for n in normal) / len(normal) for h in range(24)]
    network = document["network_curve"]
    peak, low = s.index(max(s)), s.index(min(s))
    assert (network["peak_hour"], network["low_hour"]) == (peak, low)
    assert network["hour_factors"] == pytest.approx([v / s[peak] for v in s])
    assert network["daily_factor"] == 2.0  # the median of 3.0, 2.0 and 1.5
    assert network["estimated_unique_ips"][16] == 50  # the measured hour keeps its real value
    assert network["estimated_unique_ips"] == pytest.approx([50 * v / s[16] for v in s])
    assert network["estimated_requests"][16] == 100 and network["unique_ips"][16] == 50
    us_entry, fr_entry = document["countries"]["US"], document["countries"]["FR"]
    # US: three donors have it on their own curve, so its own average shape (falling: peak 0).
    assert us_entry["curve"] == "country" and us_entry["test_hour"] == us_entry["peak_hour"] == 0
    assert us_entry["hour_factors"] == pytest.approx(falling)
    assert us_entry["estimated_unique_ips"][16] == 40
    assert us_entry["estimated_unique_ips"][0] == pytest.approx(40 * 1 / falling[16])
    # FR: one donor only, so the all-country shape.
    assert fr_entry["curve"] == "network" and fr_entry["hour_factors"] == network["hour_factors"]
    assert fr_entry["estimated_unique_ips"][16] == 10
    assert document["schedule"] == {"FR": peak, "US": 0}
    note = "borrowed: average of all networks, temporary until demo data is fixed"
    assert document["borrowed"]["note"] == note
    assert document["borrowed"]["networks"] == ["a", "b", "c"]
    assert us_entry["hour_basis"] == ["borrowed"] * 24
    assert us_entry["fallback_reason"] == f"{note} (3 networks have US on their own curve)"
    assert all(factor > 0 for factor in fr_entry["hour_factors"])
    with pytest.raises(FormulaError, match="no other network"):
        borrow.apply(_one_hour_network(), [])


def test_borrowed_daily_factor_is_the_donors_median_not_their_mean():
    from proxy_index.formula import borrow

    flat = [1.0] * 24

    def donor(name, daily, us_daily):
        document = _donor(name, flat, daily, {"FR": (flat, False)})
        document["countries"]["US"] = {"curve": "country", "hour_factors": flat}
        document["countries"]["US"]["daily_factor"] = us_daily
        return document

    # One outlier per curve: its day would pull a mean far up (6.875 and 3.8).
    donors = [donor("a", 2.0, 1.0), donor("b", 3.0, 1.2), donor("c", 2.5, 9.0)]
    donors.append(donor("d", 20.0, 4.0))
    document = borrow.apply(_one_hour_network(), donors)
    assert document["network_curve"]["daily_factor"] == 2.75  # median of 2.0, 2.5, 3.0, 20.0
    assert document["countries"]["US"]["daily_factor"] == 2.6  # its own donors: 1.0 to 9.0
    assert document["countries"]["FR"]["daily_factor"] == 2.75  # the all-country median


def test_byteful_daily_factors_are_the_donors_median():
    documents = {path.stem: json.loads(path.read_text()) for path in PUBLISHED}
    byteful = documents.pop("byteful")
    donors = [documents[name] for name in byteful["borrowed"]["networks"]]
    network = statistics.median(d["network_curve"]["daily_factor"] for d in donors)
    assert byteful["network_curve"]["daily_factor"] == network
    for code, entry in byteful["countries"].items():
        own = [d["countries"][code] for d in donors if code in d["countries"]]
        own = [other["daily_factor"] for other in own if other["curve"] == "country"]
        expected = statistics.median(own) if entry["curve"] == "country" else network
        assert entry["daily_factor"] == expected, code


def test_cli_build_borrows_only_for_the_named_network(tmp_path, capsys):
    counts = tmp_path / "counts"
    counts.mkdir()
    full = _counts([100 + h for h in range(24)], {"US": _country([50 + h for h in range(24)])})
    one = _counts([0] * 16 + [50] + [0] * 7, {"US": _country([0] * 16 + [40] + [0] * 7)})
    (counts / "full.json").write_text(json.dumps(full | {"network": "full"}))
    (counts / "one.json").write_text(json.dumps(one | {"network": "one"}))
    out = tmp_path / "out"
    args = ["build", "--aggregates", str(counts), "--out", str(out), "--version", "2"]
    assert cli.main([*args, "--min-peak-ips", "1", "--borrow", "one"]) == 0
    built = json.loads((out / "one.json").read_text())
    assert built["borrowed"]["networks"] == ["full"]
    assert "borrowed" not in json.loads((out / "full.json").read_text())
    assert built["network_curve"]["peak_hour"] == 23 and built["schedule"] == {"US": 23}
    assert "| one (borrowed curve) |" in (out / "SUMMARY.md").read_text()
    capsys.readouterr()
    assert cli.main([*args, "--borrow", "nope"]) == 1
    assert "--borrow names networks without counts: ['nope']" in capsys.readouterr().err
