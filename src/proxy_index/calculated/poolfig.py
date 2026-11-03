"""`aggregate.pool()`'s window counts and shares from database-counted inputs.

`pool_figures(sqlwindow.window_inputs(...)[key], since, hours, hosting)` equals
`aggregate.pool(observations, since, hours, hosting)[0]` for the same window's observations. For
now this duplicates the formula part of `pool()`; once the incremental calculation counts its
missed windows in the database, `pool()` calls this and the copy goes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .policy import HOSTING_USAGE_TYPES
from .sqlwindow import WindowInputs

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
