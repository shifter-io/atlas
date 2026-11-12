"""Network summaries (contract K9): one entry per network in the head of payload v4.

A network is one (provider, kind) pair (netlists.py). Its groups are every product group of that
provider with that kind; its union is their bucket rows, observations, outages and tested
countries together. summaries() builds each network window in build(), by the rules one group
uses (calculator.build()) applied to the union: metrics, targets, series, uptime and the
previous period's success rate from the groups' bucket rows, with every slot rule of the
scheduled groups (their pulse slots, batches, outages and tested countries). Browser rows stay
out of every network-level figure and count per target only (browser.py). Tests and new IPs
are the groups' sums; pool shares the groups' weighted by their unique IPs. The browser target
scores and browser_overall rank the networks of one kind (target_scores.score_networks()).

attach() completes the windows in store.write(), once the network lists are written:

- the window's distinct IPs and each series point's, counted in the database by
  hourly.bucket_ips() over the window's runs. The window is one point as wide as the window,
  so the count keeps to the window's time bounds as a group's pool does (window_distinct()
  counts every observation of a run, also one outside the window). No IP set is held in
  Python, so the incremental calculation reads no observation for them;
- the K10 list id and entry counts (lists, breakdown_entries, pool.list_entries) and the place
  counts of its rows (pool countries, regions, cities and asns, also in pool.daily): distinct
  country codes, regions, cities and ASN numbers (netlists.COUNTED), never the rows, which are
  labels (an ASN once per usage type);
- the groups' estimates (estimates/window.py) summed, scaled by the network's distinct IPs,
  per released hour in the 24h window and per UTC day in the 7d and 30d windows (their charts
  show daily points, and an hourly series would grow the head by about 2.4 kB a day);
- the ownership overlap, counted in the database over the same window-bounded IPs (7d and 30d
  once a UTC day, carried from the previous head of the same implementation in between while it
  has pairs), and the balanced score, rank and readiness, against the networks of the same kind
  and window (network_scores.py).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from . import browser, fleetsql, hourly, netlists, network_scores, parts, target_scores
from .aggregate import (
    SeriesSpec,
    floor_time,
    load_factors,
    outage_list,
    outage_uptime,
    series,
    slot_coverage,
    target_timed,
    timed,
    uptime,
    with_under_load,
)
from .policy import latency_target, slot_settings, uptime_settings

# Pool shares: the groups' values weighted by their unique IPs.
SHARES = (
    "recurring_share",
    "datacenter_asn_share",
    "classification_coverage",
    "geo_accuracy",
    "enrichment_coverage",
)
# Pool counts from the window's network list (netlists.COUNTED): its distinct places.
PLACES = ("countries", "regions", "cities", "asns")
TESTS = ("census", "pulse", "load", "unscheduled")
SHARES_BASIS = "unique exit IPs with the DB-IP field (unknowns excluded)"
OUTAGE_HOURS = 720
DAY = 86400  # seconds: a 7d or 30d window's estimate points are UTC days
INVALID = "Invalid network summary"


def slot_runs(batches, slot_seconds, flagged):
    """A product's done pulse batch runs in their time slots (time_slots() adds the observed
    live counters of one whose artifact was never ingested). flagged maps (batch_id,
    product_id) to the census batches the pulse ran during."""
    return [
        {
            "slot": floor_time(b["scheduled_for"], slot_seconds),
            "scheduled_for": b["scheduled_for"],
            "target": b["target_id"],
            "requests": b["requests"],
            "successes": b["successes"],
            "counted": b.get("run_key") is not None,
            "during_census": bool(flagged.get((b.get("batch_id"), b["product_id"]))),
            "census_batches": flagged.get((b.get("batch_id"), b["product_id"]), []),
        }
        for b in batches
    ]


def summaries(fleet_groups, bucket_groups, by_product, flagged, as_of, config):
    """A build's networks, ordered by provider, then kind; attach() completes their windows.

    fleet_groups are build()'s members (product, rows, observations, member) by (cohort,
    window); bucket_groups every bucket row of each group; by_product build()'s scheduled group,
    pulse batch runs, outages, tested countries and monitored spans of each product; flagged
    the pulse batches that ran during a census (slot_runs()).
    """
    windows, configs = defaultdict(lambda: defaultdict(list)), {}
    for (_, label), members in fleet_groups.items():
        for p, *_, member in members:
            windows[p["provider"], p["kind"]][label].append(p)
            configs[p["id"]] = member["config"]
    networks = []
    for key in sorted(windows):
        labels = windows[key]
        groups = list({p["id"]: p for ms in labels.values() for p in ms}.values())
        union = Network(groups, bucket_groups, by_product, flagged, config)
        entry = union.entry(as_of)
        cfg = configs[union.lead["id"]]  # its required targets and target groups
        entry["windows"] = {label: union.window(ms, label, cfg) for label, ms in labels.items()}
        networks.append(entry)
    target_scores.score_networks(networks, config)
    return networks


class Network:
    """One network's union: its groups' bucket rows (network and browser rows apart), and the
    pulse batch runs, pulse slots, outages and tested countries of its scheduled groups."""

    def __init__(self, groups, bucket_groups, by_product, flagged, config):
        scheduled, pulses, outages, tested, monitored = by_product
        self.groups = groups
        rows = [r for g in groups for r in bucket_groups[g["product_id"], g["cohort"]]]
        self.rows, self.browser = browser.split(rows)
        # A product's census-pulse group (build()'s scheduled_groups) has its slots and outages.
        keys = {g["id"]: (g["product_id"], g["cohort"]) for g in groups}
        self.scheduled = {gid for gid, key in keys.items() if scheduled.get(key[0]) == key}
        products = [g["product_id"] for g in groups if g["id"] in self.scheduled]
        self.batches = [b for product in products for b in pulses[product]]
        self.minutes = groups[0]["scoring"]["slots"]["minutes"]
        self.pulses = slot_runs(self.batches, self.minutes * 60, flagged)
        self.down = [
            o | {"country": o["country"].strip()} for product in products for o in outages[product]
        ]
        self.tested = {}
        for product in products:
            for country, last in tested[product].items():
                self.tested[country] = max(self.tested.get(country, last), last)
        self.monitored = [t for product in products for t in monitored[product]]
        self.minimum = slot_settings(config)["min_latency_successes"]
        self.target = latency_target(config)
        self.threshold = uptime_settings(config)["min_successes"]
        self.lead = lead(groups, self.scheduled)

    def entry(self, as_of):
        """The network entry without its windows: the headline product's names, census,
        pricing and 30d evidence (lead()), and the newest outages of the scheduled groups."""
        g = self.lead
        asked = {(p["criteria"].get("country") or "").upper() for p in self.groups} - {""}
        return {
            "id": netlists.network_id(g),
            "product_id": g["product_id"],
            "provider": g["provider"],
            "product": g["product"],
            "name": g["name"],
            "kind": g["kind"],
            "cohort": netlists.COHORT,
            "criteria": {
                "country": None,
                "requested_countries": sorted(asked),
                "groups": len(self.groups),
            },
            "census": g["census"] if g["id"] in self.scheduled else [],
            "pricing": g["pricing"],
            "outages": outage_list(self.down, as_of - timedelta(hours=OUTAGE_HOURS), as_of),
            "evidence": g["windows"]["30d"]["evidence"] if "30d" in g["windows"] else {},
        }

    def window(self, groups, label, cfg):
        """One window over the union, waiting for attach(): "_runs" lists its runs, "_lead"
        is the headline group's window (its evidence, network_scores.member())."""
        period = groups[0]["windows"][label]["period"]
        since, end, hours = period["start"], period["end"], period["hours"]
        rows = self.inside(self.rows, since, end)
        slots = [d for d in self.pulses if since <= d["scheduled_for"] < end]
        m = timed(rows, hours, self.minimum, slots, self.minutes)
        factors, _, excluded = load_factors(
            [r for r in rows if r["target"] == self.target], self.minimum
        )
        with_under_load(m, rows, factors, self.minimum, excluded)
        up = uptime(self.batches, since, end, self.threshold)
        up |= outage_uptime(self.down, self.tested, since, end)
        m["uptime"] = up["value"]
        if self.scheduled:
            slot_coverage(m, self.batches, since, end)
        before = since - (end - since)
        previous = [d for d in self.pulses if before <= d["scheduled_for"] < since]
        prev_rows = self.inside(self.rows, before, since)
        prev = timed(prev_rows, hours, self.minimum, previous, self.minutes)
        windows = [p["windows"][label] for p in groups]
        latency = {"latency_target": self.target, "minimum": self.minimum}
        latency |= {"factors": factors, "excluded": excluded, "outages": self.down}
        points = series(rows, (), SeriesSpec(since, end, hours, tested=self.monitored, **latency))
        rate, old = m["success_rate"], prev["success_rate"]
        return {
            "period": period,
            "readiness": "partial" if m["completed"] else "unavailable",
            "metrics": m,
            "uptime": up,
            "tests": {name: sum(w["tests"][name] for w in windows) for name in TESTS},
            "pool": pool([w["pool"] for w in windows]),
            "targets": self.targets(cfg, rows, (since, end, hours), (factors, excluded)),
            "series": points,
            "success_delta": rate - old if rate is not None and old is not None else None,
            "scores": {},
            "overlap": None,
            "_runs": sorted({r["run_key"] for r in rows}),
            "_lead": self.lead["windows"].get(label, {}),
        }

    def targets(self, cfg, rows, span, latency):
        """Each target's metrics over the union, as a group's (calculator.build()): browser
        targets from the browser rows, latency as aggregate.target_timed() gives it."""
        since, end, hours = span
        found = {}
        for target, own, extra in browser.targets(cfg, rows, self.browser, since, end):
            batches = [b for b in self.batches if b["target_id"] == target]
            slots = [d for d in self.pulses if d["target"] == target]
            slots = [d for d in slots if since <= d["scheduled_for"] < end]
            timing = (self.minimum, self.minutes, self.target)
            tm = target_timed((target, own, extra), hours, slots, timing, latency)
            tm["uptime"] = uptime(batches, since, end, self.threshold)["value"]
            tm["units"] = tm["units"] | {"uptime": "fraction of pulse batches"}
            if self.scheduled and not extra:  # a browser target's coverage stays its buckets'
                slot_coverage(tm, batches, since, end)
            readiness = "measured" if tm["completed"] else "unavailable"
            found[target] = {"metrics": tm, "readiness": readiness, **extra}
        return found

    @staticmethod
    def inside(rows, since, end):
        return [r for r in rows if since <= r["bucket_start"] < end]


def lead(groups, scheduled):
    """The headline product's group (netlists.headline()): its scheduled group if it has one,
    else its group with the most 30d completed requests (the first of them on a tie)."""
    product = netlists.headline(groups)
    own = [g for g in groups if g["product_id"] == product]
    return min(own, key=lambda g: (g["id"] not in scheduled, -completed(g)))


def completed(group):
    window = group["windows"].get("30d")
    return window["metrics"]["completed"] if window else 0


def pool(pools):
    """The groups' pools of one window together, before attach(): new IPs and observations
    summed, shares weighted by unique IPs, the daily averages' days and unique IPs."""
    days = [p["daily"] for p in pools]
    daily = [d["unique_ips"] for d in days if d["unique_ips"] is not None]
    daily = {"days": max(d["days"] for d in days), "unique_ips": sum(daily) if daily else None}
    return {
        "unique_ips": None,
        "new_ips": sum(p["new_ips"] for p in pools),
        "churn_rate": None,
        **{key: weighted(pools, key) for key in SHARES},
        **dict.fromkeys(PLACES),
        "observations": sum(p["observations"] for p in pools),
        "shares_basis": SHARES_BASIS,
        "daily": daily,
    }


def weighted(pools, key):
    """key of the pools that have it, weighted by their unique IPs; null when none has it."""
    found = [(p[key], p["unique_ips"]) for p in pools if p.get(key) is not None]
    if len(found) == 1:
        return found[0][0]
    total = sum(n for _, n in found)
    return sum(value * n for value, n in found) / total if total else None


def attach(conn, payload, written):
    """{"networks": payload's networks completed} for the head of a v4 payload, else {}.

    written is netlists.write_network_lists()'s {(network id, label): (list_id, entries,
    places)};
    payload's groups carry their estimates (estimates/window.py).
    """
    if payload["schema_version"] < 4 or "networks" not in payload:
        return {}
    runs, buckets = {}, {}
    for entry in payload["networks"]:
        for label, w in entry["windows"].items():
            since, end = w["period"]["start"], w["period"]["end"]
            step = timedelta(minutes=5) if w["period"]["hours"] == 24 else timedelta(days=1)
            for part, width in (("pool", end - since), ("series", step)):
                runs[entry["id"], label, part] = w["_runs"]
                buckets[entry["id"], label, part] = (since, end, width)
    counted = hourly.bucket_ips(conn, runs, buckets)
    previous = parts.previous(conn, groups=False)
    shared = network_scores.overlap(conn, payload, previous)
    estimated = defaultdict(list)
    for group in payload["products"]:
        for label, w in group["windows"].items():
            if "estimated" in w:
                estimated[netlists.network_id(group), label].append(w["estimated"])
    networks = []
    for entry in payload["networks"]:
        windows = {}
        for label, w in entry["windows"].items():
            key = (entry["id"], label)
            ips = counted[(*key, "pool")].get(w["period"]["start"], 0)
            points = fleetsql.fill(w["series"], counted[(*key, "series")])
            found = written.get(key)
            windows[label] = complete(w, ips, points, found, estimated.get(key))
            windows[label]["overlap"] = shared[key]
        networks.append(entry | {"windows": windows})
    network_scores.score(networks, payload, previous)
    return {"networks": networks}  # store.write() validates them (validate())


def complete(window, ips, points, found, blocks):
    """window with its distinct IPs (ips, and points' per series point), its network list
    (found: (list_id, entries, places), or None without one) and its groups' estimates
    (blocks)."""
    entries, places = (found[1], found[2]) if found else ({}, {})
    new = window["pool"]["new_ips"]
    ps = window["pool"] | {"unique_ips": ips, "churn_rate": new / ips if ips else None}
    counts = {name: places.get(name) for name in PLACES}
    ps |= counts | {"daily": ps["daily"] | counts, "list_entries": entries}
    done = {k: v for k, v in window.items() if k != "_runs"} | {"pool": ps, "series": points}
    if blocks:
        done["estimated"] = estimates(blocks, ps, daily=window["period"]["hours"] > 24)
    done["lists"] = {"id": found[0], "entries": entries} if found else None
    done["breakdown_entries"] = entries
    return done


def estimates(blocks, ps, daily=False):
    """The groups' estimated blocks of one window summed (estimates/window.py): unique IPs and
    requests, and per released hour (daily: per UTC day, each the sum of its released hours),
    scaled by the network's distinct IPs. A network window carries no per-country estimate and
    no overlap (NET-SUM-2)."""
    versions = sorted({v for b in blocks for v in b["formula_versions"]})
    value = sum(b["unique_ips"]["value"] for b in blocks)
    figure = {"estimated": True, "formula_version": max(versions)}
    hours = defaultdict(lambda: {"unique_ips": 0, "requests": 0})
    for point in (s for b in blocks for s in b["series"]):
        start = floor_time(point["start"], DAY) if daily else point["start"]
        for key in ("unique_ips", "requests"):
            hours[start][key] += point[key]
    return {
        "estimated": True,
        "formula_versions": versions,
        "hours": sum(b["hours"] for b in blocks),
        "unique_ips": {"value": value, **figure, "method": blocks[0]["unique_ips"]["method"]},
        "requests": {"value": sum(b["requests"]["value"] for b in blocks), **figure},
        "series": [{"start": start} | hours[start] for start in sorted(hours)],
        "breakdown_scale": value / ps["unique_ips"] if ps["unique_ips"] else None,
    }


def validate(payload):
    """The reconciling invariants of a v4 payload's networks (store.validate_payload())."""
    if payload["schema_version"] < 4:
        return
    for entry in payload.get("networks", ()):
        if entry.get("cohort") != netlists.COHORT or set(entry["windows"]) - {"24h", "7d", "30d"}:
            raise ValueError(INVALID)
        for window in entry["windows"].values():
            check(window)


def check(window):
    """One network window's invariants, as store.validate_payload() checks a group's."""
    m, found, ps = window["metrics"], window.get("lists"), window["pool"]
    rates = all(_fraction(m[k]) for k in ("challenge_rate", "block_rate"))
    if {"_runs", "_lead"} & set(window) or not rates:
        raise ValueError(INVALID)
    if not _fraction(m["uptime"]) or not all(_fraction(p.get("uptime")) for p in window["series"]):
        raise ValueError("Availability series must contain fractions or null")
    for metrics in (m, *[t["metrics"] for t in window["targets"].values()]):
        counts = (metrics["completed"], metrics["successes"], metrics["errors"])
        if counts[0] != counts[1] + counts[2] or min(counts) < 0:
            raise ValueError("Snapshot counters do not reconcile")
    if type(ps["unique_ips"]) is not int or ps["observations"] > m["successes"]:
        raise ValueError("IP observations exceed successful requests")
    if found is not None and (
        type(found["id"]) is not int
        or not all(type(n) is int and n >= 0 for n in found["entries"].values())
    ):
        raise ValueError(INVALID)


def _fraction(value):
    return value is None or (type(value) in (int, float) and 0 <= value <= 1)
