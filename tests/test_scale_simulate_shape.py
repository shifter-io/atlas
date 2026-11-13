"""SIM-MODEL-3: the daily tests' buckets per run and per IP follow the reference, DB-IP's release
comes once a month and only then re-enriches every IP (in the real ingest), a run's bytes per
load follow the spread's median and band, and a fragmented long tail takes its city and ASN
shares; on the synthetic profile of test_scale_simulate_model.py."""

from __future__ import annotations

import bisect
import copy
import importlib
import random
import sys
from collections import Counter
from datetime import date
from itertools import accumulate
from pathlib import Path
from types import SimpleNamespace

import pytest
from orchestrator_cases import prepare, query

from atlas.storage import ingest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "scale"))
cases = importlib.import_module("test_scale_simulate_model")
check = importlib.import_module("simulate_check")
shape = importlib.import_module("simulate_shape")
simulate_places = importlib.import_module("simulate_places")
model, spread, profile = cases.model, cases.spread, cases.profile

pytest_plugins = ["test_storage"]

# alpha's reference: runs by buckets, and per a run's buckets its IPs by the buckets seen in.
PER_RUN = {"1": 30, "2": 50, "5": 20}
PER_IP = {"1": {"1": 900}, "2": {"1": 800, "2": 200}, "5": {"1": 650, "2": 200, "3": 100, "5": 50}}


def calibrated(given: dict, releases=("2026-10-05",)) -> dict:
    """The profile with alpha's buckets and the reference runs' DB-IP releases."""
    found = copy.deepcopy(given)
    daily = found["reference"].setdefault("daily", {})
    daily["releases"] = list(releases)
    alpha = daily.setdefault("networks", {}).setdefault("alpha", {})
    alpha["buckets"] = {"per_run": PER_RUN, "per_ip": PER_IP}
    return found


def mean(counts: dict) -> float:
    return sum(int(k) * n for k, n in counts.items()) / sum(counts.values())


def test_buckets_per_run_and_per_ip_follow_the_reference(tmp_path):
    built = cases.plan(tmp_path)
    given = calibrated(cases.with_reference(profile(), cases.week(profile(), built, 1)))
    alpha = shape.Buckets(given, "alpha")
    draws = Counter(alpha.per_run((k + 0.5) / 1000) for k in range(1000))
    assert draws == Counter({int(k): n * 10 for k, n in PER_RUN.items()})
    assert Counter(alpha.per_ip(5, (k + 0.5) / 1000) for k in range(1000)) == Counter(
        {int(k): n for k, n in PER_IP["5"].items()}
    )
    assert alpha.per_ip(4, 0.99) == 4 and shape.Buckets({}, "beta").per_run(0.5) == 3
    found = model.Model(given, built, 2)
    hours = [found.hour(day, h) for day in range(1, 8) for h in range(24)]
    tests = [t for h in hours for t in h.daily if t.lane.network == "alpha"]
    assert {t.buckets for t in tests} <= {1, 2, 5} and len({t.buckets for t in tests}) == 3
    for test in (t for t in tests if t.size >= 500):  # observations per IP: the buckets' shares
        expected = mean(PER_IP[str(test.buckets)])
        assert test.successes[1] / test.size == pytest.approx(expected, rel=0.02)
    # The reference's ratios from the distributions alone, not from generated runs.
    rows = given["reference"]["daily"]["networks"]["alpha"]["rows_per_run"]
    ips = rows["ip_addresses"]["mean"]
    seen = sum(n * mean(PER_IP[b]) for b, n in PER_RUN.items()) / sum(PER_RUN.values())
    rows["run_buckets"]["mean"], rows["ip_observations"]["mean"] = mean(PER_RUN), ips * seen
    ratios = [c for c in check.size(given, hours, found)["checks"] if c["check"] == "ratio"]
    assert ratios and all(c["pass"] for c in ratios), [c for c in ratios if not c["pass"]]
    old = model.Model(given | {"reference": profile()["reference"]}, built, 2)  # 2 to 4 buckets
    hours = [old.hour(day, h) for day in range(1, 8) for h in range(24)]
    wrong = [c for c in check.size(given, hours, old)["checks"] if c["check"] == "ratio"]
    wrong = {c["metric"] for c in wrong if c["network"] == "alpha" and not c["pass"]}
    assert wrong == {"observations_per_ip", "buckets_per_run"}


def test_db_ips_release_comes_once_a_month():
    assert shape.release_date(date(2026, 11, 4), 5) == date(2026, 10, 5)
    assert shape.release_date(date(2026, 11, 5), 5) == date(2026, 11, 5)
    assert shape.release_date(date(2027, 1, 2), 5) == date(2026, 12, 5)
    assert shape.release_date(date(2027, 2, 28), 31) == date(2027, 2, 28)
    assert shape.release_day({}) == 1
    assert shape.release_day(calibrated({"reference": {}}, ("2026-09-01", "2026-10-05"))) == 5


def test_a_new_release_re_enriches_every_ip_only_on_its_day(database, tmp_path):
    """A week of alpha's US test through the real ingest: ip_enrichment grows by the new IPs,
    and by every IP on day 4 (5 November, the reference's release day), as the check expects."""
    prepare(database, tuple(cases.SCHEDULE))
    given = calibrated(profile())
    built = model.Model(given, cases.plan(tmp_path), 3)
    assert [built.renewed(day) for day in range(1, 8)] == [True, False, False, True] + [False] * 3
    assert len({built.release(day) for day in range(1, 8)}) == 2
    lane, before = built.lanes["alpha", "US"], 0
    for day in range(1, 8):
        test = built.daily(lane, day)
        directory = tmp_path / f"day-{day}"
        directory.mkdir()
        test.write(directory)
        assert ingest(directory, database)["imported"]
        rows = query(database, "SELECT count(*) FROM ip_enrichment")[0][0]
        fresh = test.size - test.recurring
        assert rows - before == (test.size if day in (1, 4) else fresh), day
        hour = SimpleNamespace(daily=[test], browser=[])
        expected = check.expected_rows(given, [hour], day)["ip_enrichment"]
        assert expected == pytest.approx(rows - before, rel=0.02)
        before = rows
    files = query(database, "SELECT metadata->>'file' FROM dbip_releases ORDER BY 1")
    assert [name for (name,) in files] == ["dbip-full-2026-10-05.mmdb", "dbip-full-2026-11-05.mmdb"]


def test_a_runs_bytes_per_load_take_the_spreads_median_and_band(tmp_path):
    given, engine = profile(), cases.DEFAULT_ENGINE
    wide = spread([500_000] * 2 + [1_000_000] * 6 + [2_000_000] * 2)  # p10, p50, p90
    given["networks"]["alpha"]["browser"]["google_search"][engine]["bytes_per_load"] = wide
    built = model.Model(given, cases.plan(tmp_path), 4)
    hours = [built.hour(day, h) for day in range(1, 8) for h in range(24)]
    runs = [r for h in hours for r in h.browser if r.network == "alpha"]
    loads = [r.bytes_per_load for r in runs if r.target == "google_search"]
    assert len(loads) == 7 and spread(loads)["p50"] == pytest.approx(1_000_000, rel=0.001)
    assert len([n for n in loads if 500_000 <= n <= 2_000_000]) == 5  # 5 of 7 sevenths
    assert max(loads) / min(loads) > 3  # not the p50 scaled by a day's jitter
    loads = [c for c in check.size(given, hours, built)["checks"] if "bytes" in c["check"]]
    assert len(loads) == 4 and all(c["pass"] for c in loads)


def fragmented(cities: int = 300, asns: int = 8) -> dict:
    """A long-tailed country: each city's IPs over several ASNs, the tuples by share (as the
    profile ranks them), so a city's tuples are far apart in that order."""
    weights = {
        (c, a): (1 + (c * 7 + a) % 5) / ((c + 1) * (a + 1) ** 1.5)
        for c in range(cities)
        for a in range(asns)
    }
    total = sum(weights.values()) / 0.9
    top = [
        {"country_code": "US", "region": f"R{c % 9}", "city": f"City {c}", "asn": 64500 + a}
        | {"usage_type": "consumer", "asn_name": f"Org {a}", "share": round(w / total, 6)}
        for (c, a), w in sorted(weights.items(), key=lambda item: -item[1])
    ]
    return {"ips": 50_000, "top": top, "tail_share": 0.095, "unmatched_share": 0.005}


def distances(places, shape: dict, picks: list[int]) -> list[float]:
    """The check's city and ASN distances of these picks from the profile's places."""
    built = SimpleNamespace(places=lambda network, code: places)
    measured = {"US": Counter(picks)}
    return [
        check.place_distance(shape, built, "alpha", measured, check.ATTRIBUTES[name])
        for name in ("city", "asn")
    ]


def a_real_day(places, shape: dict, ips: int) -> list[float]:
    """The mean distances of 20 days whose IPs are independent draws from the profile's
    tuples: the noise a real day of that many IPs has."""
    found = []
    for seed in range(20):
        rng, picks = random.Random(seed), []
        for _ in range(ips):
            i = bisect.bisect_right(places.flat, rng.random())  # past the tail: unmatched
            picks.append(i if i <= len(places.top) else -1)
        found.append(distances(places, shape, picks))
    return [sum(own[k] for own in found) / len(found) for k in (0, 1)]


@pytest.mark.parametrize("ips", [300, 800, 3000])
def test_a_fragmented_tail_takes_its_city_and_asn_shares(monkeypatch, ips):
    """City first, then the tuple inside the city by ASN rank: the city distance falls below
    the single-level draw's (each tuple its own slice) and both stay within a real day's."""
    shape = {"places": {"US": fragmented()}}
    places = simulate_places.Places("US", shape["places"]["US"], 0.25)
    positions = range(1000, 1000 + ips)
    city, asn = distances(places, shape, [places.pick(p, p) for p in positions])
    every = ("country_code", "region", "city", "asn", "usage_type")
    monkeypatch.setattr(simulate_places, "FIRST", every)
    single = simulate_places.Places("US", shape["places"]["US"], 0.25)
    before = distances(single, shape, [single.pick(p, p) for p in positions])
    flat = list(accumulate(place["share"] for place in single.top))
    single.flat = [*flat, flat[-1] + shape["places"]["US"]["tail_share"]]
    real = a_real_day(single, shape, ips)
    assert city < before[0] and 0 < city < real[0] and 0 < asn < real[1]
    assert city < check.TVD and asn < check.TVD


def test_an_ip_keeps_its_real_tuple_from_day_to_day(tmp_path):
    given = profile()
    given["networks"]["alpha"]["places"]["US"] = fragmented()
    built = model.Model(given, cases.plan(tmp_path), 5)
    lane = built.lanes["alpha", "US"]
    days = [{n: place for n, _, _, place in built.daily(lane, day).ips()} for day in (1, 2)]
    recurring = days[0].keys() & days[1].keys()
    assert len(recurring) > 500 and all(days[0][n] == days[1][n] for n in recurring)
    top = given["networks"]["alpha"]["places"]["US"]["top"]
    places = built.places("alpha", "US")
    for city in places.cities:  # a city's tuples: the profile's own, of that one city
        assert len({tuple(top[i][name] for name in simulate_places.CITY) for i in city}) == 1
    picked = [place for own in days for place in own.values() if 0 <= place < len(top)]
    assert picked and {(top[i]["city"], top[i]["asn"]) for i in picked} <= {
        (place["city"], place["asn"]) for place in top
    }
