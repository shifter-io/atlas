"""Scoring from every request: time-weighted success and latency, latency under load, per-IP
shares without DB-IP unknowns, and methodology files that extend another."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from milf_framework.calculated.aggregate import (
    LIGHT_CENSUS,
    UNDER_LOAD_CENSUS,
    UNDER_LOAD_LATE,
    UNDER_LOAD_LIGHT,
    UNDER_LOAD_NONE,
    blend,
    census_overlaps,
    census_split,
    latency_detail,
    load_factors,
    pool,
    product_inputs,
    timed,
    under_load,
)
from milf_framework.calculated.policy import events, load, slot_settings
from milf_framework.stats import Histogram

T = datetime(2026, 10, 3, 18, tzinfo=UTC)
HALF_HOUR = timedelta(minutes=30)


def row(
    slot, ms, *, n=100, successes=None, test="pulse", country="US", target="generic_http",
    census=(), run_key=None,
):
    """One bucket row: n requests in a time slot, every success at `ms` milliseconds; census
    names the census batches the run overlapped (a pulse or load wave during a census)."""
    successes = n if successes is None else successes
    return {
        "bucket_start": slot,
        "last_observed": slot,
        "slot": slot,
        "run_key": run_key,
        "test": test,
        "during_census": bool(census),
        "census_batches": list(census),
        "light": test in (None, "pulse") and not census,
        "country": country,
        "target": target,
        "concurrency": 1000 if test == "load" else 25,
        "counters": {
            "completed": n,
            "successes": successes,
            "errors": {"proxy_connect": n - successes} if n > successes else {},
            "response_body_bytes": 0,
            "latency_histogram": {"bins": [[ms, successes]] if successes else []},
        },
    }


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
        "minutes": 30, "success": 4, "down": 1, "skipped_during_census": 0, "latency": 2,
        "latency_targets": ["generic_http"], "min_latency_successes": 30,
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
    assert timed(rows[:1], 24, 30)["latency_ms"]["p50"] == 800
    assert timed(rows[1:], 24, 30)["latency_ms"]["p50"] == 1600


def test_latency_under_load_worked_example():
    """US pulse 600 ms, US load wave 900 ms: factor 1.5. A Romanian pulse of 800 ms in the same
    slot is 1,200 ms under load. Census latency never makes a factor."""
    rows = [
        row(T, 600, country="US"),
        row(T, 900, n=1000, test="load", country="US"),
        row(T, 800, country="RO"),
        row(T, 5000, n=1000, test="census", country="RO"),
    ]
    factors, reference, excluded = load_factors(rows, 30)
    assert reference == {"country": "US", "target": "generic_http", "concurrency": 1000}
    assert excluded == 0
    assert factors == {T: {"p50": 1.5, "p90": 1.5, "p95": 1.5, "p99": 1.5}}
    detail = latency_detail(rows, factors, reference, 30)
    assert {(c["country"], c["target"]) for c in detail["countries"]} == {
        ("RO", "generic_http"), ("US", "generic_http"),
    }
    countries = {c["country"]: c for c in detail["countries"]}
    assert countries["RO"]["latency_ms"]["p50"] == 800
    assert countries["RO"]["latency_under_load_ms"]["p50"] == 1200
    assert countries["US"]["latency_under_load_ms"]["p50"] == 900
    assert detail["factors"]["latest"] == {"at": T, "p50": 1.5, "p90": 1.5, "p95": 1.5,
                                           "p99": 1.5}


def test_under_load_carries_the_newest_earlier_factor_and_says_why_when_none():
    rows = [
        row(T - HALF_HOUR, 700, country="RO"),  # before the first load test: no value
        row(T, 600), row(T, 900, n=1000, test="load"), row(T, 800, country="RO"),
        # Next slot: the load run had too few successes; the factor of T carries forward.
        row(T + HALF_HOUR, 400), row(T + HALF_HOUR, 4000, n=1000, successes=10, test="load"),
        row(T + HALF_HOUR, 1000, country="RO"),
    ]
    factors, *_ = load_factors(rows, 30)
    assert list(factors) == [T]
    romania = [r for r in rows if r["country"] == "RO"]
    result = under_load(romania, factors, 30)
    assert result["ms"]["p50"] == pytest.approx((800 * 1.5 + 1000 * 1.5) / 2)
    assert (result["own_factor"], result["carried_factor"], result["without_factor"]) == (1, 1, 1)
    assert result["reason"] is None
    assert under_load(romania[:1], factors, 30)["reason"] == UNDER_LOAD_LATE
    assert under_load(romania, {}, 30)["reason"] == UNDER_LOAD_NONE
    assert under_load([row(T, 800, n=10)], factors, 30)["reason"] == UNDER_LOAD_LIGHT


def observation(ip_id, country, asked, usage="residential", **fields):
    return {
        "ip_id": ip_id,
        "bucket_start": T,
        "first_seen": T,
        "last_seen": T,
        "count": 1,
        "product_first_seen": T,
        "requested_country": asked,
        "country_code": country,
        "usage_type": usage,
        "asn": ip_id,
        "matched": country is not None,
        **fields,
    }


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
    inputs = product_inputs(counts, {"lowest_per_gb": 0.75}, config, "residential")
    assert inputs["diversity"]["state"] == inputs["dcAsns"]["state"] == "ready"
    assert inputs["dcAsns"]["value"] == pytest.approx(0.5)
    assert inputs["authenticity"]["value"] == pytest.approx(200 / 3)
    assert inputs["value"]["value"] == 0.75
    # A datacenter product keeps 100 on dcAsns.
    datacenter = product_inputs(counts, {}, config, "datacenter")["dcAsns"]
    assert datacenter["fixed"] == 100 and datacenter["state"] == "ready"


def test_staging_methodology_extends_production(tmp_path):
    production, production_id = load("configs/methodology.toml")
    staging, staging_id = load("configs/staging-methodology.toml")
    assert (production["minimum_requests"], staging["minimum_requests"]) == (4000, 1000)
    assert {k: v for k, v in staging.items() if k not in ("minimum_requests", "name")} == {
        k: v for k, v in production.items() if k not in ("minimum_requests", "name")
    }
    assert "extends" not in staging and staging_id != production_id
    assert slot_settings(staging) == {"min_latency_successes": 30}
    assert events("configs/staging-methodology.toml") == events("configs/methodology.toml")
    # Tables merge key by key; a cycle is refused.
    child = tmp_path / "child.toml"
    child.write_text(
        f'extends = "{Path("configs/methodology.toml").resolve()}"\n'
        '[products."shifter/residential"]\ndisplay_name = "Other"\n'
    )
    merged, _ = load(child)
    assert merged["products"]["shifter/residential"] == {
        "kind": "residential",
        "display_name": "Other",
    }
    (tmp_path / "a.toml").write_text('extends = "b.toml"\n')
    (tmp_path / "b.toml").write_text('extends = "a.toml"\n')
    with pytest.raises(ValueError, match="cycle"):
        load(tmp_path / "a.toml")


def test_removed_settings_are_refused(tmp_path):
    text = Path("configs/methodology.toml").read_text()
    for extra in ('scored_roles = ["benchmark"]\n', "[authenticity]\nblocklist_penalty = 120\n"):
        policy = tmp_path / "methodology.toml"
        policy.write_text(extra + text if extra.startswith("scored") else text + extra)
        with pytest.raises(ValueError, match="Unknown methodology setting"):
            load(policy)


def census_run(batch_id, start, minutes, product=1):
    return {"product_id": product, "batch_id": batch_id, "started_at": start,
            "finished_at": start + timedelta(minutes=minutes)}


def test_census_overlap_detection_any_country_same_product():
    pulse = {"product_id": 1, "batch_id": 50, "run_key": 7, "started_at": T,
             "finished_at": T + timedelta(minutes=1)}
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
    split = census_split(rows, [], 30, 30)
    assert split["pulses"]["clean"] | {"latency_ms": None} == {
        "runs": 0, "requests": 100, "successes": 99, "request_success_rate": 0.99,
        "success_rate": 0.99, "latency_ms": None,
    }
    assert split["pulses"]["clean"]["latency_ms"]["p50"] == 600
    during_census = split["pulses"]["during_census"]
    assert (during_census["requests"], during_census["successes"]) == (300, 110)
    assert during_census["success_rate"] == pytest.approx((0.5 + 0.6 + 0) / 3)
    assert during_census["latency_ms"]["p50"] == 2500
    assert split["census_batches"] == [9, 10]
    assert split["targets"]["generic_http"]["during_census"]["requests"] == 300
    # Done runs without an artifact count by their own flag.
    runs = [{"slot": T + 3 * HALF_HOUR, "target": "generic_http", "requests": 100,
             "successes": 100, "counted": False, "during_census": True, "census_batches": [11]}]
    assert census_split(rows, runs, 30, 30)["pulses"]["during_census"]["runs"] == 1
    assert timed(rows, 24, 30, runs)["time_slots"]["skipped_during_census"] == 2


def test_no_load_factor_from_a_census_slot():
    rows = [
        row(T, 600), row(T, 900, n=1000, test="load"), row(T, 800, country="RO"),
        # Next slot: the load wave ran during a census; T's factor carries forward.
        row(T + HALF_HOUR, 600), row(T + HALF_HOUR, 6000, n=1000, test="load", census=[5]),
        row(T + HALF_HOUR, 1000, country="RO"),
        # Then the reference pulse ran during a census: no factor either.
        row(T + 2 * HALF_HOUR, 600, census=[6]), row(T + 2 * HALF_HOUR, 900, n=1000, test="load"),
        row(T + 2 * HALF_HOUR, 1000, country="RO"),
    ]
    factors, _, excluded = load_factors(rows, 30)
    assert list(factors) == [T] and excluded == 2
    romania = [r for r in rows if r["country"] == "RO"]
    result = under_load(romania, factors, 30, excluded)
    assert (result["own_factor"], result["carried_factor"]) == (1, 2)
    assert result["ms"]["p50"] == pytest.approx((800 + 1000 + 1000) * 1.5 / 3)
    assert latency_detail(rows, factors, None, 30, excluded)["factors"][
        "excluded_during_census"
    ] == 2
    # Every load wave during a census: no factor at all, and the reason says so.
    only = [r for r in rows if r["slot"] != T]
    factors, _, excluded = load_factors(only, 30)
    assert factors == {} and excluded == 2
    assert under_load(only, factors, 30, excluded)["reason"] == UNDER_LOAD_CENSUS
