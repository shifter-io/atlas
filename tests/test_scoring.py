"""Scoring from every request: time-weighted success and latency, latency under load, per-IP
shares without DB-IP unknowns (methodology files: test_scoring_methodology.py)."""

from collections import Counter
from datetime import timedelta

import pytest
from scoring_cases import HALF_HOUR, T, census_run, observation, row

from atlas.calculated.aggregate import (
    LIGHT_CENSUS,
    UNDER_LOAD_CENSUS,
    UNDER_LOAD_LIGHT,
    UNDER_LOAD_NONE,
    blend,
    census_overlaps,
    census_split,
    latency_detail,
    metrics,
    pool,
    product_inputs,
    timed,
    under_load,
    with_under_load,
)
from atlas.calculated.inputs import light_load, under_load_run
from atlas.calculated.latency import KEYS, blend_mean
from atlas.calculated.policy import load
from atlas.stats import Histogram


def test_blend_normalises_each_slot_and_never_averages_percentiles():
    one = Counter({100: 99, 1000: 1})
    assert blend([one]) == Histogram(one).percentiles()
    # A busy slot (1,000 successes at 100 ms) and a quiet one (40 at 900 ms) weigh the same:
    # the merged p50 is the busy slot's value, p90 the quiet one's. Request-weighting would
    # put p90 at 100 ms; averaging the two medians would invent 500 ms.
    busy, quiet = Counter({100: 1000}), Counter({900: 40})
    assert blend([busy, quiet]) == {"p50": 100, "p90": 900, "p95": 900, "p99": 900}
    assert Histogram(busy + quiet).percentiles()["p90"] == 100
    assert blend([]) == dict.fromkeys(("p50", "p90", "p95", "p99"))


def test_blend_mean_weighs_each_histogram_the_same():
    one = Counter({100: 99, 1000: 1})
    assert blend_mean([one]) == Histogram(one).mean() == 109.0
    # Means 100, 900 and 300 ms, whatever their volumes: (100 + 900 + 300) / 3.
    three = [Counter({100: 1000}), Counter({900: 40}), Counter({200: 1, 400: 1})]
    assert blend_mean(three) == 433.3
    assert blend_mean([]) is None and blend_mean([Counter()]) is None


def test_success_rate_is_the_mean_of_time_slots():
    rows = [
        row(T, 100, n=1000, successes=1000),  # a census-sized slot, all good
        row(T + HALF_HOUR, 100, n=100, successes=50),  # a pulse slot at half
        row(T + 2 * HALF_HOUR, 100, n=10, successes=5, test="census"),
    ]
    # A done pulse whose artifact never arrived: its observed live counters (100 requests, all
    # failed: the provider was down) vote 0. One that observed nothing does not vote at all.
    pulses = [
        {"slot": T + 3 * HALF_HOUR, "requests": 100, "successes": 0, "counted": False},
        {"slot": T + 4 * HALF_HOUR, "requests": 0, "successes": 0, "counted": False},
    ]
    m = timed(rows, 24, 30, pulses, 30)
    assert m["success_rate"] == pytest.approx((1 + 0.5 + 0.5 + 0) / 4)
    assert m["request_success_rate"] == pytest.approx(1055 / 1110)
    assert m["time_slots"] == {
        "minutes": 30,
        "success": 4,
        "down": 1,
        "skipped_during_census": 0,
        "latency": 2,
        "latency_targets": ["generic_http"],
        "min_latency_successes": 30,
    }
    # Light-load latency: pulses only; the census slot (and slots under 30 successes) do not vote.
    assert m["latency_ms"]["p50"] == 100
    light = timed([row(T, 100), row(T + HALF_HOUR, 800, n=29)], 24, 30)
    assert light["latency_ms"]["p50"] == 100 and light["time_slots"]["latency"] == 1


def test_light_load_latency_is_per_target_and_equal_weight():
    """http and https are different round trips: each target's light-load latency stands on
    its own, and a window over both is their equal-weight mixture, whatever their volumes."""
    rows = [
        row(T, 800, n=100),
        row(T, 1600, n=40, target="generic_https"),
    ]
    both = timed(rows, 24, 30)
    assert both["time_slots"]["latency_targets"] == ["generic_http", "generic_https"]
    assert both["latency_ms"]["p50"] == 800 and both["latency_ms"]["p90"] == 1600
    assert both["latency_ms"]["mean"] == 1200.0  # equal weight, not 1,028.6 by request
    assert timed(rows[:1], 24, 30)["latency_ms"]["p50"] == 800
    assert timed(rows[1:], 24, 30)["latency_ms"]["p50"] == 1600


def test_under_load_is_the_daily_tests_and_normal_requests_are_the_pulses():
    """Under load: the latency target's runs at load concurrency (daily tests outside a batch,
    census and load runs); normal requests: its pulses (concurrency 25). Each is measured on its
    own, every slot with enough successes weighing the same."""
    rows = [
        row(T, 600, country="RO", region="fra1"),
        row(T, 900, n=1000, test="load", country="RO", region="fra1"),
        row(T, 800, country="BG", region="fra1"),
        row(T + HALF_HOUR, 1200, n=1000, test=None, country="BG"),
        row(T + 2 * HALF_HOUR, 1500, n=1000, test="census", country="US"),
        # Another target's load run is not under load: latency is the latency target's alone.
        row(T, 9000, n=1000, test="load", target="generic_https"),
    ]
    m = with_under_load(timed(rows, 24, 30), rows, 30)
    assert m["latency_ms"]["p50"] == 600 and m["latency_ms"]["p90"] == 800
    assert m["time_slots"]["latency"] == 1
    assert m["latency_under_load_ms"]["p50"] == 1200
    assert m["latency_under_load_ms"]["mean"] == pytest.approx((900 + 1200 + 1500) / 3)
    assert m["under_load"] == {"slots": 3, "reason": None}
    # Request-weighted metrics() leave the requests under load out of its latency too.
    assert metrics(rows[:4], 24)["latency_ms"]["p99"] == 800
    choices = [
        {
            "day": "2026-10-03",
            "region": "fra1",
            "country": "RO",
            "basis": "fastest",
            "p50_ms": 610,
            "successes": 4000,
            "candidates": 2,
        }
    ]
    detail = latency_detail(rows[:5], 30, choices)
    countries = {c["country"]: c for c in detail["countries"]}
    assert countries["BG"]["region"] == "fra1"
    assert countries["BG"]["latency_ms"]["p50"] == 800
    assert countries["BG"]["latency_under_load_ms"]["p50"] == 1200
    assert countries["RO"]["latency_ms"]["p50"] == 600
    assert countries["RO"]["latency_under_load_ms"]["p50"] == 900
    assert countries["US"]["region"] is None and countries["US"]["latency_ms"]["p50"] is None
    assert countries["US"]["latency_under_load_ms"]["p50"] == 1500
    assert detail["regions"] == [
        {
            "region": "fra1",
            "reference": {"country": "RO", "target": "generic_http", "concurrency": 1000},
            "chosen": [{k: v for k, v in choices[0].items() if k != "region"}],
        }
    ]


def test_a_daily_test_of_the_latency_target_is_under_load_and_a_pulse_is_not():
    """The calculator's run flags (inputs): a daily test (a run outside a batch), a census and
    a load run of the latency target are under load; its pulses are the normal requests; a run
    during a census and another target's run are neither."""

    def run(test, target="generic_http", key=1):
        return {"run_key": key, "test": test, "scope": {"target": {"id": target}}}

    flagged = {9: [5]}
    cases = {
        "daily": (run(None), True),
        "census": (run("census"), True),
        "load": (run("load"), True),
        "pulse": (run("pulse"), False),
        "load during a census": (run("load", key=9), False),
        "another target's daily test": (run(None, "generic_https"), False),
    }
    for name, (r, want) in cases.items():
        assert under_load_run(r, flagged, "generic_http") is want, name
    # A daily test stays light for the breakdowns' latency, as before (inputs.light_load()).
    assert light_load(run(None), flagged, "generic_http")


def test_under_load_says_why_when_it_has_no_value():
    pulses = [row(T, 600), row(T + HALF_HOUR, 700)]
    assert under_load(pulses, 30) == {
        "ms": dict.fromkeys(KEYS),
        "slots": 0,
        "reason": UNDER_LOAD_NONE,
    }
    few = [*pulses, row(T, 1200, n=10, test=None)]
    assert under_load(few, 30)["reason"] == UNDER_LOAD_LIGHT
    assert under_load(few, 10)["ms"]["p50"] == 1200


def test_shares_are_per_ip_and_leave_db_ip_unknowns_out():
    observations = [
        observation(1, "RO", "ro"),
        observation(1, "RO", "ro"),  # the same IP again: counted once
        observation(2, "BG", "ro", usage="hosting"),
        observation(3, "RO", "ro", usage="datacenter"),
        observation(4, None, "ro", usage=None),  # no DB-IP record: out of every share
        observation(5, "RO", None),  # asked for no country: out of geo accuracy
    ]
    counts, _ = pool(observations, T - timedelta(hours=24), 24)
    assert counts["unique_ips"] == 5
    assert counts["geo_accuracy"] == pytest.approx(2 / 3)
    assert counts["datacenter_asn_share"] == pytest.approx(2 / 4)
    assert counts["classification_coverage"] == pytest.approx(4 / 5)
    assert counts["enrichment_coverage"] == pytest.approx(4 / 5)
    # Incomplete enrichment no longer blocks diversity, dcAsns or authenticity.
    counts["daily"] = {"unique_ips": 5, "countries": 2, "regions": 0, "cities": 0, "asns": 4}
    config, _ = load("configs/methodology.toml")
    inputs = product_inputs(
        counts, {"lowest_per_gb": 0.75, "basis_per_gb": 0.75}, config, "residential"
    )
    assert inputs["diversity"]["state"] == inputs["dcAsns"]["state"] == "ready"
    assert inputs["dcAsns"]["value"] == pytest.approx(0.5)
    assert inputs["authenticity"]["value"] == pytest.approx(200 / 3)
    assert inputs["value"]["value"] == 0.75
    # A datacenter product keeps 100 on dcAsns.
    datacenter = product_inputs(counts, {}, config, "datacenter")["dcAsns"]
    assert datacenter["fixed"] == 100 and datacenter["state"] == "ready"


def test_census_overlap_detection_any_country_same_product():
    pulse = {
        "product_id": 1,
        "batch_id": 50,
        "run_key": 7,
        "started_at": T,
        "finished_at": T + timedelta(minutes=1),
    }
    census = [
        census_run(1, T - timedelta(minutes=10), 10),  # ends as the pulse starts: no overlap
        census_run(2, T - timedelta(minutes=10), 10.5),  # still running at its start
        census_run(3, T + timedelta(seconds=59), 20),  # starts before it ends (another country)
        census_run(4, T + timedelta(minutes=1), 20),  # starts as it ends: no overlap
        census_run(5, T - timedelta(hours=3), 300),  # a long one still running
        census_run(6, T, 5, product=2),  # another product's census never counts
    ]
    assert census_overlaps(census, [pulse]) == [
        {"batch_id": 50, "product_id": 1, "run_key": 7, "census": [2, 3, 5]}
    ]
    assert census_overlaps(census[:1] + census[3:4], [pulse]) == []
    assert census_overlaps([], [pulse]) == []


def test_pulses_during_a_census_count_as_requests_but_not_light_load():
    clean, during = 1, 2
    rows = [
        # Slot T: a clean pulse at 600 ms (99%) and one during a census at 3,000 ms (50%).
        row(T, 600, successes=99, run_key=clean),
        row(T, 3000, successes=50, census=[9], run_key=during),
        # Slot T+30: only a pulse during a census: no success vote, no latency vote.
        row(T + HALF_HOUR, 2500, successes=60, census=[9], run_key=3),
        # Slot T+60: only a pulse during a census, every request failed: down, votes 0.
        row(T + 2 * HALF_HOUR, 0, successes=0, census=[10], run_key=4),
    ]
    m = timed(rows, 24, 30, slot_minutes=30)
    assert m["success_rate"] == pytest.approx((0.99 + 0) / 2)
    assert m["time_slots"]["success"] == 2 and m["time_slots"]["down"] == 1
    assert m["time_slots"]["skipped_during_census"] == 1
    assert m["latency_ms"]["p50"] == 600 and m["time_slots"]["latency"] == 1
    # Every request still counts.
    assert m["completed"] == 400 and m["request_success_rate"] == pytest.approx(209 / 400)
    # Only pulses during a census: no light-load latency, and the reason says why.
    only = timed(rows[1:], 24, 30)
    assert only["latency_ms"]["p50"] is None and only["latency_reason"] == LIGHT_CENSUS
    # Over every target, latency is the latency target's (generic_http) alone.
    https = [r | {"target": "generic_https", "run_key": 9} for r in rows[:1]]
    timed_split = census_split(rows + https, [], 30, 30, "generic_http")
    assert timed_split["pulses"]["clean"]["latency_ms"]["p50"] == 600
    assert timed_split["targets"]["generic_https"]["clean"]["latency_ms"]["p50"] is None
    assert timed_split["targets"]["generic_http"]["clean"]["latency_ms"]["p50"] == 600
    split = census_split(rows, [], 30, 30)
    assert split["pulses"]["clean"] | {"latency_ms": None} == {
        "runs": 0,
        "requests": 100,
        "successes": 99,
        "request_success_rate": 0.99,
        "success_rate": 0.99,
        "latency_ms": None,
    }
    assert split["pulses"]["clean"]["latency_ms"]["p50"] == 600
    during_census = split["pulses"]["during_census"]
    assert (during_census["requests"], during_census["successes"]) == (300, 110)
    assert during_census["success_rate"] == pytest.approx((0.5 + 0.6 + 0) / 3)
    assert during_census["latency_ms"]["p50"] == 2500
    assert split["census_batches"] == [9, 10]
    assert split["targets"]["generic_http"]["during_census"]["requests"] == 300
    # Done runs without an artifact count by their own flag.
    runs = [
        {
            "slot": T + 3 * HALF_HOUR,
            "target": "generic_http",
            "requests": 100,
            "successes": 100,
            "counted": False,
            "during_census": True,
            "census_batches": [11],
        }
    ]
    assert census_split(rows, runs, 30, 30)["pulses"]["during_census"]["runs"] == 1
    assert timed(rows, 24, 30, runs)["time_slots"]["skipped_during_census"] == 2


def test_load_runs_during_a_census_are_not_under_load():
    rows = [
        row(T, 600),
        row(T, 900, n=1000, test="load"),
        # Next slot: the load wave ran during a census; it is left out.
        row(T + HALF_HOUR, 600),
        row(T + HALF_HOUR, 6000, n=1000, test="load", census=[5]),
    ]
    result = under_load(rows, 30)
    assert result["ms"]["p50"] == 900 and result["slots"] == 1
    # Every load wave during a census: no value, and the reason says so.
    assert under_load(rows[2:], 30)["reason"] == UNDER_LOAD_CENSUS
