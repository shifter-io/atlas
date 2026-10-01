"""Pure window aggregation. No estimates, percentile averaging or inferred measurements."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from ..stats import Histogram
from .policy import composite, curve_score, grade, uptime_settings

# Challenge and block rates come from the typed outcome counters of v6 buckets and uptime from
# pulse batches, so they are no longer listed here; a None metric means nothing measured them.
UNAVAILABLE = {
    "ttfb_p50": "TTFB is not recorded by this target version",
    "stickiness_hold_rate": "Sticky-session survival measurements are required",
    "blacklist_hit_rate": "A timestamped reputation check is required",
    "leak_checks": "Leak measurements are required",
    "billed_bytes": "Response body size is not billed wire traffic",
    "cost_per_1k_success": "Verified pricing and billed usage are required",
    "price_per_gb": "Verified effective-dated pricing is required",
    "requests_per_ip": "Consecutive rotation behavior is not recorded",
    "ip_latency": "Per-IP timing is not recorded",
}
UPTIME_UNAVAILABLE = "No finished pulse batches in this window"
UPTIME_INCOMPLETE = "Fewer finished pulse batches than the methodology requires"
PLACE = ("country_code", "country", "region_code", "region", "city")
NETWORK = ("asn", "name", "usage_type")
# Payload breakdown name -> calculated_breakdowns dimension code.
BREAKDOWNS = {"country": "c", "region": "r", "city": "g", "asn": "a", "country_asn": "x"}
OUTCOMES = ("challenges", "blocks", "bytes_up", "bytes_down")
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
    outcomes = Counter()
    completed = successes = body = classified = 0
    measured = False
    for row in rows:
        b = row["counters"]
        completed += b["completed"]
        successes += b["successes"]
        body += b["response_body_bytes"]
        errors.update(b["errors"])
        histogram.merge(Histogram(Counter(dict(b["latency_histogram"]["bins"]))))
        # Only v6 buckets classify outcomes and count wire bytes; older ones stay out of the rates.
        if row.get("outcome"):
            measured = True
            classified += b["completed"]
            outcomes.update({k: b[k] for k in OUTCOMES})
    return {
        "completed": completed,
        "successes": successes,
        "errors": completed - successes,
        "error_categories": dict(errors),
        "body_bytes": body,
        "success_rate": successes / completed if completed else None,
        "timeout_rate": errors["timeout"] / completed if completed else None,
        "challenge_rate": outcomes["challenges"] / classified if classified else None,
        "block_rate": outcomes["blocks"] / classified if classified else None,
        "uptime": None,
        "bytes_up": outcomes["bytes_up"] if measured else None,
        "bytes_down": outcomes["bytes_down"] if measured else None,
        "latency_ms": histogram.percentiles(),
        "coverage": len({r["bucket_start"] for r in rows}) / (hours * 12),
        "coverage_basis": "5-minute buckets",
        "last_observed": max((r["last_observed"] for r in rows), default=None),
        "latency_basis": "successful requests",
        "units": {
            "latency_ms": "milliseconds",
            "success_rate": "fraction",
            "body_bytes": "response body bytes",
            "uptime": "fraction of time (outages over countries tested)",
            "bytes": "wire bytes",
        },
    }


def pool(observations, since, hours):
    ips = {}
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
    countries, regions, cities, asns = set(), set(), set(), set()
    for entry in ips.values():
        o = entry["row"]
        cc = o.get("country_code")
        region = o.get("region_code") or o.get("region")
        if cc:
            countries.add(cc)
            if region:
                regions.add((cc, region))
            if o.get("city"):
                cities.add((cc, region, o["city"]))
        if o.get("asn") is not None:
            asns.add(o["asn"])
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
        "countries": len(countries),
        "regions": len(regions),
        "cities": len(cities),
        "asns": len(asns),
        "datacenter_asn_share": host / known if known else None,
        "classification_coverage": known / size if size else None,
        "geo_accuracy": matched_observations / known_country
        if known_country and any(o["requested_country"] for o in observations)
        else None,
        "enrichment_coverage": sum(bool(e["row"].get("matched")) for e in ips.values()) / size
        if size
        else None,
        "observations": sum(e["count"] for e in ips.values()),
    }, ips


def _memberships(place, network):
    """(breakdown, key, label) for every breakdown row a location and network fall in."""
    result = []
    located = place is not None and place[0] is not None
    if located:
        cc, country, region_code, region, city = place
        result.append(("country", cc, {"country_code": cc, "country": country}))
        if region_code is not None or region is not None:
            result.append(
                (
                    "region",
                    place[:4],
                    {
                        "country_code": cc,
                        "country": country,
                        "region": region,
                        "region_code": region_code,
                    },
                )
            )
        result.append(
            (
                "city",
                place,
                {
                    "country_code": cc,
                    "country": country,
                    "region": region,
                    "region_code": region_code,
                    "city": city,
                },
            )
        )
    if network is not None and network[0] is not None:
        asn = dict(zip(NETWORK, network, strict=True))
        result.append(("asn", network, asn))
        if located:
            result.append(
                ("country_asn", (place[0], network), {"country_code": cc, "country": country} | asn)
            )
    return result


def breakdowns(ips, geo):
    """Success, outcome, latency and IP counts per country, region, city, ASN and country x ASN.

    ips are pool() entries (each IP once, labelled by its DB-IP enrichment); geo rows carry
    run_geo_buckets counters per exit location and network. Requests without an exit address
    have no location: they count only towards the country the run requested (fallback), so a
    country's success rate includes its failures. Country rows are keyed by the country code;
    when several names occur, the smallest is used.
    """
    rows = {name: {} for name in BREAKDOWNS}

    def entry(name, key, label):
        row = rows[name].get(key)
        if row is None:
            row = rows[name][key] = {
                "label": label,
                "ips": 0,
                "observations": 0,
                "completed": 0,
                "successes": 0,
                "challenges": 0,
                "blocks": 0,
                "histogram": Histogram(Counter()),
            }
        elif label.get("country") is not None and (
            row["label"].get("country") is None or label["country"] < row["label"]["country"]
        ):
            row["label"] = row["label"] | {"country": label["country"]}
        return row

    for e in ips.values():
        o = e["row"]
        place = tuple(o.get(f) for f in PLACE)
        network = (o.get("asn"), o.get("asn_organization"), o.get("usage_type"))
        for name, key, label in _memberships(place, network):
            row = entry(name, key, label)
            row["ips"] += 1
            row["observations"] += e["count"]

    def add(row, g):
        for k in ("completed", "successes", "challenges", "blocks"):
            row[k] += g[k]
        row["histogram"].merge(Histogram(Counter(dict(g["histogram"]))))

    fallback = []
    for g in geo:
        memberships = _memberships(g["location"], g["network"])
        if not any(name == "country" for name, _, _ in memberships) and g.get("fallback"):
            fallback.append(g)
        for name, key, label in memberships:
            add(entry(name, key, label), g)
    for g in fallback:
        cc, country = g["fallback"]
        row = rows["country"].get(cc) or entry(
            "country", cc, {"country_code": cc, "country": country}
        )
        add(row, g)
    result = {}
    for name, members in rows.items():
        out = []
        for row in members.values():
            latency = row.pop("histogram").percentiles()
            label = row.pop("label")
            out.append(
                label
                | row
                | {
                    "success_rate": row["successes"] / row["completed"]
                    if row["completed"]
                    else None,
                    "p50": latency["p50"],
                    "p90": latency["p90"],
                    "p99": latency["p99"],
                }
            )
        out.sort(
            key=lambda r: (
                -r["completed"],
                -r["ips"],
                json.dumps([r.get(f) for f in (*PLACE, *NETWORK)], default=str),
            )
        )
        result[name] = out
    return result


def pool_lists(rows):
    """The v1 pool lists (IP counts per geography, country and ASN), from the breakdowns."""

    def pick(name, fields):
        return sorted(
            ({k: r[k] for k in (*fields, "ips", "observations")} for r in rows[name] if r["ips"]),
            key=lambda x: (-x["ips"], str(x)),
        )

    return {
        "geo": pick("city", ("country_code", "country", "region", "region_code", "city")),
        "countries_breakdown": pick("country", ("country_code", "country")),
        "asns_breakdown": pick("asn", NETWORK),
    }


def slot_coverage(m, slots, since, end):
    """Set m's coverage to the share of pulse slots in [since, end) the product filled.

    slots are the product's pulse batch runs that count (done, or failed for a reason of the
    provider's); a slot is filled by a done run with at least one completed request.
    """
    due = [b for b in slots if since <= b["scheduled_for"] < end]
    filled = sum(b["status"] == "done" and b["requests"] > 0 for b in due)
    m["coverage"] = filled / len(due) if due else 0.0
    m["coverage_basis"] = "pulse slots"
    m["coverage_slots"] = {"due": len(due), "filled": filled}
    return m


def uptime(batches, since, end, min_successes):
    """Share of done pulse batch runs in [since, end) with at least min_successes successes."""
    rows = [b for b in batches if since <= b["scheduled_for"] < end and b["status"] == "done"]
    up = sum(b["successes"] >= min_successes for b in rows)
    return {"batches": len(rows), "up": up, "value": up / len(rows) if rows else None}


def downtime(outages, countries, since, end):
    """Seconds of [since, end) a product was down, summed over `countries`.

    Per country, the union of its outage intervals clipped to the window (an open outage, or
    one that ended after `end`, runs to `end`), so overlapping outages are counted once.
    """
    total = 0.0
    for country in countries:
        spans = sorted(
            (max(o["started_at"], since), min(o["ended_at"] or end, end))
            for o in outages
            if o["country"] == country
        )
        start = stop = None
        for a, b in spans:
            if b <= a:
                continue
            if stop is None or a > stop:
                if stop is not None:
                    total += (stop - start).total_seconds()
                start, stop = a, b
            else:
                stop = max(stop, b)
        if stop is not None:
            total += (stop - start).total_seconds()
    return total


def outage_uptime(outages, tested, since, end):
    """Time-based uptime: 1 - downtime / (window seconds x countries tested in the window).

    tested maps each country the product had a started batch run in to the end of its latest
    one (or `end` while running); a country counts when that is within the window.
    """
    countries = sorted(c for c, last in tested.items() if last >= since)
    down = downtime(outages, countries, since, end)
    available = (end - since).total_seconds() * len(countries)
    return {
        "value": min(1.0, max(0.0, 1 - down / available)) if available else None,
        "downtime_s": round(down, 3),
        "countries": len(countries),
        "basis": "outage time",
    }


def outage_list(outages, since, end, limit=50):
    """The newest outages overlapping [since, end); one still open at `end` has ended_at None."""
    rows = [
        o
        for o in outages
        if o["started_at"] < end and (o["ended_at"] is None or o["ended_at"] > since)
    ]
    rows.sort(key=lambda o: (o["started_at"], o["outage_id"]), reverse=True)
    result = []
    for o in rows[:limit]:
        ended = o["ended_at"] if o["ended_at"] is not None and o["ended_at"] <= end else None
        result.append(
            {
                "country": o["country"],
                "region": o["region"],
                "source": o["source"],
                "started_at": o["started_at"],
                "ended_at": ended,
                "duration_s": round(((ended or end) - o["started_at"]).total_seconds(), 3),
            }
        )
    return result


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


def product_scores(targets, p, evidence, config, kind, measured_uptime=None):
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
    # Measured uptime (outage time over the countries tested) wins once enough pulse batches
    # finished; evidence is the fallback.
    batches = (measured_uptime or {}).get("batches", 0)
    value = (measured_uptime or {}).get("value")
    pct = 100 * value if batches and value is not None else None
    if pct is not None and batches >= uptime_settings(config)["min_batches"]:
        inputs["uptime"] = component(curve_score(curves["uptime"], pct), inputs=pct)
    elif "uptime_pct" in evidence:
        inputs["uptime"] = component(curve_score(curves["uptime"], evidence["uptime_pct"]))
    else:
        inputs["uptime"] = component(
            curve_score(curves["uptime"], pct),
            "incomplete",
            UPTIME_INCOMPLETE if batches else UPTIME_UNAVAILABLE,
            inputs=pct,
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
