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
OVERRIDES = {
    "weights",
    "target_weights",
    "curves",
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
        "curves",
        "grades",
        "profiles",
        "products",
        "targets",
        "kind",
        "display_name",
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
    if set(config["curves"]) != set(KEYS):
        raise ValueError("Unknown scoring curve")
    for key, curve in config["curves"].items():
        if key == "diversity":
            if set(curve) != {"countries", "regions", "cities", "asns"}:
                raise ValueError("Invalid diversity dimensions")
            for low, high, weight in curve.values():
                if not 0 < low < high or weight < 0:
                    raise ValueError("Invalid diversity curve")
            if abs(sum(v[2] for v in curve.values()) - 1) > 1e-9:
                raise ValueError("Diversity weights must sum to one")
        elif key == "authenticity":
            if curve["blocklist_penalty"] < 0:
                raise ValueError("Invalid authenticity penalty")
        elif curve.get("kind") == "cost":
            if curve["anchor"] <= 0 or curve["penalty"] <= 0:
                raise ValueError("Invalid cost curve")
        elif curve.get("kind") in ("linear", "inverse", "log", "log_inverse"):
            if not curve["low"] < curve["high"] or (
                curve["kind"].startswith("log") and curve["low"] <= 0
            ):
                raise ValueError("Invalid curve bounds")
        else:
            raise ValueError("Unknown curve kind")
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
    return config


def load(path):
    config = tomllib.loads(Path(path).read_text())
    # JSON roundtrip rejects dates and nonfinite numbers in TOML configurations.
    config = json.loads(json.dumps(config, allow_nan=False))
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


def curve_score(curve, value):
    if value is None:
        return None
    kind = curve["kind"]
    if kind == "cost":
        value = 100 - curve["penalty"] * math.log10(max(value, 1e-4) / curve["anchor"])
    else:
        lo, hi = curve["low"], curve["high"]
        ratio = (
            math.log(max(value, lo) / lo) / math.log(hi / lo)
            if kind.startswith("log")
            else (value - lo) / (hi - lo)
        )
        value = 100 * (1 - ratio if kind in ("inverse", "log_inverse") else ratio)
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
