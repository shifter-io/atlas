"""The row count of every table the synthetic data set fills, from its model (SCALE-GEN-1a).

`plan` takes a synthetic.Model and its days and counts each table's rows without generating them.
"""

from __future__ import annotations


def plan(model, days) -> dict[str, int]:
    """Rows each generated table holds after `days` (an iterable of day numbers), uncounted."""
    days = sorted(days)
    lanes, settings = model.lanes, model.settings
    pulses = len(lanes) * 24 * len(days)
    counts = dict.fromkeys(("ip_observations", "run_buckets", "ip_enrichment"), 0)
    counts |= dict.fromkeys(("run_geo_buckets", "estimated_hours", "pool_ips"), 0)
    for day in days:
        for lane in lanes:
            test = model.test(lane, day)
            counts["ip_observations"] += test.rows
            counts["run_buckets"] += test.buckets + 24
            counts["ip_enrichment"] += test.size
            counts["estimated_hours"] += len(model.estimates(test))
            counts["run_geo_buckets"] += sum(len(model.pulse(lane, day, h)[1]) for h in range(24))
    owned = [
        sum(after - first for first, after in (model.new(lane, d) for d in days)) for lane in lanes
    ]
    for lane in lanes:
        counts["pool_ips"] += owned[lane.position] + _borrowed_union(model, lane, days)
    count = len(settings.countries)
    return counts | {
        "ip_addresses": sum(owned),
        "product_ips": sum(owned),
        "test_runs": len(lanes) * len(days) + pulses,
        "test_batches": count * 24 * len(days),
        "batch_runs": pulses,
        "run_enrichments": len(lanes) * len(days),
        "dbip_releases": len(days),
        "providers": len(settings.networks),
        "products": len(settings.networks),
        "pool_stats": len(lanes),
        "scope_stats": 2 * len(lanes),
        "place_names": 1 + sum(p.cities for p in model.places),
        "enrichment_locations": 1 + sum(p.cities for p in model.places),
        "asn_names": 1 + sum(p.asns for p in model.places),
        "enrichment_networks": 1 + sum(p.asns for p in model.places),
    }


def _borrowed_union(model, lane, days) -> int:
    """Distinct IPs the lane borrowed over the days (each day: a prefix of a sliding window)."""
    spans = sorted(
        (test.source, test.source + test.borrowed)
        for test in (model.test(lane, day) for day in days)
        if test.borrowed
    )
    total, end = 0, None
    for first, after in spans:
        if end is None or first > end:
            total, end = total + after - first, after
        elif after > end:
            total, end = total + after - end, after
    return total
