"""Scoring from every request: time-weighted success and latency, latency under load, per-IP
shares without DB-IP unknowns, and methodology files that extend another."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from milf_framework.calculated.aggregate import (
    UNDER_LOAD_LATE,
    UNDER_LOAD_LIGHT,
    UNDER_LOAD_NONE,
    blend,
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


def row(slot, ms, *, n=100, successes=None, test="pulse", country="US", target="generic_http"):
    """One bucket row: n requests in a time slot, every success at `ms` milliseconds."""
    successes = n if successes is None else successes
    return {
        "bucket_start": slot,
        "last_observed": slot,
        "slot": slot,
        "test": test,
        "light": test in (None, "pulse"),
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
    # A pulse the provider failed without an artifact: a filled-and-down slot voting 0.
    down = [{"slot": T + 3 * HALF_HOUR, "down": True, "requests": 0, "successes": 0,
             "counted": False}]
    m = timed(rows, 24, 30, down, 30)
    assert m["success_rate"] == pytest.approx((1 + 0.5 + 0.5 + 0) / 4)
    assert m["request_success_rate"] == pytest.approx(1055 / 1110)
    assert m["time_slots"] == {
        "minutes": 30, "success": 4, "down": 1, "latency": 2, "min_latency_successes": 30,
    }
    # Light-load latency: pulses only; the census slot (and slots under 30 successes) do not vote.
    assert m["latency_ms"]["p50"] == 100
    light = timed([row(T, 100), row(T + HALF_HOUR, 800, n=29)], 24, 30)
    assert light["latency_ms"]["p50"] == 100 and light["time_slots"]["latency"] == 1


def test_latency_under_load_worked_example():
    """US pulse 600 ms, US load wave 900 ms: factor 1.5. A Romanian pulse of 800 ms in the same
    slot is 1,200 ms under load. Census latency never makes a factor."""
    rows = [
        row(T, 600, country="US"),
        row(T, 900, n=1000, test="load", country="US"),
        row(T, 800, country="RO"),
        row(T, 5000, n=1000, test="census", country="RO"),
    ]
    factors, reference = load_factors(rows, 30)
    assert reference == {"country": "US", "target": "generic_http", "concurrency": 1000}
    assert factors == {T: {"p50": 1.5, "p90": 1.5, "p95": 1.5, "p99": 1.5}}
    detail = latency_detail(rows, factors, reference, 30)
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
    factors, _ = load_factors(rows, 30)
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
