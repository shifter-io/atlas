"""Pure window aggregation. No estimates, percentile averaging or inferred measurements."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from ..stats import Histogram
from .policy import DIMENSIONS, KEYS, composite, grade, ratio, uptime_settings

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
# Pool counts averaged over daily (24-hour) pools: coverage and diversity score from these.
DAILY = ("unique_ips", *DIMENSIONS)
DAY = timedelta(hours=24)


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


def daily_pools(observations, as_of, days=30):
    """Pool counts of each 24-hour day ending at as_of, newest first; None for a day without
    observations. Every window reuses them (24h is the first day, 7d the first seven)."""
    groups = defaultdict(list)
    for o in observations:
        day = -((o["bucket_start"] - as_of) // DAY) - 1
        if 0 <= day < days:
            groups[day].append(o)
    result = []
    for day in range(days):
        counts = pool(groups[day], as_of - (day + 1) * DAY, 24)[0] if groups[day] else None
        result.append({k: counts[k] for k in DAILY} if counts else None)
    return result


def daily_average(days, hours):
    """Average daily counts over the window's days with observations (days: daily_pools())."""
    seen = [d for d in days[: hours // 24] if d]
    return {"days": len(seen)} | {
        k: sum(d[k] for d in seen) / len(seen) if seen else None for k in DAILY
    }


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
    """Readiness and raw values of a target score; peer_scores() scores them against peers.

    The raw components hold the benchmark success rate (reliability) and p50 latency (speed).
    """
    reasons = []
    if not benchmark:
        reasons.append("Run is not assigned to the benchmark schedule")
    if m["completed"] < config["minimum_requests"]:
        reasons.append("Insufficient requests")
    if m["coverage"] < config["minimum_coverage"]:
        reasons.append("Insufficient time coverage")
    state = "ready" if not reasons else "incomplete"
    return {
        "state": state,
        "components": {
            "reliability": component(m["success_rate"], state),
            "speed": component(m["latency_ms"]["p50"], state),
        },
        "weights": config["target_weights"],
        "reasons": reasons,
    }


def product_inputs(p, evidence, config, kind, measured_uptime=None):
    """Raw values and readiness of a product's components besides reliability and speed, which
    peer_scores() derives from the target scores.

    p is the window's pool() counts with "daily" (daily_average()): coverage and diversity use
    the daily averages. A raw component's value is what is compared with peers; diversity's is
    the four daily dimension counts (also kept in its inputs), None until local enrichment is
    complete. "fixed" is a score that does not depend on peers (datacenter products keep 100 on
    dcAsns).
    """
    daily = p["daily"]
    inputs = {}
    # Measured uptime (outage time over the countries tested) wins once enough pulse batches
    # finished; evidence is the fallback.
    batches = (measured_uptime or {}).get("batches", 0)
    value = (measured_uptime or {}).get("value")
    pct = 100 * value if batches and value is not None else None
    if pct is not None and batches >= uptime_settings(config)["min_batches"]:
        inputs["uptime"] = component(pct)
    elif "uptime_pct" in evidence:
        inputs["uptime"] = component(evidence["uptime_pct"])
    else:
        inputs["uptime"] = component(
            pct, "incomplete", UPTIME_INCOMPLETE if batches else UPTIME_UNAVAILABLE
        )
    pool_ready = p["unique_ips"] > 0 and p["enrichment_coverage"] == 1
    inputs["coverage"] = component(daily["unique_ips"] if p["unique_ips"] else None)
    counts = {k: daily[k] for k in DIMENSIONS}
    inputs["diversity"] = component(
        counts if pool_ready else None,
        reason=None if pool_ready else "Complete local enrichment required",
        inputs=counts,
    )
    share = p["datacenter_asn_share"]
    state = "ready" if p["classification_coverage"] == 1 else "incomplete"
    reason = "DB-IP usage classification; unknown records remain unclassified"
    inputs["dcAsns"] = component(1 - share if share is not None else None, state, reason)
    if kind == "datacenter" and pool_ready:
        inputs["dcAsns"] |= {"state": state, "fixed": 100.0}
    claim = evidence.get("claim_gap")
    blacklist = evidence.get("blacklist_hit_rate")
    penalty = config["authenticity"]["blocklist_penalty"]
    auth = (
        max(0, min(100, (1 - claim) * 100 - blacklist * penalty))
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
        evidence.get("cost_per_1k_success"),
        reason=None if "cost_per_1k_success" in evidence else UNAVAILABLE["cost_per_1k_success"],
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


def score_entry(value, state, components, weights, reasons, config):
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
                components,
                t["weights"],
                t["reasons"],
                config,
            )
    raw = [dict(m["inputs"]) for m in members]
    for m, r, inputs in zip(members, results, raw, strict=True):
        required = m["config"]["required_targets"]
        weights = m["config"]["benchmark_weights"]
        ready = {
            t: r["targets"][t]["score"] is not None and m["targets"][t]["fresh"] for t in required
        }
        for key in ("reliability", "speed"):
            parts = [r["targets"][t]["components"][key] for t in required]
            done = all(c["state"] == "ready" for c in parts) and all(ready.values())
            inputs[key] = component(
                sum(c["value"] * weights[t] for t, c in zip(required, parts, strict=True))
                / sum(weights.values())
                if all(c["value"] is not None for c in parts)
                else None,
                "ready" if done else "incomplete",
                None if done else "Required benchmark targets are incomplete",
                {
                    "targets": {
                        t: {k: c["inputs"][k] for k in ("value", "best")}
                        for t, c in zip(required, parts, strict=True)
                    }
                },
            )
        r["missing"] = [t for t in required if not ready[t]]
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
    # Diversity: each pool dimension against its own leader, mixed with [peer.diversity].
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
    for m, r, inputs in zip(members, results, raw, strict=True):
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
                components,
                weights,
                [f"Required benchmark incomplete: {t}" for t in r["missing"]]
                + [
                    c["reason"] or f"{k} not ready"
                    for k, c in components.items()
                    if weights[k] > 0 and c["state"] != "ready"
                ],
                config,
            )
        del r["missing"]
    return results


def iso(value):
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(type(value).__name__)
