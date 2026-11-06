"""Time slots and latency under load: the slots of a window's rows (time_slots), a slot's
success vote (slot_rate), the light-load votes of its slots and targets (light_votes), and the
latency under load those votes make with the load factors (under_load, with_under_load), the
factors themselves (load_factors) and the window's latency block (latency_detail)."""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter, defaultdict

from .latency import KEYS, LATENCY, averaged, blended, load_factor, under_load_values
from .scores import UNDER_LOAD_NONE

UNDER_LOAD_LATE = "No load test before this window's light-load slots"
UNDER_LOAD_LIGHT = "No light-load slot with enough successes in this window"
UNDER_LOAD_CENSUS = (
    "Every load test in this window ran during a census (or its reference pulse did)"
)


def time_slots(rows, pulses=()):
    """Bucket rows (each with its time "slot") grouped per slot.

    A slot holds every request observed in it: "completed"/"successes" count the requests that
    vote on its success, "all_completed"/"all_successes" every request, including pulses and
    load waves that ran during a census of the product (rows with "during_census"). Per target
    it merges the histogram of its light-load successes ("light": {target: Counter},
    "light_successes": {target: n}; rows without the "light" flag stay out). pulses are the
    product's done pulse runs ({"slot", "requests", "successes", "counted", "during_census"}):
    the live counters of a run whose artifact was never ingested (counted False) are added to
    its slot as observed; nothing is invented for requests nobody observed.
    """
    slots = {}

    def slot(key):
        return slots.setdefault(
            key,
            {
                "completed": 0,
                "successes": 0,
                "all_completed": 0,
                "all_successes": 0,
                "light": defaultdict(Counter),
                "light_successes": Counter(),
            },
        )

    def add(s, completed, successes, clean):
        s["all_completed"] += completed
        s["all_successes"] += successes
        if clean:
            s["completed"] += completed
            s["successes"] += successes

    for r in rows:
        c = r["counters"]
        s = slot(r["slot"])
        add(s, c["completed"], c["successes"], not r.get("during_census"))
        if r.get("light", True):
            target = r.get("target")
            for ms, n in c["latency_histogram"]["bins"]:
                s["light"][target][ms] += n
            s["light_successes"][target] += c["successes"]
    for d in pulses:
        if not d["counted"]:
            add(slot(d["slot"]), d["requests"], d["successes"], not d.get("during_census"))
    return slots


def slot_rate(s):
    """A slot's success vote: successes ÷ its voting requests (every request except pulses and
    load waves that ran during a census). A slot left with only such requests votes 0 when all
    of them failed too (the provider was down) and does not vote otherwise. None: no vote."""
    if s["completed"]:
        return s["successes"] / s["completed"]
    if s["all_completed"] and not s["all_successes"]:
        return 0.0
    return None


def light_votes(slots, minimum):
    """(slot, target, histogram) of every slot and target with `minimum` light-load successes:
    each votes with equal weight, so a target or a slot never outweighs another by volume."""
    return [
        (key, target, s["light"][target])
        for key, s in sorted(slots.items())
        for target in sorted(s["light"], key=str)
        if s["light_successes"][target] >= minimum
    ]


def under_load(rows, factors, minimum, excluded=None):
    """Latency under load: per time slot and requested country, the country's light-load
    percentile (and mean) × the slot's factor of the country's runner region (load_factors()),
    or the region's newest earlier factor in the window when the slot has none; the window value
    is the mean of those values (equal weight per slot and country). Counts are of those values.
    excluded: load slots load_factors() left without a factor because of a census (the reason).
    """
    excluded = excluded or {}
    groups = defaultdict(list)
    for r in rows:
        groups[(r.get("region") or "", r.get("country") or "")].append(r)
    values = {k: [] for k in KEYS}
    own = carried = missing = 0
    voting = False
    for (region, _), own_rows in sorted(groups.items()):
        region_factors = factors.get(region, {})
        known = sorted(region_factors)
        for key, _, histogram in light_votes(time_slots(own_rows), minimum):
            voting = True
            if key in region_factors:
                factor, own = region_factors[key], own + 1
            else:
                i = bisect_left(known, key)
                if not i:
                    missing += 1
                    continue
                factor, carried = region_factors[known[i - 1]], carried + 1
            for k, value in under_load_values(histogram, factor).items():
                values[k].append(value)
    n = own + carried
    reason = None
    if not n:
        regions = {region for region, _ in groups}
        reason = (
            UNDER_LOAD_LIGHT
            if not voting
            else UNDER_LOAD_LATE
            if any(factors.get(region) for region in regions)
            else UNDER_LOAD_CENSUS
            if any(excluded.get(region) for region in regions)
            else UNDER_LOAD_NONE
        )
    return {
        "ms": averaged(values),
        "slots": n,
        "own_factor": own,
        "carried_factor": carried,
        "without_factor": missing,
        "reason": reason,
    }


def with_under_load(m, rows, factors, minimum, excluded=None):
    """Add latency_under_load_ms and its slot counts and reason to timed() metrics."""
    result = under_load(rows, factors, minimum, excluded)
    m["latency_under_load_ms"] = result.pop("ms")
    m["under_load"] = result
    return m


def load_factors(rows, minimum):
    """Per runner region and time slot, the factor of each percentile: the latency of the
    product's load runs (census concurrency) in the region's reference country ÷ the latency of
    its pulses in that country, on the same target, in the same slot. Both need `minimum`
    successes. Census runs never make a factor, and neither do load runs or reference pulses that
    ran during a census of the product: they are left out, and a slot left without a usable
    clean pair carries the region's newest earlier factor forward (under_load()). Rows carry
    "region" (the batch's runner region slug).

    Returns (factors {region: {slot: {percentile: factor}}}, references {region: {"country",
    "target", "concurrency"} of its newest load run}, excluded {region: load slots left without
    a factor because of a census}).
    """
    loads, pulses = {}, {}
    references = {}
    tainted = set()

    def key_of(r):
        return (r.get("region") or "", r["slot"], r["country"] or "", r["target"])

    for r in sorted(rows, key=lambda r: (r["slot"], r["target"])):
        if r.get("test") not in ("load", "pulse"):
            continue
        key = key_of(r)
        if r.get("during_census"):
            tainted.add(key)
            if r["test"] == "load":
                loads.setdefault(key, [Counter(), 0])
            continue
        if r["test"] == "load":
            into = loads.setdefault(key, [Counter(), 0])
            references[key[0]] = {
                "country": r["country"],
                "target": r["target"],
                "concurrency": r.get("concurrency"),
            }
        else:
            into = pulses.setdefault(key, [Counter(), 0])
        for ms, n in r["counters"]["latency_histogram"]["bins"]:
            into[0][ms] += n
        into[1] += r["counters"]["successes"]
    factors = defaultdict(dict)
    excluded = defaultdict(set)
    for key in sorted(loads):
        region, slot = key[0], key[1]
        load, base = loads[key], pulses.get(key)
        if slot in factors[region]:
            continue
        if base is None or min(load[1], base[1]) < minimum:
            if key in tainted:
                excluded[region].add(slot)
            continue
        found = load_factor(load[0], base[0])
        if found:
            factors[region][slot] = found
    factors = {region: slots for region, slots in factors.items() if slots}
    return (
        factors,
        references,
        {
            region: len(slots - set(factors.get(region, {})))
            for region, slots in excluded.items()
            if slots - set(factors.get(region, {}))
        },
    )


def latency_detail(rows, factors, references, minimum, excluded=None, choices=()):
    """The window's latency block. regions: per runner region its load wave's reference country
    (the newest load run's), the daily choices that named it (choices: load_references rows
    {"day", "region", "country", "basis", "p50_ms", "successes", "candidates"}) and its factors
    (excluded_during_census: load slots without one because the load run or its reference pulse
    ran during a census). countries: per requested country (and its region) the light-load and
    under-load latency of its pulses."""
    excluded = excluded or {}
    pulses = defaultdict(list)
    for r in rows:
        if r.get("test") == "pulse":
            pulses[(r.get("region") or "", r["country"] or "")].append(r)
    countries = []
    for region, country in sorted(pulses):
        own = pulses[(region, country)]
        result = under_load(own, factors, minimum, excluded)
        countries.append(
            {
                "country": country or None,
                "region": region or None,
                "latency_ms": blended(h for *_, h in light_votes(time_slots(own), minimum)),
                "latency_under_load_ms": result.pop("ms"),
                **result,
            }
        )
    chosen = defaultdict(list)
    for c in sorted(choices, key=lambda c: (c["region"], c["day"])):
        chosen[c["region"]].append({k: c[k] for k in c if k != "region"})
    regions = []
    for region in sorted(
        {r for r, _ in pulses} | set(factors) | set(references) | set(excluded) | set(chosen)
    ):
        own = factors.get(region, {})
        latest = max(own) if own else None
        regions.append(
            {
                "region": region or None,
                "reference": references.get(region),
                "chosen": chosen.get(region, []),
                "factors": {
                    "slots": len(own),
                    "excluded_during_census": excluded.get(region, 0),
                    "mean": {
                        p: round(sum(f[p] for f in own.values()) / len(own), 6) for p in LATENCY
                    }
                    if own
                    else None,
                    "latest": {"at": latest} | {p: round(own[latest][p], 6) for p in LATENCY}
                    if latest
                    else None,
                },
            }
        )
    return {
        "regions": regions,
        "basis": "under load = light-load (pulse) latency x (load run ÷ pulse in the region's "
        "reference country) of the same slot, else the region's newest earlier factor; mean "
        "of slot and country values",
        "countries": countries,
    }
