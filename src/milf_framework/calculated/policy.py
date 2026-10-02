"""Validated, immutable, data-only scoring configuration."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import tomllib
from pathlib import Path

KEYS = (
    "reliability",
    "speed",
    "uptime",
    "coverage",
    "diversity",
    "dcAsns",
    "authenticity",
    "value",
)
ROLES = ("exploratory", "pool_sampling", "benchmark", "monitor")
# Uptime: a pulse batch counts as up when the product had at least min_successes successful
# requests in it; the uptime component is ready from min_batches finished pulse batches.
UPTIME = {"min_successes": 1, "min_batches": 1}
# Live tape detection thresholds ([events] in the methodology file). They never change a
# calculation, so load() keeps them out of the methodology id; detect.py reads them via events().
EVENTS = {
    "baseline_batches": 48,
    "min_baseline_batches": 6,
    "min_requests": 20,
    "min_latency_samples": 10,
    "success_drop_pp": 0.15,
    "latency_spike_ratio": 2.0,
    "block_spike_pp": 0.10,
    "down_baseline": 0.5,
    "pulse_change_pp": 0.05,
    "pulse_latency_ratio": 1.5,
    "hosting_usage_types": ["hosting", "datacenter"],
    "settle_seconds": 120,
    "initial_lookback_minutes": 60,
}
# Diversity mixes the ratio to the leader of these pool dimensions ([peer.diversity] weights).
DIMENSIONS = ("countries", "regions", "cities", "asns")
OVERRIDES = {
    "weights",
    "target_weights",
    "peer",
    "required_targets",
    "minimum_requests",
    "minimum_coverage",
    "score_valid_hours",
    "kind",
    "display_name",
    "benchmark_weights",
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    ).hexdigest()


def merge(base, overrides):
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        result[key] = (
            merge(result[key], value) if isinstance(value, dict) and key in result else value
        )
    if "required_targets" in overrides and "benchmark_weights" in result:
        result["benchmark_weights"] = {
            t: result["benchmark_weights"][t]
            for t in result["required_targets"]
            if t in result["benchmark_weights"]
        }
    return result


def validate(config):
    def weights(values, keys):
        if (
            set(values) != set(keys)
            or any(
                type(v) not in (int, float) or not math.isfinite(v) or v < 0
                for v in values.values()
            )
            or sum(values.values()) <= 0
        ):
            raise ValueError("Weights must cover known components and have a positive total")

    known = {
        "name",
        "status",
        "windows",
        "refresh_seconds",
        "required_targets",
        "minimum_requests",
        "minimum_coverage",
        "score_valid_hours",
        "sample_limit",
        "weights",
        "target_weights",
        "benchmark_weights",
        "peer",
        "authenticity",
        "grades",
        "profiles",
        "products",
        "targets",
        "kind",
        "display_name",
        "scored_roles",
        "uptime",
    }
    if set(config) - known:
        raise ValueError("Unknown methodology setting")
    if "balanced" not in config.get("profiles", {}):
        raise ValueError("The balanced profile is required")
    weights(config["weights"], KEYS)
    weights(config["target_weights"], ("reliability", "speed"))
    for name in ("minimum_requests", "score_valid_hours", "refresh_seconds", "sample_limit"):
        if type(config[name]) is not int or config[name] <= 0:
            raise ValueError(f"Invalid {name}")
    if not 0 <= config["minimum_coverage"] <= 1:
        raise ValueError("Invalid coverage")
    if sorted(config["windows"]) != [24, 168, 720]:
        raise ValueError("Windows must be 24, 168 and 720 hours")
    if not config["required_targets"] or len(set(config["required_targets"])) != len(
        config["required_targets"]
    ):
        raise ValueError("Required targets must be distinct and nonempty")
    weights(config["benchmark_weights"], config["required_targets"])
    if set(config["peer"]) != {"diversity"}:
        raise ValueError("Unknown peer setting")
    weights(config["peer"]["diversity"], DIMENSIONS)
    if abs(sum(config["peer"]["diversity"].values()) - 1) > 1e-9:
        raise ValueError("Diversity weights must sum to one")
    penalty = config["authenticity"]
    if (
        set(penalty) != {"blocklist_penalty"}
        or type(penalty["blocklist_penalty"]) not in (int, float)
        or not math.isfinite(penalty["blocklist_penalty"])
        or penalty["blocklist_penalty"] < 0
    ):
        raise ValueError("Invalid authenticity penalty")
    if set(config["grades"]) != {"A+", "A", "B+", "B", "C+", "C", "D"}:
        raise ValueError("Invalid grade labels")
    grades = [config["grades"][g] for g in ("A+", "A", "B+", "B", "C+", "C", "D")]
    if (
        not 100 >= grades[0]
        or grades[-1] != 0
        or any(a <= b for a, b in zip(grades, grades[1:], strict=False))
    ):
        raise ValueError("Invalid grade thresholds")
    for values in config.get("profiles", {}).values():
        weights(values, KEYS)
    roles = config.get("scored_roles", ["benchmark"])
    if not roles or not set(roles) <= set(ROLES) or len(set(roles)) != len(roles):
        raise ValueError("Scored roles must be distinct, known collection roles")
    uptime = config.get("uptime", {})
    if set(uptime) - set(UPTIME) or any(type(v) is not int or v < 1 for v in uptime.values()):
        raise ValueError("Invalid uptime settings")
    return config


def scored_roles(config):
    """Collection roles whose buckets feed target scores (the benchmark metrics)."""
    return set(config.get("scored_roles", ["benchmark"]))


def uptime_settings(config):
    return UPTIME | config.get("uptime", {})


def events(path=None):
    """Validated [events] thresholds of a methodology file, over the defaults."""
    section = {}
    if path is not None and Path(path).exists():
        section = tomllib.loads(Path(path).read_text()).get("events", {})
    if set(section) - set(EVENTS):
        raise ValueError("Unknown event setting")
    result = EVENTS | section
    for key, value in result.items():
        if key == "hosting_usage_types":
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                raise ValueError("Invalid hosting usage types")
        elif (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or value < 0
            or (type(EVENTS[key]) is int and type(value) is not int)
        ):
            raise ValueError(f"Invalid event setting {key}")
    if result["latency_spike_ratio"] <= 1 or result["pulse_latency_ratio"] <= 1:
        raise ValueError("Latency ratios must exceed one")
    return result


def load(path):
    config = tomllib.loads(Path(path).read_text())
    # JSON roundtrip rejects dates and nonfinite numbers in TOML configurations.
    config = json.loads(json.dumps(config, allow_nan=False))
    # Detection thresholds are validated by events() and are not part of the methodology id.
    config.pop("events", None)
    validate(config)
    for section in ("products", "targets"):
        for overrides in config.get(section, {}).values():
            if set(overrides) - OVERRIDES:
                raise ValueError("Unknown methodology override")
            validate(merge(config, overrides))
    return config, digest(config)


def effective(config, provider, product, target=None):
    result = merge(config, config.get("products", {}).get(f"{provider}/{product}", {}))
    if target:
        result = merge(result, config.get("targets", {}).get(target, {}))
    return validate(result)


def ratio(value, best, higher):
    """A raw value's score against the best raw value among its peers, from 0 to 100.

    Higher-is-better values score 100 x value / best (0 when the best is not positive);
    lower-is-better values score 100 x best / value (100 when the value is not positive).
    """
    if value is None or best is None:
        return None
    if higher:
        value = 100 * value / best if best > 0 else 0.0
    else:
        value = 100 * best / value if value > 0 else 100.0
    return min(100.0, max(0.0, value))


def composite(components, weights):
    required = [key for key, weight in weights.items() if weight > 0]
    if any(
        components[key]["value"] is None or components[key]["state"] != "ready" for key in required
    ):
        return None
    return sum(components[k]["value"] * weights[k] for k in required) / sum(weights.values())


def grade(value, config):
    return next(
        label
        for label, threshold in sorted(config["grades"].items(), key=lambda x: -x[1])
        if value >= threshold
    )
