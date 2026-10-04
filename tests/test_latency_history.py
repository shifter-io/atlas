"""Chart histories keep the HTTP pulse and load measurements distinct."""
from copy import deepcopy

from test_calculated import ready_inputs

from milf_framework.calculated.calculator import build
from milf_framework.calculated.policy import digest


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
    assert measured[0]["latency_ms"] == window["targets"]["generic_http"]["metrics"]["latency_ms"]
    assert all(
        p["latency_under_load_ms"]["p50"] is None
        for p in window["series"] if p not in measured
    )

    # Without the load wave, normal latency remains; load latency is unavailable.
    pulse_runs = [r for r in runs if r["test"] != "load"]
    keys = {r["run_key"] for r in pulse_runs}
    pulse_buckets = [deepcopy(b) for b in buckets if b["run_key"] in keys]
    result = build(
        pulse_runs, pulse_buckets, observations, evidence,
        config, digest(config), now, 2, None, None
    )
    points = result["products"][0]["windows"]["24h"]["series"]
    assert any(p["latency_ms"]["p50"] is not None for p in points)
    assert all(p["latency_under_load_ms"]["p50"] is None for p in points)
