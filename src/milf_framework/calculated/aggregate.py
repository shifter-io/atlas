"""Pure window aggregation. No estimates, percentile averaging or inferred measurements."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from ..stats import Histogram
from .policy import composite, curve_score, grade

UNAVAILABLE = {
    "uptime_pct": "Continuous availability measurements are required",
    "ttfb_p50": "TTFB is not recorded by this target version",
    "captcha_rate": "Challenge classification is not recorded by this target version",
    "block_rate": "HTTP statuses alone do not establish block-page classification",
    "stickiness_hold_rate": "Sticky-session survival measurements are required",
    "blacklist_hit_rate": "A timestamped reputation check is required",
    "leak_checks": "Leak measurements are required",
    "billed_bytes": "Response body size is not billed wire traffic",
    "cost_per_1k_success": "Verified pricing and billed usage are required",
    "price_per_gb": "Verified effective-dated pricing is required",
    "requests_per_ip": "Consecutive rotation behavior is not recorded",
    "ip_latency": "Per-IP timing is not recorded",
}
EVIDENCE_RANGES = {
    "uptime_pct": (0, 100),
    "blacklist_hit_rate": (0, 1),
    "claim_gap": (0, 1),
    "stickiness_hold_rate": (0, 1),
    "captcha_rate": (0, 1),
    "block_rate": (0, 1),
    "ttfb_p50": (0, 1e9),
    "billed_bytes": (0, 1e18),
    "price_per_gb": (0, 1e9),
    "cost_per_1k_success": (0, 1e12),
    "requests_per_ip": (0, 1e12),
}


def metrics(rows, hours):
    histogram = Histogram(Counter())
    errors = Counter()
    completed = successes = body = 0
    for row in rows:
        b = row["counters"]
        completed += b["completed"]
        successes += b["successes"]
        body += b["response_body_bytes"]
        errors.update(b["errors"])
        histogram.merge(Histogram(Counter(dict(b["latency_histogram"]["bins"]))))
    return {
        "completed": completed,
        "successes": successes,
        "errors": completed - successes,
        "error_categories": dict(errors),
        "body_bytes": body,
        "success_rate": successes / completed if completed else None,
        "timeout_rate": errors["timeout"] / completed if completed else None,
        "latency_ms": histogram.percentiles(),
        "coverage": len({r["bucket_start"] for r in rows}) / (hours * 12),
        "last_observed": max((r["last_observed"] for r in rows), default=None),
        "latency_basis": "successful requests",
        "units": {
            "latency_ms": "milliseconds",
            "success_rate": "fraction",
            "body_bytes": "response body bytes",
        },
    }


def pool(observations, since, hours):
    ips = {}
    geo = {}
    networks = {}
    country = {}
    matched_observations = known_country = 0
    for o in observations:
        entry = ips.setdefault(
            o["ip_id"],
            {"first": o["first_seen"], "last": o["last_seen"], "days": set(), "count": 0, "row": o},
        )
        entry["days"].add(o["bucket_start"].date())
        entry["count"] += o["count"]
        entry["first"] = min(entry["first"], o["first_seen"])
        entry["last"] = max(entry["last"], o["last_seen"])
        if o.get("country_code"):
            known_country += o["count"]
            if o["country_code"].lower() == (o["requested_country"] or "").lower():
                matched_observations += o["count"]
    for entry in ips.values():
        o = entry["row"]
        cc = o.get("country_code")
        region = o.get("region_code") or o.get("region")
        if cc:
            key = (cc, region, o.get("city"))
            g = geo.setdefault(
                key,
                {
                    "country_code": cc,
                    "country": o.get("country"),
                    "region": o.get("region"),
                    "region_code": o.get("region_code"),
                    "city": o.get("city"),
                    "ips": 0,
                    "observations": 0,
                },
            )
            g["ips"] += 1
            g["observations"] += entry["count"]
            c = country.setdefault(
                cc, {"country_code": cc, "country": o.get("country"), "ips": 0, "observations": 0}
            )
            c["ips"] += 1
            c["observations"] += entry["count"]
        if o.get("asn") is not None:
            key = (o["asn"], o.get("asn_organization"), o.get("usage_type"))
            n = networks.setdefault(
                key,
                {
                    "asn": o["asn"],
                    "name": o.get("asn_organization"),
                    "usage_type": o.get("usage_type"),
                    "ips": 0,
                    "observations": 0,
                },
            )
            n["ips"] += 1
            n["observations"] += entry["count"]
    size = len(ips)
    known = sum(bool(e["row"].get("usage_type")) for e in ips.values())
    host = sum(e["row"].get("usage_type") == "hosting" for e in ips.values())
    new = sum(e["row"]["product_first_seen"] >= since for e in ips.values())
    return {
        "unique_ips": size,
        "new_ips": new,
        "churn_rate": new / size if size else None,
        "recurring_share": sum(len(e["days"]) >= 2 for e in ips.values()) / size
        if size and hours > 24
        else None,
        "countries": len(country),
        "regions": len({(k[0], k[1]) for k in geo if k[1]}),
        "cities": len({k for k in geo if k[2]}),
        "asns": len({k[0] for k in networks}),
        "datacenter_asn_share": host / known if known else None,
        "classification_coverage": known / size if size else None,
        "geo_accuracy": matched_observations / known_country
        if known_country and any(o["requested_country"] for o in observations)
        else None,
        "enrichment_coverage": sum(bool(e["row"].get("matched")) for e in ips.values()) / size
        if size
        else None,
        "observations": sum(e["count"] for e in ips.values()),
        "geo": sorted(geo.values(), key=lambda x: (-x["ips"], str(x))),
        "countries_breakdown": sorted(country.values(), key=lambda x: -x["ips"]),
        "asns_breakdown": sorted(networks.values(), key=lambda x: (-x["ips"], str(x))),
    }, ips


def series(rows, observations, start, end, hours):
    step = timedelta(minutes=5) if hours == 24 else timedelta(days=1)
    buckets = defaultdict(list)
    seen = defaultdict(set)

    def slot(t):
        return start + ((t - start) // step) * step

    for r in rows:
        buckets[slot(r["bucket_start"])].append(r)
    for o in observations:
        seen[slot(o["bucket_start"])].add(o["ip_id"])
    out = []
    t = start
    while t < end:
        m = metrics(buckets[t], step.total_seconds() / 3600)
        out.append(
            {
                "at": t,
                "completed": m["completed"],
                "successes": m["successes"],
                "success_rate": m["success_rate"],
                "latency_ms": m["latency_ms"],
                "unique_ips": len(seen[t]) if buckets[t] else None,
            }
        )
        t += step
    return out


def component(value, state="ready", reason=None, inputs=None):
    return {
        "value": value,
        "state": state if value is not None else "unavailable",
        "reason": reason,
        "inputs": inputs,
    }


def target_score(m, config, benchmark=True):
    reasons = []
    if not benchmark:
        reasons.append("Run is not assigned to the benchmark schedule")
    if m["completed"] < config["minimum_requests"]:
        reasons.append("Insufficient requests")
    if m["coverage"] < config["minimum_coverage"]:
        reasons.append("Insufficient time coverage")
    state = "ready" if not reasons else "incomplete"
    components = {
        "reliability": component(
            curve_score(config["curves"]["reliability"], m["success_rate"]),
            state,
            inputs=m["success_rate"],
        ),
        "speed": component(
            curve_score(config["curves"]["speed"], m["latency_ms"]["p50"]),
            state,
            inputs=m["latency_ms"]["p50"],
        ),
    }
    score = composite(components, config["target_weights"])
    return {
        "score": score,
        "grade": grade(score, config) if score is not None else None,
        "state": state if score is not None else "incomplete",
        "components": components,
        "weights": config["target_weights"],
        "reasons": reasons,
        "rank": None,
        "previous_rank": None,
        "scored_at": None,
        "valid_until": None,
    }


def product_scores(targets, p, evidence, config, kind):
    curves = config["curves"]
    required = config["required_targets"]
    inputs = {}
    for key in ("reliability", "speed"):
        components = [targets[t]["score"]["components"][key] for t in required]
        weights = config["benchmark_weights"]
        value = (
            sum(c["value"] * weights[t] for t, c in zip(required, components, strict=True))
            / sum(weights.values())
            if all(c["value"] is not None for c in components)
            else None
        )
        ready = all(c["state"] == "ready" for c in components) and all(
            targets[t]["score"]["state"] == "ready" for t in required
        )
        inputs[key] = component(
            value,
            "ready" if ready else "incomplete",
            None if ready else "Required benchmark targets are incomplete",
        )
    inputs["uptime"] = component(
        curve_score(curves["uptime"], evidence.get("uptime_pct")),
        reason=None if "uptime_pct" in evidence else UNAVAILABLE["uptime_pct"],
    )
    pool_ready = p["unique_ips"] > 0 and p["enrichment_coverage"] == 1
    inputs["coverage"] = component(
        curve_score(curves["coverage"], p["unique_ips"]) if p["unique_ips"] else None,
        inputs=p["unique_ips"],
    )
    diversity = (
        100
        * sum(
            curve_score({"kind": "log", "low": v[0], "high": v[1]}, p[k]) / 100 * v[2]
            for k, v in curves["diversity"].items()
        )
        if pool_ready
        else None
    )
    inputs["diversity"] = component(
        diversity, reason=None if pool_ready else "Complete local enrichment required"
    )
    dc = (
        100
        if kind == "datacenter" and pool_ready
        else curve_score(curves["dcAsns"], p["datacenter_asn_share"])
    )
    inputs["dcAsns"] = component(
        dc,
        "ready" if p["classification_coverage"] == 1 else "incomplete",
        "DB-IP usage classification; unknown records remain unclassified",
    )
    claim = evidence.get("claim_gap")
    blacklist = evidence.get("blacklist_hit_rate")
    auth = (
        max(
            0, min(100, (1 - claim) * 100 - blacklist * curves["authenticity"]["blocklist_penalty"])
        )
        if (claim is not None and blacklist is not None)
        else None
    )
    inputs["authenticity"] = component(
        auth,
        reason=None
        if auth is not None
        else "Verified product claim and reputation measurements required",
    )
    inputs["value"] = component(
        curve_score(curves["value"], evidence.get("cost_per_1k_success")),
        reason=None if "cost_per_1k_success" in evidence else UNAVAILABLE["cost_per_1k_success"],
    )
    result = {}
    profiles = config.get("profiles", {}) | {"balanced": config["weights"]}
    for name, weights in profiles.items():
        value = composite(inputs, weights)
        missing_targets = [t for t in required if targets[t]["score"]["state"] != "ready"]
        if missing_targets:
            value = None
        result[name] = {
            "score": value,
            "grade": grade(value, config) if value is not None else None,
            "state": "ready" if value is not None else "incomplete",
            "components": inputs,
            "weights": weights,
            "reasons": (
                [f"Required benchmark incomplete: {t}" for t in missing_targets]
                + [
                    c["reason"] or f"{k} not ready"
                    for k, c in inputs.items()
                    if weights[k] > 0 and c["state"] != "ready"
                ]
            ),
            "rank": None,
            "previous_rank": None,
            "scored_at": None,
            "valid_until": None,
        }
    return result


def iso(value):
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(type(value).__name__)
