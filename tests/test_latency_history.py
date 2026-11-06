"""Chart histories keep the HTTP pulse and load measurements distinct."""

from copy import deepcopy

import pytest
from calculated_cases import ready_inputs

from proxy_index.calculated.calculator import build
from proxy_index.calculated.latency import LATENCY, without_mean
from proxy_index.calculated.policy import digest


def test_history_uses_only_http_pulses_and_measured_load_factors():
    config, now, runs, buckets, observations, evidence = ready_inputs(products=(1,))
    # The same-slot fixture has a factor of one. HTTPS must not affect either curve.
    result = build(
        runs, buckets, observations, evidence, config, digest(config), now, 1, None, None
    )
    window = result["products"][0]["windows"]["24h"]
    measured = [p for p in window["series"] if p["latency_ms"]["p50"] is not None]
    assert len(measured) == 1
    assert measured[0]["latency_under_load_ms"] == measured[0]["latency_ms"]
    http = window["targets"]["generic_http"]["metrics"]
    # Series points carry the percentiles alone; the window's latency objects also the mean.
    assert measured[0]["latency_ms"] == without_mean(http["latency_ms"])
    assert set(measured[0]["latency_under_load_ms"]) == set(LATENCY)
    assert http["latency_ms"]["mean"] is not None
    assert http["latency_under_load_ms"]["mean"] == pytest.approx(
        http["latency_ms"]["mean"], abs=0.1
    )
    assert all(
        p["latency_under_load_ms"]["p50"] is None for p in window["series"] if p not in measured
    )

    # Without the load wave, normal latency remains; load latency is unavailable.
    pulse_runs = [r for r in runs if r["test"] != "load"]
    keys = {r["run_key"] for r in pulse_runs}
    pulse_buckets = [deepcopy(b) for b in buckets if b["run_key"] in keys]
    result = build(
        pulse_runs,
        pulse_buckets,
        observations,
        evidence,
        config,
        digest(config),
        now,
        2,
        None,
        None,
    )
    points = result["products"][0]["windows"]["24h"]["series"]
    assert any(p["latency_ms"]["p50"] is not None for p in points)
    assert all(p["latency_under_load_ms"]["p50"] is None for p in points)
    http = result["products"][0]["windows"]["24h"]["targets"]["generic_http"]["metrics"]
    assert http["latency_ms"]["mean"] is not None and http["latency_under_load_ms"]["mean"] is None
