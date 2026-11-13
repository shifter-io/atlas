"""HTTPS-LATENCY-1: every HTTP target other than the latency target has a display-only
light-load latency from its own successes; nothing the latency target feeds changes."""

import json

import pytest
from calculated_cases import build_inputs, ready_inputs

from atlas.calculated import aggregate, store
from atlas.calculated.policy import digest

HTTPS = "generic_https"
# What only the display latency changes in an HTTPS target's metrics.
DISPLAY = ("latency_ms", "latency_reason", "latency_basis", "time_slots")


def https_inputs(https_ms=150, https_successes=1):
    """ready_inputs() with the HTTPS pulses at https_ms x product id and https_successes each."""
    c, now, r, b, o, e = ready_inputs()
    https = {x["run_key"] for x in r if x["scope"]["target"]["id"] == HTTPS}
    for bucket in b:
        if bucket["run_key"] in https:
            counters = bucket["counters"]
            ms = https_ms * (bucket["run_key"] // 10)
            counters["completed"] = counters["successes"] = https_successes
            counters["latency_histogram"] = {"bins": [[ms, https_successes]]}
    return c, now, r, b, o, e


def built(inputs):
    from atlas.calculated.calculator import build

    c, now, r, b, o, e = inputs
    return store.jsonable(build(build_inputs(r, b, o, e, now), c, digest(c), 1, None))


def windows(result):
    """(where, window) of every product and network window of a build."""
    for kind in ("products", "networks"):
        for i, entry in enumerate(result[kind]):
            for label, w in entry["windows"].items():
                yield (kind, i, label), w


def test_https_latency_comes_from_its_own_rows():
    result = built(https_inputs())
    for (_, i, _), w in windows(result):
        https = w["targets"][HTTPS]["metrics"]
        http = w["targets"]["generic_http"]["metrics"]
        # Product (and network) 1's HTTPS pulse at 150 ms, 2's at 300 ms; HTTP at 100 and 200.
        expected = 150 * (i + 1)
        assert https["latency_ms"]["p50"] == pytest.approx(expected, rel=0.05)
        assert https["latency_reason"] is None
        assert https["time_slots"]["latency_targets"] == [HTTPS]
        assert "display only" in https["latency_basis"]
        # Display only: no latency under load, and no speed in its score.
        assert https["latency_under_load_ms"]["p50"] is None
        assert http["latency_ms"]["p50"] != https["latency_ms"]["p50"]
    for p in result["products"]:
        score = p["windows"]["24h"]["targets"][HTTPS]["score"]
        assert score["components"]["speed"]["value"] is None
        assert score["weights"]["speed"] == 0


def test_https_latency_is_null_below_the_minimum_successes():
    c, now, r, b, o, e = https_inputs(https_successes=1)
    c["slots"] = {"min_latency_successes": 2}
    for bucket in b:
        if bucket["run_key"] % 10 != 1:  # the HTTP pulse and load runs: 2 successes each
            counters = bucket["counters"]
            counters["completed"] = counters["successes"] = 2
            counters["latency_histogram"]["bins"][0][1] = 2
    result = built((c, now, r, b, o, e))
    for _, w in windows(result):
        https = w["targets"][HTTPS]["metrics"]
        assert https["latency_ms"]["p50"] is None
        assert https["latency_reason"] == aggregate.LIGHT_NONE
        assert w["targets"]["generic_http"]["metrics"]["latency_ms"]["p50"] is not None


def without_display(result):
    """result as plain JSON values, without its build time and every HTTPS target's display
    latency fields (also in the copies a network window keeps of its lead's, "_lead")."""
    plain = json.loads(json.dumps(result, sort_keys=True, default=str))
    plain.pop("generated_at")

    def strip(value):
        if isinstance(value, dict):
            for key, v in value.items():
                if key == HTTPS and isinstance(v, dict):
                    for metrics in (v.get("metrics"), v.get("benchmark")):
                        for name in DISPLAY if isinstance(metrics, dict) else ():
                            metrics.pop(name, None)
                strip(v)
        elif isinstance(value, list):
            for v in value:
                strip(v)

    strip(plain)
    return plain


def differences(a, b, path=""):
    """The paths where JSON values a and b differ."""
    if isinstance(a, dict) and isinstance(b, dict):
        return [
            d
            for k in sorted(set(a) | set(b))
            for d in differences(a.get(k, "<missing>"), b.get(k, "<missing>"), f"{path}.{k}")
        ]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [
            d
            for i, (x, y) in enumerate(zip(a, b, strict=True))
            for d in differences(x, y, f"{path}[{i}]")
        ]
    return [] if a == b else [path]


def test_everything_else_is_byte_for_byte_as_before(monkeypatch):
    now_result = without_display(built(https_inputs()))
    # Before: no row of another target is light, so it has no latency.
    monkeypatch.setattr(aggregate, "own_light", lambda rows: rows)
    before = built(https_inputs())
    for _, w in windows(before):
        assert w["targets"][HTTPS]["metrics"]["latency_ms"]["p50"] is None
    found = differences(now_result, without_display(before))
    assert not found, found[:20]


def test_own_light_leaves_out_census_load_and_overlapping_pulses():
    rows = [
        {"test": "pulse", "during_census": False},
        {"test": None, "during_census": False},
        {"test": "pulse", "during_census": True},
        {"test": "load", "during_census": False},
        {"test": "census", "during_census": False},
    ]
    assert [r["light"] for r in aggregate.own_light(rows)] == [True, True, False, False, False]


def test_browser_targets_keep_no_latency():
    timing = (1, 30, "generic_http")
    tm = aggregate.target_timed(("t", [], {"browser": {}}), 24, [], timing, ({}, {}))
    assert tm["latency_ms"] == dict.fromkeys(aggregate.KEYS)
    assert tm["latency_reason"] == aggregate.NOT_LATENCY_TARGET
    assert tm["under_load"] == {"reason": aggregate.NOT_LATENCY_TARGET}
