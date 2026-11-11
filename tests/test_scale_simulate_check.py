"""SIM-CHECK-2: each rule of simulate_check on the synthetic profile of
test_scale_simulate_model.py: browser tries against the plan, bytes per load, the daily ratios,
unique IPs against the formulas, a day's rows with recurrence and the rows apart, the same-support
place distance and an insufficient reference; and the model's long tail."""

from __future__ import annotations

import copy
import importlib
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "scale"))
cases = importlib.import_module("test_scale_simulate_model")
check = importlib.import_module("simulate_check")
simulate_browser = importlib.import_module("simulate_browser")
Places = importlib.import_module("simulate_places").Places
model, spread, profile = cases.model, cases.spread, cases.profile


@pytest.fixture
def generated(tmp_path):
    """(profile with a reference of seed 1's days 1-2, seed 2's model, its days 1-2)."""
    built = cases.plan(tmp_path)
    given = cases.with_reference(profile(), cases.week(profile(), built, 1, days=(1, 2)))
    found = model.Model(given, built, 2)
    return given, found, [found.hour(day, h) for day in (1, 2) for h in range(24)]


def failed(found: dict, name: str) -> list[dict]:
    return [c for c in found["checks"] if c["check"] == name and not c["pass"]]


def named(found: dict, name: str) -> list[dict]:
    return [c for c in found["checks"] if c["check"] == name]


def test_browser_tries_are_checked_against_the_plan_beside_bp_verify_3(generated):
    given, built, hours = generated
    given = given | {"real_24h": {name: {"browser_tries": 30} for name in cases.NETWORKS}}
    found = check.size(given, hours, built)
    tries = named(found, "tries_per_run")
    assert len(tries) == len(cases.NETWORKS) * len(cases.TARGETS) and all(c["pass"] for c in tries)
    note = found["browser_tries_day"]
    assert note == note | {"generated": 400, "bp_verify_3": 60}  # 2 networks x 2 targets x 100
    assert "slot schedule" in note["schedules"]
    half = [
        SimpleNamespace(
            day=h.day, daily=h.daily, browser=[replace(r, tries=r.tries[:50]) for r in h.browser]
        )
        for h in hours
    ]
    assert len(failed(check.size(given, half, built), "tries_per_run")) == len(tries)


def test_bytes_per_load_follow_the_p50_then_the_targets_then_bp_verify_2(tmp_path):
    given, engine = profile(), cases.DEFAULT_ENGINE
    alpha = given["networks"]["alpha"]["browser"]
    alpha["google_search"][engine]["bytes_per_load"] = spread([1_000_000, 1_100_000, 9_000_000])
    alpha["amazon_pdp"][engine]["bytes_per_load"] = spread([0, 0, 0])  # no bytes recorded
    figure = simulate_browser.bytes_per_load
    assert figure(given, "alpha", "google_search", engine) == (1_100_000, "network")
    assert figure(given, "alpha", "amazon_pdp", engine) == (1_500_000, "target")
    assert figure({}, "alpha", "amazon_pdp", engine)[1] == "BP-VERIFY-2"
    built = model.Model(given, cases.plan(tmp_path), 4)
    hours = [built.hour(day, h) for day in range(1, 8) for h in range(24)]
    loads = named(check.size(given, hours, built), "bytes_per_load_p50")
    assert len(loads) == 4 and all(c["pass"] for c in loads)
    assert {c["basis"] for c in loads} == {"network", "target"}
    runs = [r for h in hours for r in h.browser if (r.network, r.target) == ("alpha", "amazon_pdp")]
    mean = sum(size for r in runs for _, size, *_ in r.tries) / sum(len(r.tries) for r in runs)
    assert mean == pytest.approx(1_500_000, rel=0.1)  # never the zero or the 2.79 MB default


def test_daily_ratios_and_unique_ips_against_the_reference_and_the_formulas(generated):
    given, built, hours = generated
    found = check.size(given, hours, built)
    ratios = named(found, "ratio")
    assert {c["metric"] for c in ratios} == {
        "observations_per_ip",
        "enrichment_per_ip",
        "buckets_per_run",
        "product_ips_per_ip",
        "pool_ips_per_ip",
    }  # no scope_ips: neither the model's tables nor the reference count them
    assert ratios and all(c["pass"] for c in ratios)
    ips = named(found, "unique_ips")
    assert len(ips) == 2 * len(cases.NETWORKS) and all(c["pass"] for c in ips)
    more = copy.deepcopy(given)
    rows = more["reference"]["daily"]["networks"]["beta"]["rows_per_run"]
    rows["ip_observations"]["mean"] *= 1.3
    assert [c["network"] for c in failed(check.size(more, hours, built), "ratio")] == ["beta"]
    larger = model.Model(profile(1.4), built.plan, 2)
    bigger = [larger.hour(day, h) for day in (1, 2) for h in range(24)]
    assert len(failed(check.size(given, bigger, larger), "unique_ips")) == len(ips)


def snapshot(rows: dict) -> dict:
    return {"tables": {f"public.{name}": {"rows": n} for name, n in rows.items()}}


def test_a_days_rows_count_recurrence_and_list_back_fills_apart(generated):
    given, built, hours = generated
    days = {day: [h for h in hours if h.day == day] for day in (1, 2)}
    one, two = (check.expected_rows(given, days[day], day) for day in (1, 2))
    tests = {day: [t for h in days[day] for t in h.daily] for day in (1, 2)}
    assert one["ip_addresses"] == sum(t.size for t in tests[1])
    assert two["ip_addresses"] == pytest.approx(sum(t.size * (1 - 0.45) for t in tests[2]), abs=1)
    assert two["ip_observations"] > two["ip_addresses"] and "run_buckets" not in two
    first = {name: one[name] for name in one if name != "scope_ips"}
    second = {name: first[name] + two[name] for name in first}
    second |= {"scope_ips": 1_000_000, "calculated_series": 500}  # v14's back-fill, a build
    sizes = {"0": snapshot(dict.fromkeys(first, 0)), "1": snapshot(first), "2": snapshot(second)}
    found = check.size(given, hours, built, sizes)
    rows = named(found, "day_rows")
    assert rows and all(c["pass"] for c in rows), [c for c in rows if not c["pass"]]
    assert ("2", "scope_ips") not in {(str(c["day"]), c["metric"]) for c in rows}
    assert found["apart"]["2"] == {
        "calculated_series": {"rows": 500, "why": "not the ingest's"},
        "scope_ips": {"rows": 1_000_000, "why": "new table: a back-fill"},
    }
    every = second | {"ip_addresses": first["ip_addresses"] + one["ip_addresses"]}
    sizes["2"] = snapshot(every)  # day 2's IPs counted as all new: the old expectation
    wrong = failed(check.size(given, hours, built, sizes), "day_rows")
    assert [(c["day"], c["metric"]) for c in wrong] == [(2, "ip_addresses")]
    text = check.markdown({"stats_day": 2, "size": found, "stats": check.verdict([])})
    assert "new table: a back-fill" in text and "BP-VERIFY-3" in text


def long_tail(code: str, tuples: int = 2000) -> dict:
    top = [
        {"country_code": code, "region": f"R{i % 7}", "city": f"City {i}", "asn": 64500 + i}
        | {"usage_type": "consumer", "asn_name": f"Org {i}", "share": 0.8 / tuples}
        for i in range(tuples)
    ]
    return {"ips": 50_000, "top": top, "tail_share": 0.195, "unmatched_share": 0.005}


def test_the_tail_is_drawn_from_the_profiles_2000_tuples():
    places = Places("US", long_tail("US"), 0.25)
    picks = Counter(places.pick(p, p) for p in range(20_000))
    top = sum(n for place, n in picks.items() if 0 <= place < 2000)
    assert top / 20_000 == pytest.approx(0.8, abs=0.002)
    assert len([place for place in picks if 500 <= place < 2000]) > 1400


def test_places_are_compared_on_the_same_support_with_a_top_and_a_tail(monkeypatch):
    shape = {"places": {"US": long_tail("US")}}
    places = Places("US", shape["places"]["US"], 0.25)
    built = SimpleNamespace(places=lambda network, code: places)
    measured = {"US": Counter(places.pick(p, p) for p in range(3000))}  # 1.5 IPs a tuple
    city = check.ATTRIBUTES["city"]
    assert check.place_distance(shape, built, "alpha", measured, city) < check.TVD
    monkeypatch.setattr(check, "TOP_K", 10_000)  # every tuple apart: the sample's noise
    assert check.place_distance(shape, built, "alpha", measured, city) > check.TVD


def test_an_insufficient_reference_is_reported_not_failed(generated):
    given, built, hours = generated
    shifted = copy.deepcopy(given)
    for entry in shifted["networks"].values():
        entry["places"] = {code: cases.places(code, rotate=2) for code in ("US", "DE")}
    day = hours[24:]
    places = [c for c in check.stats(shifted, day)["checks"] if c["check"] in check.ATTRIBUTES]
    assert places and not any(c["pass"] for c in places if c["check"] == "country")
    for entry in shifted["networks"].values():
        for own in entry["places"].values():
            own["ips"] = 2000  # 4,000 a network, under 5,000
    thin = [c for c in check.stats(shifted, day)["checks"] if c["check"] in check.ATTRIBUTES]
    assert thin and all(c["pass"] and c["insufficient_reference"] for c in thin)
