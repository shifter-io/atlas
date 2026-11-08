"""Validated, immutable, data-only scoring configuration."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import tomllib
from pathlib import Path

from ..browser.contracts import BROWSER_TARGETS, RUN_TARGETS

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
# Collection role labels (calculation_roles). They are display labels only: every request of a
# run counts for every metric whatever its role.
ROLES = ("exploratory", "pool_sampling", "benchmark", "monitor")
# Uptime: a pulse batch counts as up when the product had at least min_successes successful
# requests in it; the uptime component is ready from min_batches finished pulse batches.
UPTIME = {"min_successes": 1, "min_batches": 1}
# Time-weighted window metrics ([slots]): a time slot (the cohort's pulse interval) votes on
# latency only with at least min_latency_successes successful requests.
SLOTS = {"min_latency_successes": 30}
# DB-IP usage types that mark an exit address as a datacenter/hosting address (dcAsns, and the
# new_hosting_asn tape event). A top-level methodology setting, so it is part of the id.
HOSTING_USAGE_TYPES = ["hosting", "datacenter"]
# The one target every latency figure comes from (light load, under load, speed, breakdowns, the
# load factor): the census target. Other targets count for everything but latency.
LATENCY_TARGET = "generic_http"
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
    "settle_seconds": 120,
    "initial_lookback_minutes": 60,
}
# Rows each list of the published snapshot keeps ([payload] in the methodology file): the
# breakdowns (by completed requests) and the pool lists (by IPs), top first. calculated_list_rows
# keeps every row and every total is computed before capping (calculated/slim.py); 0 publishes
# only the list's entry count. The country lists are bounded by the countries there are, so they
# stay whole; the site API restores the full city x ASN and ASN lists from calculated_list_rows.
PAYLOAD = {
    "country": 1000,
    "region": 25,
    "city": 25,
    "asn": 25,
    "country_asn": 25,
    "city_asn": 25,
    "geo": 50,
    "countries_breakdown": 1000,
    "asns_breakdown": 50,
}
# A browser target's difficulty ([browser_difficulty]): an integer from 1 (easy) to 5 (hard), the
# weight of its success score in a network's overall browser score (calculated/target_scores.py).
DIFFICULTY = range(1, 6)
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
# Every top-level methodology setting validate() accepts.
KNOWN = {
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
    "grades",
    "profiles",
    "products",
    "targets",
    "kind",
    "display_name",
    "uptime",
    "slots",
    "hosting_usage_types",
    "latency_target",
    "payload",
    "target_groups",
    "browser_difficulty",
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


def _weights(values, keys):
    if (
        set(values) != set(keys)
        or any(
            type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values.values()
        )
        or sum(values.values()) <= 0
    ):
        raise ValueError("Weights must cover known components and have a positive total")


def validate(config):
    if set(config) - KNOWN:
        raise ValueError("Unknown methodology setting")
    if "balanced" not in config.get("profiles", {}):
        raise ValueError("The balanced profile is required")
    _weights(config["weights"], KEYS)
    _weights(config["target_weights"], ("reliability", "speed"))
    _limits(config)
    _weights(config["benchmark_weights"], config["required_targets"])
    _peer(config["peer"])
    _grades(config["grades"])
    for values in config.get("profiles", {}).values():
        _weights(values, KEYS)
    uptime = config.get("uptime", {})
    if set(uptime) - set(UPTIME) or any(type(v) is not int or v < 1 for v in uptime.values()):
        raise ValueError("Invalid uptime settings")
    _counts(config.get("slots", {}), SLOTS, 1, "slot")
    _counts(config.get("payload", {}), PAYLOAD, 0, "payload")
    _usage_types(config.get("hosting_usage_types", HOSTING_USAGE_TYPES))
    _groups(config)
    _difficulty(config)
    if latency_target(config) not in config["required_targets"]:
        raise ValueError("The latency target must be a required target")
    return config


def _limits(config):
    """The positive counts, the coverage share, the windows and the required targets."""
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


def _peer(peer):
    """[peer]: only the diversity weights, over DIMENSIONS and summing to one."""
    if set(peer) != {"diversity"}:
        raise ValueError("Unknown peer setting")
    _weights(peer["diversity"], DIMENSIONS)
    if abs(sum(peer["diversity"].values()) - 1) > 1e-9:
        raise ValueError("Diversity weights must sum to one")


def _grades(thresholds):
    """[grades]: the seven labels, strictly falling from at most 100 down to 0."""
    if set(thresholds) != {"A+", "A", "B+", "B", "C+", "C", "D"}:
        raise ValueError("Invalid grade labels")
    grades = [thresholds[g] for g in ("A+", "A", "B+", "B", "C+", "C", "D")]
    if (
        not 100 >= grades[0]
        or grades[-1] != 0
        or any(a <= b for a, b in zip(grades, grades[1:], strict=False))
    ):
        raise ValueError("Invalid grade thresholds")


def latency_target(config):
    """The target every latency figure is measured on (methodology latency_target)."""
    target = config.get("latency_target", LATENCY_TARGET)
    if not isinstance(target, str) or not target:
        raise ValueError("Invalid latency target")
    return target


def _groups(config):
    """[target_groups]: each group target names its distinct sources; a target is a source of
    one group at most, and a group is nobody's source (calculated/browser.py). No browser, group
    or source target is a required target: those are per-target figures only."""
    groups = config.get("target_groups", {})
    if not isinstance(groups, dict) or not all(isinstance(v, list) and v for v in groups.values()):
        raise ValueError("Invalid target groups")
    sources = [s for v in groups.values() for s in v]
    if (
        not all(isinstance(s, str) and s for s in sources)
        or len(set(sources)) != len(sources)
        or set(sources) & set(groups)
    ):
        raise ValueError("Invalid target groups")
    if set(config["required_targets"]) & {*BROWSER_TARGETS, *groups, *sources}:
        raise ValueError("A browser or group target cannot be a required target")


def _difficulty(config):
    """[browser_difficulty] (optional): a DIFFICULTY integer for every run target, and only for
    browser, group or source targets; a group without one takes its hardest source's."""
    values = config.get("browser_difficulty")
    if values is None:
        return
    groups = config.get("target_groups", {})
    known = {*BROWSER_TARGETS, *groups, *(s for v in groups.values() for s in v)}
    if (
        not isinstance(values, dict)
        or set(values) - known
        or set(RUN_TARGETS) - set(values)
        or any(type(v) is not int or v not in DIFFICULTY for v in values.values())
    ):
        raise ValueError("Invalid browser difficulty")


def browser_difficulty(config):
    """Each browser target's difficulty: [browser_difficulty], plus each group without one the
    highest of its sources' (none without a table)."""
    values = dict(config.get("browser_difficulty", {}))
    for group, sources in config.get("target_groups", {}).items():
        found = [values[s] for s in sources if s in values]
        if group not in values and found:
            values[group] = max(found)
    return values


def _usage_types(value):
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise ValueError("Invalid hosting usage types")
    return value


def uptime_settings(config):
    return UPTIME | config.get("uptime", {})


def slot_settings(config):
    return SLOTS | config.get("slots", {})


def _counts(settings, defaults, minimum, name):
    """Integer settings, each a known key and at least minimum."""
    if set(settings) - set(defaults) or any(
        type(v) is not int or v < minimum for v in settings.values()
    ):
        raise ValueError(f"Invalid {name} settings")


def payload_limits(config):
    return PAYLOAD | config.get("payload", {})


def hosting_usage_types(config):
    return list(config.get("hosting_usage_types", HOSTING_USAGE_TYPES))


def read(path, _seen=()):
    """A methodology file as data, with `extends` resolved.

    `extends = "other.toml"` (relative to this file) loads that methodology first and merges
    this file's settings over it (tables merge key by key, other values replace), so a variant
    such as configs/staging-methodology.toml states only what differs.
    """
    path = Path(path).resolve()
    if path in _seen:
        raise ValueError("Methodology files extend each other in a cycle")
    data = tomllib.loads(path.read_text())
    parent = data.pop("extends", None)
    if parent is None:
        return data
    if not isinstance(parent, str) or not parent:
        raise ValueError("extends must name a methodology file")
    return merge(read(path.parent / parent, (*_seen, path)), data)


def events(path=None):
    """Validated [events] thresholds of a methodology file, over the defaults, plus the
    top-level hosting_usage_types (detect.py's new_hosting_asn)."""
    data = read(path) if path is not None and Path(path).exists() else {}
    section = dict(data.get("events", {}))
    # Older files kept hosting_usage_types under [events]; the top-level setting wins.
    usage = data.get("hosting_usage_types", section.pop("hosting_usage_types", None))
    if set(section) - set(EVENTS):
        raise ValueError("Unknown event setting")
    result = EVENTS | section
    for key, value in result.items():
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or value < 0
            or (type(EVENTS[key]) is int and type(value) is not int)
        ):
            raise ValueError(f"Invalid event setting {key}")
    if result["latency_spike_ratio"] <= 1 or result["pulse_latency_ratio"] <= 1:
        raise ValueError("Latency ratios must exceed one")
    result["hosting_usage_types"] = _usage_types(HOSTING_USAGE_TYPES if usage is None else usage)
    return result


def load(path):
    config = read(path)
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
