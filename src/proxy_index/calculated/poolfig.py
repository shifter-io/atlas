"""A window's pool counts and shares from its inputs: the one formula of `aggregate.pool()`.

`pool_figures(sqlwindow.window_inputs(...)[key], since, hours, hosting)` equals
`aggregate.pool(observations, since, hours, hosting)[0]` for the same window's observations:
`pool()` turns its per-IP entries into the same inputs (`entry_inputs()`) and calls it, so the
full and the incremental calculation share the formula.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from .policy import HOSTING_USAGE_TYPES
from .sqlwindow import LABELS, Combo, WindowInputs

BIN = timedelta(minutes=5)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def pool_figures(
    inputs: WindowInputs, since: datetime, hours: int, hosting=HOSTING_USAGE_TYPES
) -> dict:
    """Pool counts and shares per unique exit IP; since must be on the 5-minute grid, which the
    first-seen bins resolve exactly."""
    if (since - EPOCH) % BIN:
        raise ValueError(f"new IPs are counted from a cutoff on the 5-minute grid, not {since}")
    countries, regions, cities, asns = set(), set(), set(), set()
    known = host = located = matched = 0
    for c in inputs.combos:
        cc, _, region_code, region_name, city = c.place
        region = region_code or region_name
        if cc:
            countries.add(cc)
            if region:
                regions.add((cc, region))
            if city:
                cities.add((cc, region, city))
        if c.network[0] is not None:
            asns.add(c.network[0])
        known += c.ips if c.usage_type else 0
        host += c.ips if c.usage_type in hosting else 0
        located += c.located
        matched += c.geo_matched
    size = inputs.unique_ips
    new = sum(n for start, n in inputs.first_seen.items() if start >= since)
    return {
        "unique_ips": size,
        "new_ips": new,
        "churn_rate": new / size if size else None,
        "recurring_share": inputs.recurring_ips / size if size and hours > 24 else None,
        "countries": len(countries),
        "regions": len(regions),
        "cities": len(cities),
        "asns": len(asns),
        "datacenter_asn_share": host / known if known else None,
        "classification_coverage": known / size if size else None,
        "geo_accuracy": matched / located if located else None,
        "enrichment_coverage": inputs.enrichment_matched / size if size else None,
        "observations": inputs.observations,
        "shares_basis": "unique exit IPs with the DB-IP field (unknowns excluded)",
    }


def entry_inputs(ips) -> WindowInputs:
    """The inputs of `aggregate.pool()`'s per-IP entries: one combination per IP (its first row's
    labels), first_seen keyed by the exact product_first_seen, which equals the 5-minute bins for
    a cutoff on the grid."""
    combos, recurring, matched, first_seen = [], 0, 0, Counter()
    for e in ips.values():
        o, asked = e["row"], e["asked"]
        cc = o.get("country_code")
        located = bool(cc and asked)
        combos.append(
            Combo(
                place=tuple(o.get(f) for f in LABELS[:5]),
                network=(o.get("asn"), o.get("asn_organization")),
                usage_type=o.get("usage_type"),
                ips=1,
                observations=e["count"],
                located=int(located),
                geo_matched=int(located and asked == {cc.lower()}),
            )
        )
        recurring += len(e["days"]) >= 2
        matched += bool(o.get("matched"))
        first_seen[o["product_first_seen"]] += 1
    return WindowInputs(
        combos=tuple(combos),
        unique_ips=len(ips),
        recurring_ips=recurring,
        observations=sum(e["count"] for e in ips.values()),
        enrichment_matched=matched,
        first_seen=dict(first_seen),
    )
