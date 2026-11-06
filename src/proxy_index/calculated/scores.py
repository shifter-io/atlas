"""Component and peer-group scores: raw component values and readiness per target and per
product, scored by ratio to the leader of their peer group (aggregate.py re-exports them)."""

from __future__ import annotations

from .policy import DIMENSIONS, KEYS, composite, grade, latency_target, ratio, uptime_settings

UPTIME_UNAVAILABLE = "No finished pulse batches in this window"
UPTIME_INCOMPLETE = "Fewer finished pulse batches than the methodology requires"
PRICE_MISSING = "Verified effective-dated pricing (lowest $/GB) is required"
UNDER_LOAD_NONE = "No load test in this window"
NOT_LATENCY_TARGET = "Latency is measured on the latency target only"
# Whether a higher raw value is better, per component scored by ratio to the leader. Reliability
# and speed are scored per target; diversity mixes the ratios of its pool dimensions.
HIGHER = {
    "reliability": True,
    "speed": False,
    "uptime": True,
    "coverage": True,
    "dcAsns": True,
    "authenticity": True,
    "value": False,
}


def component(value, state="ready", reason=None, inputs=None):
    return {
        "value": value,
        "state": state if value is not None else "unavailable",
        "reason": reason,
        "inputs": inputs,
    }


def target_score(m, config, target=None):
    """Readiness and raw values of a target score; peer_scores() scores them against peers.

    The raw components hold the time-weighted success rate of every request (reliability) and
    the p50 latency under load (speed). Readiness counts every request of the target. Only the
    latency target has a speed: any other target scores on reliability alone (speed weight 0).
    """
    reasons = []
    if m["completed"] < config["minimum_requests"]:
        reasons.append("Insufficient requests")
    if m["coverage"] < config["minimum_coverage"]:
        reasons.append("Insufficient time coverage")
    state = "ready" if not reasons else "incomplete"
    timed_target = target in (None, latency_target(config))
    speed = (m.get("latency_under_load_ms") or {}).get("p50") if timed_target else None
    return {
        "state": state,
        "components": {
            "reliability": component(m["success_rate"], state),
            "speed": component(
                speed,
                state,
                None
                if speed is not None
                else (m.get("under_load") or {}).get("reason") or UNDER_LOAD_NONE
                if timed_target
                else NOT_LATENCY_TARGET,
            ),
        },
        "weights": config["target_weights"]
        if timed_target
        else config["target_weights"] | {"speed": 0},
        "reasons": reasons,
    }


def product_inputs(p, prices, config, kind, uptime=(None, None)):
    """Raw values and readiness of a product's components besides reliability and speed, which
    peer_scores() derives from the target scores.

    p is the window's pool() counts with "daily" (daily_average()): coverage and diversity use
    the daily averages. prices is the product's effective pricing (price_per_gb_lowest scores
    value); uptime is the window's (measured uptime, metric evidence), both optional (the
    evidence's uptime_pct is the uptime fallback). A raw
    component's value is what is compared with peers; diversity's is the four daily dimension
    counts (also kept in its inputs). IPs without a DB-IP field never block a component: shares
    leave them out. "fixed" is a score that does not depend on peers (datacenter products keep
    100 on dcAsns).
    """
    measured_uptime, evidence = uptime
    inputs = {"uptime": _uptime(config, measured_uptime, evidence or {})}
    inputs |= _pool_inputs(p, kind)
    lowest = (prices or {}).get("lowest_per_gb")
    inputs["value"] = component(lowest, reason=None if lowest is not None else PRICE_MISSING)
    return inputs


def _uptime(config, measured_uptime, evidence):
    """The uptime component: measured uptime (outage time over the countries tested) wins once
    enough pulse batches finished; evidence is the fallback."""
    batches = (measured_uptime or {}).get("batches", 0)
    value = (measured_uptime or {}).get("value")
    pct = 100 * value if batches and value is not None else None
    if pct is not None and batches >= uptime_settings(config)["min_batches"]:
        return component(pct)
    if "uptime_pct" in evidence:
        return component(evidence["uptime_pct"])
    return component(pct, "incomplete", UPTIME_INCOMPLETE if batches else UPTIME_UNAVAILABLE)


def _pool_inputs(p, kind):
    """The coverage, diversity, dcAsns and authenticity components of the pool counts p (see
    product_inputs())."""
    daily = p["daily"]
    inputs = {}
    seen = p["unique_ips"] > 0
    inputs["coverage"] = component(daily["unique_ips"] if seen else None)
    counts = {k: daily[k] for k in DIMENSIONS}
    inputs["diversity"] = component(
        counts if seen else None,
        reason=None if seen else "No exit IPs in this window",
        inputs=counts,
    )
    share = p["datacenter_asn_share"]
    clean = 1 - share if share is not None else None
    inputs["dcAsns"] = component(
        clean, reason=None if clean is not None else "No exit IP has a DB-IP usage type"
    )
    if kind == "datacenter" and seen:
        inputs["dcAsns"] = {
            "value": clean,
            "state": "ready",
            "reason": None,
            "inputs": None,
            "fixed": 100.0,
        }
    accuracy = p["geo_accuracy"]
    inputs["authenticity"] = component(
        100 * accuracy if accuracy is not None else None,
        reason=None
        if accuracy is not None
        else "No exit IP with a DB-IP country for a requested country",
    )
    return inputs


def leader(raw, higher):
    """The best value among eligible raw components and how many there are.

    raw holds (component, fresh) pairs; a component is eligible when it is ready, measured and
    its product's measurements are fresh enough for a new score.
    """
    values = [
        c["value"] for c, fresh in raw if fresh and c["state"] == "ready" and c["value"] is not None
    ]
    return (max if higher else min)(values) if values else None, len(values)


def versus(raw, best, peers, higher):
    """A raw component scored against the leader (without a ready peer, against itself)."""
    best = raw["value"] if best is None else best
    return component(
        raw.get("fixed", ratio(raw["value"], best, higher)),
        raw["state"],
        raw["reason"],
        {"value": raw["value"], "best": best, "peers": peers},
    )


def score_entry(value, state, weighted, reasons, config):
    """A score of value (None: incomplete); weighted is its (components, weights)."""
    components, weights = weighted
    return {
        "score": value,
        "grade": grade(value, config) if value is not None else None,
        "state": state if value is not None else "incomplete",
        "components": components,
        "weights": weights,
        "reasons": reasons,
        "rank": None,
        "previous_rank": None,
        "scored_at": None,
        "valid_until": None,
    }


def peer_scores(members, config):
    """Score one peer group, the products of one kind in one window, by ratio to the leader.

    Each member has "config" (its effective methodology), "fresh" (its benchmark measurements
    are recent enough for a new score), "inputs" (product_inputs()) and "targets", per target
    target_score() with its own "fresh". Every component scores 100 x value / best (lower is
    better: 100 x best / value) against the best ready, fresh value of the group, so retained,
    expired and incomplete scores never set the best. Target scores combine their reliability
    and speed ratios with target_weights; product reliability and speed are the
    benchmark_weights mean of the required targets' ratios. Returns, per member,
    {"targets": {target: score}, "scores": {profile: score}}.
    """
    results = [{"targets": {}, "scores": {}} for _ in members]
    for target in sorted({t for m in members for t in m["targets"]}):
        _target_scores(target, members, results, config)
    raw = [dict(m["inputs"]) for m in members]
    for m, r, inputs in zip(members, results, raw, strict=True):
        _benchmark_components(m, r, inputs)
    for key in ("reliability", "speed"):
        peers = sum(
            m["fresh"] and inputs[key]["state"] == "ready"
            for m, inputs in zip(members, raw, strict=True)
        )
        for inputs in raw:
            inputs[key]["inputs"]["peers"] = peers
    for key, higher in HIGHER.items():
        if key in ("reliability", "speed"):
            continue
        best, peers = leader(
            [(i[key], m["fresh"]) for m, i in zip(members, raw, strict=True)], higher
        )
        for inputs in raw:
            inputs[key] = versus(inputs[key], best, peers, higher)
    _diversity(members, raw)
    for m, r, inputs in zip(members, results, raw, strict=True):
        _profile_scores(m, r, inputs, config)
    return results


def _target_scores(target, members, results, config):
    """One target's scores in results: reliability and speed against the target's leaders."""
    group = [
        (m["targets"][target], r)
        for m, r in zip(members, results, strict=True)
        if target in m["targets"]
    ]
    for key, higher in (("reliability", True), ("speed", False)):
        best, peers = leader([(t["components"][key], t["fresh"]) for t, _ in group], higher)
        for t, r in group:
            r["targets"].setdefault(target, {})[key] = versus(
                t["components"][key], best, peers, higher
            )
    for t, r in group:
        components = r["targets"][target]
        r["targets"][target] = score_entry(
            composite(components, t["weights"]),
            t["state"],
            (components, t["weights"]),
            t["reasons"],
            config,
        )


def _benchmark_components(m, r, inputs):
    """A member's product reliability and speed from its target scores, and r["missing"]: the
    required targets without a fresh score."""
    required = m["config"]["required_targets"]
    ready = {t: r["targets"][t]["score"] is not None and m["targets"][t]["fresh"] for t in required}
    # Reliability averages the required targets (benchmark_weights); speed is the latency
    # target's alone.
    timed_target = latency_target(m["config"])
    for key, targets, weights in (
        ("reliability", required, m["config"]["benchmark_weights"]),
        ("speed", [timed_target], {timed_target: 1}),
    ):
        parts = [r["targets"][t]["components"][key] for t in targets]
        done = all(c["state"] == "ready" for c in parts) and all(ready.values())
        inputs[key] = component(
            sum(c["value"] * weights[t] for t, c in zip(targets, parts, strict=True))
            / sum(weights.values())
            if all(c["value"] is not None for c in parts)
            else None,
            "ready" if done else "incomplete",
            # A target's own reason (no load test, say) says more than the generic one.
            None
            if done
            else next(
                (c["reason"] for c in parts if c["reason"]),
                "Required benchmark targets are incomplete",
            ),
            {
                "targets": {
                    t: {k: c["inputs"][k] for k in ("value", "best")}
                    for t, c in zip(targets, parts, strict=True)
                }
            },
        )
    r["missing"] = [t for t in required if not ready[t]]


def _diversity(members, raw):
    """Diversity: each pool dimension against its own leader, mixed with [peer.diversity]."""
    eligible = [
        i["diversity"]["value"]
        for m, i in zip(members, raw, strict=True)
        if m["fresh"] and i["diversity"]["state"] == "ready" and i["diversity"]["value"]
    ]
    for m, inputs in zip(members, raw, strict=True):
        mix = m["config"]["peer"]["diversity"]
        own = inputs["diversity"]
        counts = own["inputs"]
        best = {k: max(d[k] for d in eligible) if eligible else counts[k] for k in DIMENSIONS}
        inputs["diversity"] = component(
            sum(ratio(counts[k], best[k], True) * w for k, w in mix.items()) / sum(mix.values())
            if own["value"]
            else None,
            own["state"],
            own["reason"],
            {k: {"value": counts[k], "best": best[k]} for k in DIMENSIONS}
            | {"peers": len(eligible)},
        )


def _profile_scores(m, r, inputs, config):
    """A member's score per weight profile (balanced included); none while a required target
    is missing."""
    cfg = m["config"]
    components = {k: inputs[k] for k in KEYS}
    profiles = cfg.get("profiles", {}) | {"balanced": cfg["weights"]}
    for name, weights in profiles.items():
        value = composite(components, weights)
        if r["missing"]:
            value = None
        r["scores"][name] = score_entry(
            value,
            "ready",
            (components, weights),
            [f"Required benchmark incomplete: {t}" for t in r["missing"]]
            + [
                c["reason"] or f"{k} not ready"
                for k, c in components.items()
                if weights[k] > 0 and c["state"] != "ready"
            ],
            config,
        )
    del r["missing"]
