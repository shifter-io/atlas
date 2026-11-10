"""SIM-OVERLAP-1: the profile counts the IPs networks share (counts and shares only), the model
shares IPs between the profile's pairs only, at its shares and with its recurrence, and
simulate_check compares the pair shares (3 networks, 2 shared pairs)."""

from __future__ import annotations

import copy
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "scale"))
cases = importlib.import_module("test_scale_simulate_model")
profiles = importlib.import_module("test_scale_profile")
check = importlib.import_module("simulate_check")
profile_hours = importlib.import_module("profile_hours")
profile_overlap = importlib.import_module("profile_overlap")
model = cases.model

NETWORKS = ("alpha", "beta", "gamma")
JP = {"alpha": 1200, "beta": 800, "gamma": 1000}  # each network's JP IPs at its test hour
# alpha-beta: measured in US, none in DE (both requested it), the network's figure in JP (which
# neither requested); alpha-gamma: the network's figure, so only in JP; beta-gamma: none.
PAIRS = [
    {
        "networks": ["alpha", "beta"],
        "shared": 30_000,
        "shares": {"alpha": 0.2, "beta": 0.3},  # 240 of alpha's 1,200 JP IPs, of beta's 800
        "countries": {"US": {"shared": 2000, "shares": {"alpha": 0.25, "beta": 0.25}}},
    },
    {
        "networks": ["alpha", "gamma"],
        "shared": 5000,
        "shares": {"alpha": 0.1, "gamma": 0.12},  # 120 of 1,200 and of 1,000
        "countries": {},
    },
]


def profile(pairs=PAIRS) -> dict:
    found = cases.profile()
    found["networks"]["gamma"] = copy.deepcopy(found["networks"]["alpha"])
    for name, ips in JP.items():
        hour = found["networks"][name]["hours"][cases.SCHEDULE["JP"]]
        hour["countries"]["JP"] = {"unique_ips": ips, "request_rows": 2 * ips}
    return found | {"overlap": {"pairs": copy.deepcopy(pairs)}}


def plan(tmp_path) -> model.Plan:
    path = tmp_path / "browser-settings.toml"
    path.write_text(cases.SETTINGS)
    formulas = {name: cases.formula(name) for name in NETWORKS}
    return model.Plan(formulas, model.load_settings(path), cases.START, regions=cases.REGIONS)


def addresses(found: model.Model, day: int) -> dict:
    """(network, country) -> the set of IP indexes of its test of the day."""
    return {key: {n for n, *_ in found.daily(lane, day).ips()} for key, lane in found.lanes.items()}


def test_the_profile_counts_shared_ips_per_pair_and_country_without_an_address(tmp_path):
    root = tmp_path / "enrich"
    profiles.write_network(root, "alpha", profiles.ALPHA)
    gamma = [("192.0.2.1", {"US": 1}, None), ("192.0.2.10", {"US": 2}, None)]
    gamma += [("2001:db8::7", {"DE": 1}, None)]
    profiles.write_network(root, "gamma", {"05": ("2026-01-02T05:00:00Z", gamma)})
    beta = {"16": ("2026-01-01T16:00:00Z", [("2001:db8::9", {"US": 2}, None)])}
    profiles.write_network(root, "beta", beta)
    days = {}
    for name in ("alpha", "beta", "gamma"):
        network = profile_hours.Network(root / name)
        network.read()
        network.summary()
        days[name] = network.days
    found = profile_overlap.overlap(days)
    # alpha: 9 distinct IPs, 6 for US; gamma: 3, 2 for US. They share .1 (US in both) and .10
    # (DE for alpha, US for gamma: shared over the network only). beta shares none.
    (pair,) = found["pairs"]
    assert pair == {
        "networks": ["alpha", "gamma"],
        "shared": 2,
        "shares": {"alpha": 0.222222, "gamma": 0.666667},
        "countries": {"US": {"shared": 1, "shares": {"alpha": 0.166667, "gamma": 0.5}}},
    }
    text = json.dumps(found)
    assert not any(ip in text for ip in profiles.ADDRESSES + ["2001:db8::7"])
    assert profile_overlap.overlap({}) == {"basis": profile_overlap.BASIS, "pairs": []}


def test_the_generated_overlap_matches_the_profile_and_no_other_pair_shares(tmp_path):
    found = model.Model(profile(), plan(tmp_path), 3)
    for day in (1, 2, 3):
        ips = addresses(found, day)
        for country in cases.SCHEDULE:
            beta_gamma = ips["beta", country] & ips["gamma", country]
            assert not beta_gamma, "a pair the profile lacks"
            for name in NETWORKS:  # never across countries
                others = set().union(*(v for (n, c), v in ips.items() if c != country))
                assert not ips[name, country] & others
        assert not ips["alpha", "DE"] & ips["beta", "DE"]  # both requested DE, none shared
        assert not ips["alpha", "US"] & ips["gamma", "US"]  # the network's figure: not in US
        for (a, b), country, share in (
            (("alpha", "beta"), "US", (0.25, 0.25)),
            (("alpha", "beta"), "JP", (0.2, 0.3)),
            (("alpha", "gamma"), "JP", (0.1, 0.12)),
        ):
            shared = len(ips[a, country] & ips[b, country])
            for name, expected in zip((a, b), share, strict=True):
                assert shared / len(ips[name, country]) == pytest.approx(expected, rel=0.12)
        for key, lane in found.lanes.items():
            test = found.daily(lane, day)
            assert len(ips[key]) == test.size == sum(block[2] for block in test.blocks)


def test_shared_ips_recur_and_a_test_keeps_its_recurrence(tmp_path):
    found = model.Model(profile(), plan(tmp_path), 4)
    days = {day: addresses(found, day) for day in (1, 2, 3)}
    for day in (2, 3):
        for key, lane in found.lanes.items():
            before, now = days[day - 1][key], days[day][key]
            assert len(before & now) == found.daily(lane, day).recurring
            assert len(before & now) / len(now) == pytest.approx(0.45, abs=0.01)
        shared = days[day]["alpha", "US"] & days[day]["beta", "US"]
        earlier = days[day - 1]["alpha", "US"] & days[day - 1]["beta", "US"]
        assert len(shared & earlier) / len(shared) == pytest.approx(0.45, abs=0.01)
    shared = days[1]["alpha", "JP"] & days[1]["gamma", "JP"]
    test = found.daily(found.lanes["alpha", "JP"], 1)
    picks = {n: place for n, _, _, place in test.ips() if n in shared}
    later = found.daily(found.lanes["alpha", "JP"], 2)
    assert all(picks[n] == place for n, _, _, place in later.ips() if n in picks)  # stable


def test_the_check_passes_the_generated_pairs_and_fails_shifted_ones(tmp_path):
    given, built = profile(), plan(tmp_path)
    found = model.Model(given, built, 5)
    day = [found.hour(2, h) for h in range(24)]
    checks = check.overlap(given, day)
    groups = {(c["network"], c["other"], c["country"]) for c in checks}
    assert groups == {
        ("alpha", "beta", "US"),
        ("beta", "alpha", "US"),
        ("alpha", "beta", None),
        ("beta", "alpha", None),
        ("alpha", "gamma", None),
        ("gamma", "alpha", None),
    }
    assert all(c["pass"] and not c.get("insufficient_reference") for c in checks), checks
    stated = check.stats(given, day)
    assert stated["pass"], [c for c in stated["checks"] if not c["pass"]]
    assert "overlap alpha beta US" in [check.label(c) for c in checks]
    shifted = copy.deepcopy(PAIRS)
    shifted[0]["countries"]["US"]["shares"] = {"alpha": 0.4, "beta": 0.4}
    failed = [c for c in check.overlap(profile(shifted), day) if not c["pass"]]
    assert {(c["network"], c["country"]) for c in failed} == {("alpha", "US"), ("beta", "US")}
    shifted[0]["countries"]["US"]["shared"] = 999  # under 1,000: reported, never failed
    thin = [c for c in check.overlap(profile(shifted), day) if c["country"] == "US"]
    assert thin and all(c["pass"] and c["insufficient_reference"] for c in thin)
    assert check.overlap(cases.profile(), day) == []


def test_a_days_new_ip_rows_count_a_shared_ip_once(tmp_path):
    given, built = profile(), plan(tmp_path)
    found = model.Model(given, built, 6)
    hours = {day: [found.hour(day, h) for h in range(24)] for day in (1, 2)}
    distinct = {day: set().union(*addresses(found, day).values()) for day in (1, 2)}
    one = check.expected_rows(given, hours[1], 1)
    assert one["ip_addresses"] == one["ip_enrichment"] == len(distinct[1])
    tests = [t for hour in hours[1] for t in hour.daily]
    assert one["product_ips"] == sum(t.size for t in tests) > len(distinct[1])
    two = check.expected_rows(given, hours[2], 2)
    assert two["ip_addresses"] == pytest.approx(len(distinct[2] - distinct[1]), rel=0.02)
    assert two["ip_enrichment"] == two["ip_addresses"]  # no new DB-IP release on day 2


def test_a_lane_whose_shares_add_up_to_more_than_one_is_scaled_down(tmp_path):
    pairs = copy.deepcopy(PAIRS)
    pairs[0]["shares"] = {"alpha": 0.9, "beta": 0.9}
    pairs[1]["shares"] = {"alpha": 0.6, "gamma": 0.6}
    found = model.Model(profile(pairs), plan(tmp_path), 7)
    pools = [pool for pool, _ in found.pools["alpha", "JP"]]
    assert [pool.shares[0] for pool in pools] == pytest.approx([0.6, 0.4])
    assert sum(pool.room[0] for pool in pools) == pytest.approx(1)
    test = found.daily(found.lanes["alpha", "JP"], 1)
    assert test.blocks[0][2] >= 0 and sum(block[2] for block in test.blocks) == test.size
