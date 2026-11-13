"""Hour formulas: the public formula built from verified counts, its curves and its summary."""

from __future__ import annotations

import json

import pytest
from formula_cases import RUN, _country, _counts

from atlas.formula.aggregate import FormulaError
from atlas.formula.build import build_formula, curve, dumps, summary_markdown


def test_factors_reproduce_counts_and_peak_is_one():
    hour_ips = [97 + 13 * hour for hour in range(24)]
    us = [40 + (hour * 7) % 31 for hour in range(24)]
    document = build_formula(_counts(hour_ips, {"US": _country(us, day=500)}, day=2345), 3, 30, 20)
    network = document["network_curve"]
    peak = network["unique_ips"][network["peak_hour"]]
    assert network["hour_factors"][network["peak_hour"]] == 1.0
    assert [round(factor * peak) for factor in network["hour_factors"]] == hour_ips
    assert round(network["daily_factor"] * peak) == 2345
    entry = document["countries"]["US"]
    own_peak = us[entry["peak_hour"]]
    assert entry["curve"] == "country" and entry["fallback_reason"] is None
    assert entry["hour_factors"][entry["peak_hour"]] == 1.0
    assert [round(factor * own_peak) for factor in entry["hour_factors"]] == us
    assert round(entry["daily_factor"] * own_peak) == 500
    assert document["schedule"] == {"US": entry["peak_hour"]} == {"US": entry["test_hour"]}
    assert document["formula_version"] == 3
    assert document["latency"]["available"] is False


def test_tie_breaks_prefer_requests_then_earliest_hour():
    assert curve([5, 5, 5, 1, 1], [1, 4, 4, 2, 1], [True] * 5, 9)["peak_hour"] == 1
    result = curve([5, 1, 1], [1, 2, 2], [True] * 3, 6)
    assert (result["peak_hour"], result["low_hour"]) == (0, 1)


def test_hours_without_ips_are_no_data_with_a_reason_never_zero():
    hour_ips = [0] * 24
    hour_ips[16] = 50
    status = ["requests_no_ips"] * 24
    status[16] = "present"
    status[2] = "missing"
    countries = {"US": _country(hour_ips), "FR": _country([1] * 24)}
    document = build_formula(_counts(hour_ips, countries, status=status), 1, 30, 20)
    network = document["network_curve"]
    # Byteful's case: requests answered, no exit IP reported. No data, not a pool of 0 IPs.
    assert network["peak_hour"] == network["low_hour"] == 16
    assert network["hour_factors"][2] is None and network["hour_factors"][0] is None
    assert network["no_data_reasons"][0].startswith("no data: requests were answered")
    assert network["no_data_reasons"][2].startswith("no data: no request")
    assert network["no_data_reasons"][16] is None
    assert network["hours_with_ips"] == 1 and network["status"][0] == "requests_no_ips"
    for code in ("US", "FR"):
        factors = document["countries"][code]["hour_factors"]
        assert factors[16] == 1.0 and factors.count(None) == 23
    # The hour floor is capped at the network's own hours with IPs.
    assert document["countries"]["US"]["curve"] == "country"


def test_sparse_countries_fall_back_to_the_network_curve():
    hour_ips = [100 + hour for hour in range(24)]
    few_ips = [3] * 24
    few_hours = [0] * 12 + [80] * 12
    countries = {"FR": _country(few_ips), "JP": _country(few_hours)}
    document = build_formula(_counts(hour_ips, countries), 1, 30, 20)
    network = document["network_curve"]
    for code, reason in (("FR", "peak hour has 3"), ("JP", "IPs in 12 hours")):
        entry = document["countries"][code]
        assert entry["curve"] == "network" and reason in entry["fallback_reason"]
        assert entry["hour_factors"] == network["hour_factors"]
        assert entry["test_hour"] == network["peak_hour"] == 23
        assert entry["unique_ips"] == countries[code]["unique_ips"]
    assert document["countries"]["JP"]["peak_hour"] == 12


def test_a_country_hour_without_requests_or_with_too_few_takes_the_network_curve():
    hour_ips = [1000 + 10 * hour for hour in range(24)]
    us = [200 + hour for hour in range(24)]
    us[8] = 0  # no request row of US in hour 8 (REAL-CYCLE-1: factor 0.0)
    ca = [300] * 24
    ca[18], ca[5] = 20, 120  # request rows 60 and 360 against a median of 900
    countries = {"US": _country(us), "CA": _country(ca)}
    document = build_formula(_counts(hour_ips, countries), 2, 30, 20)
    # Every factor of a covered hour rests on a measurement or the network curve: none is 0.
    for code in ("US", "CA"):
        factors = document["countries"][code]["hour_factors"]
        assert all(factor > 0.2 for factor in factors), (code, factors)
    assert document["rules"]["min_sample_share"] == 0.25
    assert document["rules"]["low_sample_share"] == 0.5
    entry = document["countries"]["US"]
    assert entry["curve"] == "country" and entry["hour_basis"][8] == "network_missing"
    kept = [hour for hour in range(24) if hour != 8]
    level = sum(us[hour] for hour in kept) / sum(hour_ips[hour] for hour in kept)
    assert entry["hour_factors"][8] == pytest.approx(hour_ips[8] * level / us[23])
    assert entry["low_hour"] == 0 and entry["peak_hour"] == entry["test_hour"] == 23
    entry = document["countries"]["CA"]
    assert entry["hour_basis"][18] == "network_low_sample"
    assert entry["hour_basis"][5] == "country_low_sample"
    assert entry["hour_factors"][5] == pytest.approx(120 / 300)  # flagged, own count kept
    kept = [hour for hour in range(24) if hour != 18]
    level = sum(ca[hour] for hour in kept) / sum(hour_ips[hour] for hour in kept)
    assert hour_ips[18] * level / 300 > 1.0  # never above the measured peak
    assert entry["hour_factors"][18] == 1.0
    assert entry["low_hour"] == 5  # a fallback hour is neither peak nor low
    assert entry["hour_basis"].count("country") == 22


def test_hour_basis_follows_the_network_and_the_country_curve_used():
    hour_ips = [0] * 24
    hour_ips[16] = 50
    status = ["requests_no_ips"] * 24
    status[16], status[2] = "present", "missing"
    countries = {"US": _country(hour_ips), "FR": _country([1] * 24)}
    document = build_formula(_counts(hour_ips, countries, status=status), 1, 30, 20)
    basis = [None] * 24
    basis[16] = "country"
    assert document["countries"]["US"]["hour_basis"] == basis
    basis[16] = "network"
    assert document["countries"]["FR"]["hour_basis"] == basis


def test_build_rejects_short_or_empty_curves():
    with pytest.raises(FormulaError, match="24 hours"):
        build_formula({**_counts([1] * 24, {}), "hours": []}, 1, 30, 20)
    with pytest.raises(FormulaError, match="no unique IP"):
        curve([0, 0], [1, 1], [True, True], 0)
    with pytest.raises(FormulaError, match="no measured"):
        curve([1], [1], [False], 0)


def test_dumps_keeps_scalar_lists_on_one_line():
    text = dumps({"a": [1, 2, None], "b": [[RUN], []], "c": {"d": "x"}})
    assert '"a": [1, 2, null]' in text
    assert f'["{RUN}"]' in text
    assert json.loads(text) == {"a": [1, 2, None], "b": [[RUN], []], "c": {"d": "x"}}
    assert text.endswith("}\n")


def test_summary_lists_each_network():
    document = build_formula(
        _counts([10 + h for h in range(24)], {"US": _country([1] * 24)}), 2, 30, 20
    )
    text = summary_markdown([document])
    row = [line for line in text.splitlines() if line.startswith("| demo |")][0]
    assert "| 2 | 24 | 0 | 0 | 23 | 00 |" in row and RUN in row
