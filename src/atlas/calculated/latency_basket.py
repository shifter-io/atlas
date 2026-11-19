"""LATENCY-BASKET-1 (the owner's decision of 2026-11-19, #1385): a network's headline latency over
a fixed basket of demand countries, each weighing the same ([latency] in the methodology file).

Per basket country, the network's latency object of its latency target's pulses there (light
load, as the pooled headline: each time slot with min_latency_successes successes in that
country weighs the same; countries()). A basket country the network has no such latency in (it
does not serve it, or too few successes in the window) counts as the slowest network's of the
same kind and window there, each percentile and the mean its own slowest (slowest()). A basket
country no network of the kind has a latency in is left out of every network's figure.

The network figure, each of p50, p90, p95, p99 and the mean, is the aggregate (the plain mean,
or the median) of those basket values (basket_latency()); it replaces the latency target's
latency_ms in the network window (the headline, and the Speed component that reads its p50),
and each series point's latency_ms, the same rule over the point's rows and the networks'
points at the same time. A network or point with no basket country of its own has no latency
(NO_BASKET): penalties alone would make a value nobody measured. latency_basket publishes the
coverage ("covered" of "size" basket countries), the countries' values used, the unserved ones
and the pooled latency it replaced ("pooled_ms"), so one build shows the old and the new figure.

Product group windows keep the pooled latency: only the network windows are served. The latency
under load, the other targets' latency, the per-place latency and the latency change
(latency_days.py) keep their rules.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median

from .latency import KEYS, LATENCY, blended
from .policy import latency_basket as settings
from .underload import light_votes, time_slots

AGGREGATES = {"mean": lambda values: sum(values) / len(values), "median": median}
NO_BASKET = "No basket country with enough pulse successes in this window"


def countries(rows, target, minimum, basket):
    """{country: latency object} of each basket country with a light-load latency of the
    target's rows there: light_votes() over the country's rows alone (each slot with `minimum`
    successes weighs the same)."""
    own = defaultdict(list)
    for r in rows:
        if r["target"] == target and r["country"] in basket:
            own[r["country"]].append(r)
    found = {}
    for country, mine in sorted(own.items()):
        votes = light_votes(time_slots(mine), minimum)
        if votes:
            found[country] = blended(h for *_, h in votes)
    return found


def measure(rows, target, minimum, basket, points):
    """A network window's "_basket": its latency target, its basket countries' latency over the
    window's rows ("window") and over each series point's ({at: countries}; points: (start, step)
    of aggregate.series_axis() and the point times)."""
    (start, step), times = points
    rows = [r for r in rows if r["target"] == target and r["country"] in basket]
    by_point = defaultdict(list)
    for r in rows:
        by_point[start + ((r["bucket_start"] - start) // step) * step].append(r)
    return {
        "target": target,
        "window": countries(rows, target, minimum, basket),
        "points": {at: countries(by_point[at], target, minimum, basket) for at in times},
    }


def slowest(owns, basket):
    """Per basket country some network has a latency in, each key's highest (slowest) value
    among owns (the networks' countries())."""
    found = {}
    for c in basket:
        values = [own[c] for own in owns if c in own]
        if values:
            found[c] = {k: max(v[k] for v in values) for k in KEYS}
    return found


def basket_latency(own, penalty, aggregate, keys=KEYS):
    """(latency object of keys, the countries' latency used): per key the aggregate of own's
    value in each country of penalty (slowest()), else the penalty's, the percentiles in whole
    milliseconds as the histograms give them, the mean to 0.1 ms; all None without a country of
    own (NO_BASKET)."""
    used = {c: own.get(c, p) for c, p in penalty.items()}
    if not own:
        return dict.fromkeys(keys), used
    values = {k: AGGREGATES[aggregate]([v[k] for v in used.values()]) for k in keys}
    return {k: round(v, 1) if k == "mean" else round(v) for k, v in values.items()}, used


def apply(networks, config):
    """Replace each network window's latency target latency_ms and its series points' by the
    basket latency, against the networks of the same kind and window; "_basket" is popped."""
    found = settings(config)
    peers = defaultdict(list)
    for entry in networks:
        for label, w in entry["windows"].items():
            if (own := w.pop("_basket", None)) is not None:
                peers[entry["kind"], label].append((w, own))
    if found is None:
        return
    for members in peers.values():
        _headline(members, found)
        _points(members, found)


def _headline(members, found):
    """The window's latency target's latency_ms and latency_basket."""
    penalty = slowest([own["window"] for _, own in members], found["basket"])
    for w, own in members:
        if own["target"] not in w["targets"]:
            continue
        m = w["targets"][own["target"]]["metrics"]
        latency, used = basket_latency(own["window"], penalty, found["aggregate"])
        m["latency_basket"] = {
            "size": len(found["basket"]),
            "covered": len(own["window"]),
            "counted": len(penalty),
            "unserved": sorted(set(penalty) - set(own["window"])),
            "unmeasured": [c for c in found["basket"] if c not in penalty],
            "aggregate": found["aggregate"],
            "penalty": found["penalty"],
            "countries": {c: v["p50"] for c, v in used.items()},
            "pooled_ms": m["latency_ms"],
        }
        if latency["p50"] is None and m["latency_ms"]["p50"] is not None:
            m["latency_reason"] = NO_BASKET
        m["latency_ms"] = latency


def _points(members, found):
    """Each series point's latency_ms (its percentiles): the basket rule over the networks'
    points at its time."""
    series = [{p["at"]: p for p in w["series"]} for w, _ in members]
    for at in {at for _, own in members for at in own["points"]}:
        owns = [own["points"].get(at, {}) for _, own in members]
        penalty = slowest(owns, found["basket"])
        for points, own in zip(series, owns, strict=True):
            if at in points:
                latency = basket_latency(own, penalty, found["aggregate"], LATENCY)[0]
                points[at]["latency_ms"] = latency
