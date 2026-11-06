"""Pure window aggregation. No estimates or inferred measurements, and light-load latency is
never an average of percentiles: slot histograms are normalised and merged."""

from __future__ import annotations

import json
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from ..names import NETWORK, PLACE
from ..stats import Histogram
from .latency import KEYS, LATENCY, blended
from .latency import blend as blend
from .policy import DIMENSIONS, HOSTING_USAGE_TYPES
from .poolfig import entry_inputs, pool_figures
from .scores import NOT_LATENCY_TARGET as NOT_LATENCY_TARGET
from .scores import UNDER_LOAD_NONE
from .scores import component as component
from .scores import leader as leader
from .scores import peer_scores as peer_scores
from .scores import product_inputs as product_inputs
from .scores import score_entry as score_entry
from .scores import target_score as target_score
from .scores import versus as versus

# Challenge and block rates come from the typed outcome counters of v6 buckets and uptime from
# pulse batches, so they are no longer listed here; a None metric means nothing measured them.
UNAVAILABLE = {
    "ttfb_p50": "TTFB is not recorded by this target version",
    "stickiness_hold_rate": "Sticky-session survival measurements are required",
    "leak_checks": "Leak measurements are required",
    "billed_bytes": "Response body size is not billed wire traffic",
    "cost_per_1k_success": "Billed usage is required",
    "requests_per_ip": "Consecutive rotation behavior is not recorded",
    "ip_latency": "Per-IP timing is not recorded",
}
# Effective-dated list prices (metric_evidence), USD per GB of the pool that is tested: the
# lowest rate (the largest plan; scored by value) and the entry rate (the smallest plan).
PRICES = {"price_per_gb_lowest": "lowest_per_gb", "price_per_gb_entry": "entry_per_gb"}
UNDER_LOAD_LATE = "No load test before this window's light-load slots"
UNDER_LOAD_LIGHT = "No light-load slot with enough successes in this window"
UNDER_LOAD_CENSUS = (
    "Every load test in this window ran during a census (or its reference pulse did)"
)
LIGHT_CENSUS = "Every pulse with enough successes in this window ran during a census"
LIGHT_NONE = "No pulse with enough successes in this window"
# Payload breakdown name -> calculated_list_rows dimension code.
BREAKDOWNS = {
    "country": "c",
    "region": "r",
    "city": "g",
    "asn": "a",
    "country_asn": "x",
    "city_asn": "y",
}
OUTCOMES = ("challenges", "blocks", "bytes_up", "bytes_down")
EVIDENCE_RANGES = {
    "uptime_pct": (0, 100),
    "stickiness_hold_rate": (0, 1),
    "captcha_rate": (0, 1),
    "block_rate": (0, 1),
    "ttfb_p50": (0, 1e9),
    "billed_bytes": (0, 1e18),
    "price_per_gb_lowest": (0, 1e9),
    "price_per_gb_entry": (0, 1e9),
    "cost_per_1k_success": (0, 1e12),
    "requests_per_ip": (0, 1e12),
}
# Pool counts averaged over daily (24-hour) pools: coverage and diversity score from these.
DAILY = ("unique_ips", *DIMENSIONS)
DAY = timedelta(hours=24)


def metrics(rows, hours):
    """Request-weighted counters and rates of bucket rows; timed() adds the time weighting.

    Every request counts, whatever the run's role or test. Latency merges the histograms of
    light-load rows only (a row's "light" flag: pulses and runs outside a batch; census and
    load runs run at census concurrency).
    """
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
        if row.get("light", True):
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
        "latency_ms": histogram.percentiles() | {"mean": histogram.mean()},
        "coverage": len({r["bucket_start"] for r in rows}) / (hours * 12),
        "coverage_basis": "5-minute buckets",
        "last_observed": max((r["last_observed"] for r in rows), default=None),
        "latency_basis": "successful light-load requests",
        "units": {
            "latency_ms": "milliseconds",
            "success_rate": "fraction",
            "body_bytes": "response body bytes",
            "uptime": "fraction of time (outages over countries tested)",
            "bytes": "wire bytes",
        },
    }


def pool(observations, since, hours, hosting=HOSTING_USAGE_TYPES):
    """Window pool counts and shares, per unique exit IP (each labelled by its DB-IP record).

    Shares leave IPs without the DB-IP field out of numerator and denominator: geo_accuracy is
    the share of IPs with a DB-IP country whose country is the one every request that returned
    them asked for (IPs only seen without a requested country are left out too);
    datacenter_asn_share the share of IPs with a DB-IP usage type that is a hosting usage type.
    """
    ips = {}
    for o in observations:
        entry = ips.setdefault(
            o["ip_id"],
            {
                "first": o["first_seen"],
                "last": o["last_seen"],
                "days": set(),
                "count": 0,
                "row": o,
                "asked": set(),
            },
        )
        entry["days"].add(o["bucket_start"].date())
        entry["count"] += o["count"]
        entry["first"] = min(entry["first"], o["first_seen"])
        entry["last"] = max(entry["last"], o["last_seen"])
        if o.get("requested_country"):
            entry["asked"].add(o["requested_country"].lower())
    return pool_figures(entry_inputs(ips), since, hours, hosting), ips


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
    if located:
        # Coverage evidence needs the actual location/network intersection,
        # never a join of independent country or city totals. Unknown network
        # labels remain explicit nulls so no observed IPs disappear.
        label = dict(zip(PLACE, place, strict=True))
        identity = network if network is not None else (None, None, None)
        result.append(
            ("city_asn", (place, identity), label | dict(zip(NETWORK, identity, strict=True)))
        )
    return result


def breakdowns(ips, geo):
    """Success, outcome, latency and IP counts per place, ASN and their intersections.

    ips are pool() entries (each IP once, labelled by its DB-IP enrichment), or weighted entries
    standing for "ips" IPs of one label combination (sqlwindow combinations); geo rows carry
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
            row["ips"] += e.get("ips", 1)  # a weighted entry stands for its IPs
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
    return {name: _breakdown_rows(members) for name, members in rows.items()}


def _breakdown_rows(members):
    """One breakdown's rows with their rates and percentiles, by completed requests, then IPs."""
    out = []
    for row in members.values():
        latency = row.pop("histogram").percentiles()
        label = row.pop("label")
        out.append(
            label
            | row
            | {
                "success_rate": row["successes"] / row["completed"] if row["completed"] else None,
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
    return out


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


def floor_time(moment, seconds):
    """The start of the epoch-aligned slot of `seconds` that holds moment."""
    return datetime.fromtimestamp(moment.timestamp() // seconds * seconds, UTC)


def census_overlaps(census, runs):
    """Pulse and load runs that ran while a census of the same product was running.

    census: the product's census runs ({"product_id", "batch_id", "started_at",
    "finished_at"}; a census still running at the cutoff ends at the cutoff), in any country:
    the account and the node are shared. runs: pulse and load runs ({"product_id", "batch_id",
    "run_key", "started_at", "finished_at"}). Two runs overlap when each starts before the other
    ends. Returns, per overlapping run, {"batch_id", "product_id", "run_key", "census": [census
    batch ids]}. Everything comes from stored start and finish times, so a calculation
    reproduces it.
    """
    by_product = defaultdict(list)
    for c in census:
        by_product[c["product_id"]].append(c)
    index = {}
    for product, rows in by_product.items():
        rows.sort(key=lambda c: (c["started_at"], c["batch_id"]))
        longest = max(c["finished_at"] - c["started_at"] for c in rows)
        index[product] = ([c["started_at"] for c in rows], rows, longest)
    result = []
    for run in runs:
        if run["product_id"] not in index:
            continue
        starts, rows, longest = index[run["product_id"]]
        hits = []
        i = bisect_left(starts, run["finished_at"]) - 1
        while i >= 0 and starts[i] >= run["started_at"] - longest:
            if rows[i]["finished_at"] > run["started_at"]:
                hits.append(rows[i]["batch_id"])
            i -= 1
        if hits:
            result.append(
                {k: run[k] for k in ("batch_id", "product_id", "run_key")}
                | {"census": sorted(set(hits))}
            )
    return result


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


def timed(rows, hours, minimum, pulses=(), slot_minutes=None):
    """metrics(rows) with the time-weighted success rate and light-load latency.

    The window is split into time slots. Every slot with observed requests weighs the same:
    success_rate is the mean of the slot success rates (successes ÷ requests observed in the
    slot; pulses as in time_slots()), request_success_rate stays successes ÷ completed. A slot
    whose requests all failed (the provider was down) votes 0. Light-load latency merges the
    light-load histograms of every slot and target after normalising each to weight 1; one
    with fewer than `minimum` successes does not vote. Over several targets that is an
    equal-weight mixture of them; targets[...] carry each target's own latency.
    """
    m = metrics(rows, hours)
    slots = time_slots(rows, pulses)
    votes = {key: slot_rate(s) for key, s in slots.items()}
    rates = [v for v in votes.values() if v is not None]
    voting = light_votes(slots, minimum)
    m["request_success_rate"] = m["success_rate"]
    m["success_rate"] = sum(rates) / len(rates) if rates else None
    m["latency_ms"] = blended(h for *_, h in voting)
    m["latency_reason"] = None
    if not voting:
        flagged = any(r.get("during_census") for r in rows) or any(
            d.get("during_census") for d in pulses
        )
        m["latency_reason"] = LIGHT_CENSUS if flagged else LIGHT_NONE
    m["time_slots"] = {
        "minutes": slot_minutes,
        "success": len(rates),
        "down": sum(v == 0 for v in rates),
        "skipped_during_census": sum(
            v is None and s["all_completed"] > 0
            for v, s in zip(votes.values(), slots.values(), strict=True)
        ),
        "latency": len({key for key, *_ in voting}),
        "latency_targets": sorted({t for _, t, _ in voting}, key=str),
        "min_latency_successes": minimum,
    }
    m["success_basis"] = (
        "mean of time-slot success rates, every observed request except pulses and load waves "
        "that ran during a census"
    )
    m["latency_basis"] = (
        "light-load successes (pulses that did not run during a census), each slot and target "
        "normalised to equal weight"
    )
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
        high = Histogram(load[0]).percentiles()
        low = Histogram(base[0]).percentiles()
        if all(high[p] and low[p] for p in LATENCY):
            factors[region][slot] = {p: high[p] / low[p] for p in LATENCY}
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


def under_load(rows, factors, minimum, excluded=None):
    """Latency under load: per time slot and requested country, the country's light-load
    percentile × the slot's factor of the country's runner region (load_factors()), or the
    region's newest earlier factor in the window when the slot has none; the window value is the
    mean of those values (equal weight per slot and country). Counts are of those values.
    excluded: load slots load_factors() left without a factor because of a census (the reason).
    """
    excluded = excluded or {}
    groups = defaultdict(list)
    for r in rows:
        groups[(r.get("region") or "", r.get("country") or "")].append(r)
    values = {p: [] for p in LATENCY}
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
            percentiles = Histogram(histogram).percentiles()
            for p in LATENCY:
                values[p].append(percentiles[p] * factor[p])
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
        "ms": {p: round(sum(v) / n, 3) if n else None for p, v in values.items()},
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


def census_split(rows, pulses, minimum, slot_minutes=None, timed_target=None):
    """Pulse runs that ran during a census of the product against clean ones, and load waves.

    rows are the window's bucket rows, pulses its done pulse runs (as for timed()). For each
    group: runs, requests, successes, request_success_rate, the time-weighted success_rate and
    the light-load style latency of its own requests (latency_ms; the latency target's only:
    timed_target, None for every target), overall and per target, so the numbers measured
    during a census stay visible next to the clean ones they are left out of. census_batches
    lists the censuses they overlapped.
    """

    def group(during, target=None):
        own = [
            r | {"light": timed_target in (None, r["target"]), "during_census": False}
            for r in rows
            if r.get("test") == "pulse"
            and bool(r.get("during_census")) == during
            and target in (None, r["target"])
        ]
        runs = [
            d | {"during_census": False}
            for d in pulses
            if bool(d.get("during_census")) == during and target in (None, d["target"])
        ]
        m = timed(own, 24, minimum, runs, slot_minutes)
        requests = m["completed"] + sum(d["requests"] for d in runs if not d["counted"])
        successes = m["successes"] + sum(d["successes"] for d in runs if not d["counted"])
        return {
            "runs": len(runs),
            "requests": requests,
            "successes": successes,
            "request_success_rate": successes / requests if requests else None,
            "success_rate": m["success_rate"],
            "latency_ms": m["latency_ms"]
            if target in (None, timed_target) or timed_target is None
            else dict.fromkeys(KEYS),
        }

    loads = {}
    for r in rows:
        if r.get("test") == "load":
            loads[r["run_key"]] = bool(r.get("during_census"))
    targets = sorted(
        {d["target"] for d in pulses} | {r["target"] for r in rows if r.get("test") == "pulse"}
    )
    return {
        "basis": "pulse and load runs that overlapped a running census of the same product (any "
        "country), from stored run start and finish times; left out of light-load latency, "
        "time-slot success and the load factor, still counted as requests, uptime and slots",
        "pulses": {"clean": group(False), "during_census": group(True)},
        "targets": {
            t: {"clean": group(False, t), "during_census": group(True, t)} for t in targets
        },
        "load": {
            "clean": sum(not v for v in loads.values()),
            "during_census": sum(loads.values()),
        },
        "census_batches": sorted(
            {c for r in rows for c in r.get("census_batches", ())}
            | {c for d in pulses for c in d.get("census_batches", ())}
        ),
    }


def slot_coverage(m, slots, since, end):
    """Set m's coverage to the share of pulse slots in [since, end) the product filled.

    slots are the product's done pulse batch runs: a run is only ever 'failed' by our side (no
    node, never started, went silent, node shut down or erred, upload rejected), so those are
    not slots. A slot is filled by a run with at least one request, also when every request
    failed: a provider that is down is data (filled and down), not a gap.
    """
    due = [b for b in slots if since <= b["scheduled_for"] < end]
    filled = [b for b in due if b["requests"] > 0]
    m["coverage"] = len(filled) / len(due) if due else 0.0
    m["coverage_basis"] = "pulse slots"
    m["coverage_slots"] = {
        "due": len(due),
        "filled": len(filled),
        "down": sum(not b["successes"] for b in filled),
    }
    return m


def uptime(batches, since, end, min_successes):
    """Share of the done pulse batch runs in [since, end) with at least min_successes
    successes."""
    rows = [b for b in batches if since <= b["scheduled_for"] < end]
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


def series(
    rows,
    observations,
    start,
    end,
    hours,
    *,
    outages=(),
    tested=(),
    latency_target=None,
    minimum=1,
    factors=None,
    excluded=None,
):
    """Request history plus outage-time availability at the same bucket resolution.

    A country contributes only in buckets intersecting a started census/pulse run
    or a recorded outage. Missing monitoring evidence is null, never assumed healthy.
    Outage intervals are unioned per country and clipped to each half-open bucket.
    """
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
        latency_rows = [r for r in buckets[t] if r["target"] == latency_target]
        timing = timed(latency_rows, step.total_seconds() / 3600, minimum) if latency_target else m
        stop = min(t + step, end)
        countries = {r["country"] for r in tested if r["started_at"] < stop and r["last"] > t} | {
            o["country"] for o in outages if o["started_at"] < stop and (o["ended_at"] or end) > t
        }
        availability = outage_uptime(outages, {c: stop for c in countries}, t, stop)
        out.append(
            {
                "at": t,
                "completed": m["completed"],
                "successes": m["successes"],
                "success_rate": m["success_rate"],
                "latency_ms": {p: timing["latency_ms"][p] for p in LATENCY},
                **(
                    {
                        "latency_under_load_ms": under_load(
                            latency_rows, factors or {}, minimum, excluded
                        )["ms"]
                    }
                    if latency_target
                    else {}
                ),
                "unique_ips": len(seen[t]) if buckets[t] else None,
                "uptime": availability["value"],
            }
        )
        t += step
    return out


def iso(value):
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(type(value).__name__)
