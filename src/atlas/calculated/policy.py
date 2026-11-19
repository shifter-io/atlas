"""Validated, immutable, data-only scoring configuration."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import tomllib
from pathlib import Path

from ..browser.contracts import BROWSER_TARGETS, RUN_TARGETS
from .outage_time import BASES

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
# requests in it; the uptime component is ready from min_batches finished pulse batches. basis is
# what the outage time is divided by (outage_time.BASES): "window" (the window length per country
# tested) or "measured_time" (each tested country's measured span).
UPTIME = {"min_successes": 1, "min_batches": 1, "basis": "window"}
# [value] basis: how the value component is scored, "ratio" (100 x cheapest list $/GB / yours)
# "rank" (by place on the HTTP cost per 1,000 successful requests, calculated/value_rank.py) or
# "rank_all_targets" (by place on the card targets' bill, calculated/value_all.py).
VALUE_BASES = ("ratio", "rank", "rank_all_targets")
# [value] price: the $/GB every cost and the value component use, "lowest" (the lowest published
# rate, price_per_gb_lowest) or "first_200gb_plan" (the first plan of 200 GB or more, else the
# largest plan: price_per_gb_basis, COST-BASIS-200GB-1).
VALUE_PRICES = ("lowest", "first_200gb_plan")
# [value] browser_window: the window whose browser card targets price the shorter windows' bill
# (value_all.reprice(), COVERAGE-ALERT-1), none without the key.
BROWSER_WINDOWS = ("7d", "30d")
# Time-weighted window metrics ([slots]): a time slot (the cohort's pulse interval) votes on
# latency only with at least min_latency_successes successful requests.
SLOTS = {"min_latency_successes": 30}
# DB-IP usage types that mark an exit address as a datacenter/hosting address (dcAsns, and the
# new_hosting_asn tape event). A top-level methodology setting, so it is part of the id.
HOSTING_USAGE_TYPES = ["hosting", "datacenter"]
# The one target every latency figure comes from (light load, under load, speed, breakdowns, the
# load factor): the census target. Other targets count for everything but latency.
LATENCY_TARGET = "generic_http"
# speed_latency: which p50 of the latency target the speed component reads, "light_load" (its
# pulses, latency_ms; the owner's decision of 2026-11-16, SPEED-PULSE-1) or "under_load" (its
# daily tests, census and load runs, latency_under_load_ms; the rule before it).
SPEED_LATENCIES = {"light_load": "latency_ms", "under_load": "latency_under_load_ms"}
# [latency] (LATENCY-BASKET-1, calculated/latency_basket.py): the network headline latency over a
# fixed basket of countries, each weighing the same: their aggregate, and the value of a basket
# country a network has no latency in.
LATENCY_BASKET = {"basket", "aggregate", "penalty"}
LATENCY_AGGREGATES = ("mean", "median")
LATENCY_PENALTIES = ("slowest_network",)
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
# Each HTTP target's difficulty, its challenge_overall weight, without an [http_difficulty] table.
HTTP_DIFFICULTY = {"generic_http": 1, "generic_https": 1}
# The Success x/10 ([success_score], SUCCESS-ALL-8-1): each card target's success by tries,
# max(floor, 10 - slope x the gap to the best network in percentage points), equal weights.
SUCCESS_SCORE = {"basis": "card_targets", "slope": 1.0, "floor": 1.0}
# The completed tries of a browser target's engine a network needs in a window to be ranked for
# it, without a browser_min_requests setting: the HTTP targets' (target_scores.MIN_REQUESTS).
BROWSER_MIN_REQUESTS = 100
# browser_window_rule: how a window holds a browser run's tries (browser.py), "bucket" (the rows
# whose bucket starts in it) without the setting, "whole_run" (BROWSER-WHOLE-RUN-1: the run whole,
# in the window that holds its end).
BROWSER_WINDOW_RULES = ("bucket", "whole_run")
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
    "http_difficulty",
    "browser_min_requests",
    "browser_window_rule",
    "country_verdicts",
    "country_min_ips",
    "success_score",
    "value",
    "speed_latency",
    "latency",
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
    _uptime(config.get("uptime", {}))
    value_basis(config)
    _counts(config.get("slots", {}), SLOTS, 1, "slot")
    _counts(config.get("payload", {}), PAYLOAD, 0, "payload")
    _usage_types(config.get("hosting_usage_types", HOSTING_USAGE_TYPES))
    _groups(config)
    _difficulty(config)
    if latency_target(config) not in config["required_targets"]:
        raise ValueError("The latency target must be a required target")
    speed_latency(config)
    latency_basket(config)
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
    minimum = browser_min_requests(config)
    if type(minimum) is not int or minimum <= 0:
        raise ValueError("Invalid browser_min_requests")
    if config.get("browser_window_rule", "bucket") not in BROWSER_WINDOW_RULES:
        raise ValueError("Invalid browser_window_rule")
    if type(config.get("country_verdicts", False)) is not bool:
        raise ValueError("Invalid country_verdicts")
    minimum = config.get("country_min_ips")
    if (minimum is not None or config.get("country_verdicts")) and (
        type(minimum) is not int or minimum <= 0
    ):
        raise ValueError("Invalid country_min_ips")
    _success_score(config.get("success_score", SUCCESS_SCORE))


def _success_score(found):
    """[success_score]: basis card_targets, a positive slope and a floor in [0, 10)."""
    if not isinstance(found, dict) or set(found) != set(SUCCESS_SCORE):
        raise ValueError("Invalid success_score")
    slope, floor = found["slope"], found["floor"]
    numbers = all(type(v) in (int, float) for v in (slope, floor))
    if found["basis"] != SUCCESS_SCORE["basis"] or not numbers or slope <= 0 or not 0 <= floor < 10:
        raise ValueError("Invalid success_score")


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


def speed_latency(config):
    """The metric the speed component reads (SPEED_LATENCIES), the pulses' "light_load" by
    default."""
    basis = config.get("speed_latency", "light_load")
    if not isinstance(basis, str) or basis not in SPEED_LATENCIES:
        raise ValueError("Invalid speed latency")
    return basis


def latency_basket(config):
    """The validated [latency] table ({"basket": upper-case country codes, "aggregate",
    "penalty"}), None without one: the pooled latency of every country a network is tested in."""
    found = config.get("latency")
    if found is None:
        return None
    if not isinstance(found, dict) or set(found) != LATENCY_BASKET:
        raise ValueError("Invalid latency settings")
    basket = found["basket"]
    codes = isinstance(basket, list) and all(
        isinstance(c, str) and len(c) == 2 and c.isalpha() and c.isupper() for c in basket
    )
    if (
        not codes
        or not basket
        or len(set(basket)) != len(basket)
        or found["aggregate"] not in LATENCY_AGGREGATES
        or found["penalty"] not in LATENCY_PENALTIES
    ):
        raise ValueError("Invalid latency settings")
    return found


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
    browser, group or source targets; a group without one takes its hardest source's.
    [http_difficulty] (optional): one for each HTTP target, and only them. Both are owner-set."""
    http = config.get("http_difficulty", HTTP_DIFFICULTY)
    if not _levels(http) or set(http) != set(HTTP_DIFFICULTY):
        raise ValueError("Invalid HTTP difficulty")
    values = config.get("browser_difficulty")
    if values is None:
        return
    groups = config.get("target_groups", {})
    known = {*BROWSER_TARGETS, *groups, *(s for v in groups.values() for s in v)}
    if not _levels(values) or set(values) - known or set(RUN_TARGETS) - set(values):
        raise ValueError("Invalid browser difficulty")


def _levels(values):
    """A table of DIFFICULTY integers."""
    ok = isinstance(values, dict)
    return ok and all(type(v) is int and v in DIFFICULTY for v in values.values())


def http_difficulty(config):
    """Each HTTP target's difficulty: [http_difficulty], else HTTP_DIFFICULTY."""
    return dict(config.get("http_difficulty", HTTP_DIFFICULTY))


def browser_min_requests(config):
    """browser_min_requests: the completed tries of a browser target's engine a network needs in
    a window to be ranked for it (SCORE-BROWSER-MIN-1), else BROWSER_MIN_REQUESTS."""
    return config.get("browser_min_requests", BROWSER_MIN_REQUESTS)


def browser_whole_runs(config):
    """Whether a browser run counts whole, by its finish, in the window that holds its end
    (browser_window_rule "whole_run", BROWSER-WHOLE-RUN-1); without the setting its rows count by
    their bucket's start."""
    return config.get("browser_window_rule") == "whole_run"


def country_verdicts(config):
    """The unique IPs a network's country needs in its latest daily test to count
    (country_min_ips, COUNTRY-MIN-IPS-1) with the methodology's country_verdicts, else None:
    every country counts. The country rule of verdicts.py and its readers take this value."""
    return config["country_min_ips"] if config.get("country_verdicts", False) else None


def success_score(config):
    """[success_score]: the Success x/10's basis, slope and floor (target_scores.success_overall()),
    else SUCCESS_SCORE."""
    return dict(config.get("success_score", SUCCESS_SCORE))


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


def _uptime(settings):
    counts = {k: v for k, v in settings.items() if k != "basis"}
    _counts(counts, {k: v for k, v in UPTIME.items() if k != "basis"}, 1, "uptime")
    if settings.get("basis", UPTIME["basis"]) not in BASES:
        raise ValueError("Invalid uptime settings")


def value_basis(config):
    """[value] basis (VALUE_BASES), "ratio" without a [value] table."""
    settings = config.get("value", {})
    if not isinstance(settings, dict) or set(settings) - {"basis", "price", "browser_window"}:
        raise ValueError("Invalid value settings")
    basis = settings.get("basis", "ratio")
    if basis not in VALUE_BASES or value_price(config) not in VALUE_PRICES:
        raise ValueError("Invalid value basis")
    if settings.get("browser_window", BROWSER_WINDOWS[0]) not in BROWSER_WINDOWS:
        raise ValueError("Invalid value browser_window")
    return basis


def value_price(config):
    """[value] price (VALUE_PRICES), "lowest" without one."""
    return config.get("value", {}).get("price", "lowest")


def value_browser_window(config):
    """[value] browser_window (BROWSER_WINDOWS), None without one."""
    return config.get("value", {}).get("browser_window")


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
