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

DAY-SCORE-1: each network's `days` are its 30d window's days, the slices of its series (a day
apart from the window's start, so the last is the 24h window): each day's success_overall, the
day's targets and daily tests measured as a window's (Network.days()) and scored as the windows
are, the networks of one kind per day (target_scores.score_days()). A window's and a day's
"_success" are their card targets' tries (card_success.counts(), browser targets over the 7 days
for a 24h span), which target_scores.score_networks() turns into success_overall
(SUCCESS-ALL-8-1).

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
  and window (network_scores.py);
- the Atlas Score trend (atlas_score_spark): that balanced score with the earlier published
  builds' (score_history.py, ATLAS-SCORE-SPARK-1).
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections import defaultdict
from datetime import timedelta

from .. import verdicts
from . import (
    browser,
    card_success,
    fleetsql,
    hourly,
    latency_basket,
    latency_store,
    netlists,
    network_scores,
    parts,
    score_history,
    standing,
    target_scores,
)
from .aggregate import (
    SeriesSpec,
    floor_time,
    in_window,
    outage_list,
    outage_uptime,
    series,
    series_axis,
    slot_coverage,
    target_timed,
    timed,
    uptime,
    with_under_load,
)
from .latency_days import PERIOD_DAYS, window_delta
from .policy import latency_basket as basket_settings
from .policy import latency_target, slot_settings, uptime_settings
from .underload import slot_memo, slot_rate, time_slots

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
DAY_STEP = timedelta(days=1)  # a 7d or 30d window's series point and a network's days
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


def summaries(
    fleet_groups, bucket_groups, by_product, flagged, as_of, config, each=None, held=None
):
    """A build's networks, ordered by provider, then kind; attach() completes their windows.

    fleet_groups are build()'s members (product, rows, observations, member) by (cohort,
    window); bucket_groups every bucket row of each group; by_product build()'s scheduled group,
    pulse batch runs, outages, tested countries and monitored spans of each product; flagged
    the pulse batches that ran during a census (slot_runs()). each, when given, summarizes the
    networks (parallel_measure.Measure.networks(): in worker processes) as summary() does. held
    is standing.sources(): every window gets its standing (standing.mark()), and only the
    measured ones are scored, each against the measured networks of its kind.
    """
    windows, configs = defaultdict(lambda: defaultdict(list)), {}
    for (_, label), members in fleet_groups.items():
        for p, *_, member in members:
            windows[p["provider"], p["kind"]][label].append(p)
            configs[p["id"]] = member["config"]
    found = [(key, windows[key]) for key in sorted(windows)]
    context = (bucket_groups, by_product, flagged, as_of, config, configs)
    if each is None:
        networks = [summary(labels, context)[0] for _, labels in found]
    else:
        networks = each(found, context)
    standing.mark(networks, held or {}, as_of, config)
    live = standing.live(networks)
    latency_basket.apply(live, config)
    target_scores.score_networks(live, config)
    days = [n for n in networks if "_held" not in n["windows"].get("30d", {})]
    target_scores.score_days(days, config)
    return networks


def summary(labels, context):
    """One network's entry before attach() and the id of its headline group (lead()), whose
    windows its windows' "_lead" are: labels are its groups by window label, context
    summaries()'s inputs."""
    bucket_groups, by_product, flagged, as_of, config, configs = context
    groups = list({p["id"]: p for ms in labels.values() for p in ms}.values())
    union = Network(groups, bucket_groups, by_product, flagged, config)
    entry = union.entry(as_of)
    cfg = configs[union.lead["id"]]  # its required targets and target groups
    with slot_memo():  # its windows, days and points share their time slots' counts
        entry["windows"] = {label: union.window(ms, label, cfg) for label, ms in labels.items()}
        entry["days"] = union.days(labels.get("30d"), cfg)
    return entry, union.lead["id"]


class Network:
    """One network's union: its groups' bucket rows (network and browser rows apart), and the
    pulse batch runs, pulse slots, outages and tested countries of its scheduled groups."""

    def __init__(self, groups, bucket_groups, by_product, flagged, config):
        scheduled, pulses, outages, tested, monitored, counting, self.verdicts = by_product
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
        # Each target's pulse batch runs and slots, in their order: targets() runs once a day.
        self.target_batches, self.target_pulses = defaultdict(list), defaultdict(list)
        for b, d in zip(self.batches, self.pulses, strict=True):
            self.target_batches[b["target_id"]].append(b)
            self.target_pulses[d["target"]].append(d)
        self.down = [
            o | {"country": o["country"].strip()} for product in products for o in outages[product]
        ]
        self.tested = {}
        for product in products:
            for country, last in tested[product].items():
                self.tested[country] = max(self.tested.get(country, last), last)
        self.monitored = [t for product in products for t in monitored[product]]
        # Each country's counting spans, of all the scheduled products (None: not clipped).
        self.counting = None if counting is None else defaultdict(list)
        for product in products if counting is not None else ():
            for country, found in counting.get(product, {}).items():
                self.counting[country] += found
        self.minimum = slot_settings(config)["min_latency_successes"]
        self.target = latency_target(config)
        self.basket = (basket_settings(config) or {}).get("basket")
        self.threshold = uptime_settings(config)["min_successes"]
        self.measured = uptime_settings(config)["basis"] == "measured_time"
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
            }
            | self.country_status(asked),
            "census": g["census"] if g["id"] in self.scheduled else [],
            "pricing": g["pricing"],
            "outages": outage_list(self.down, as_of - timedelta(hours=OUTAGE_HOURS), as_of),
            "evidence": g["windows"]["30d"]["evidence"] if "30d" in g["windows"] else {},
        }

    def country_status(self, asked):
        """{"country_status": each requested country's status, sorted} with the methodology's
        country_verdicts (verdicts.status() over the groups' products), else nothing."""
        if self.verdicts is None:
            return {}
        latest = [self.verdicts.get(p, {}) for p in {g["product_id"] for g in self.groups}]
        found = [
            {"country": c} | verdicts.status([own[c] for own in latest if c in own])
            for c in sorted(asked)
        ]
        return {"country_status": found}

    def window(self, groups, label, cfg):
        """One window over the union, waiting for attach(): "_runs" lists its runs, "_lead"
        is the headline group's window (its evidence, network_scores.member())."""
        period = groups[0]["windows"][label]["period"]
        since, end, hours = period["start"], period["end"], period["hours"]
        rows = in_window(self.rows, since, end, hours)
        slots = [d for d in self.pulses if since <= d["scheduled_for"] < end]
        m = timed(rows, hours, self.minimum, slots, self.minutes, since)
        with_under_load(m, rows, self.minimum)
        up = uptime(self.batches, since, end, self.threshold)
        runs = self.monitored if self.measured else None
        up |= outage_uptime(self.down, self.tested, since, end, runs, self.counting)
        m["uptime"] = up["value"]
        if self.scheduled:
            slot_coverage(m, self.batches, since, end)
        before = since - (end - since)
        previous = [d for d in self.pulses if before <= d["scheduled_for"] < since]
        prev_rows = self.inside(self.rows, before, since)
        prev = timed(prev_rows, hours, self.minimum, previous, self.minutes, before)
        windows = [p["windows"][label] for p in groups]
        latency = {"latency_target": self.target, "minimum": self.minimum}
        latency |= {"outages": self.down}
        points = series(rows, (), SeriesSpec(since, end, hours, tested=self.monitored, **latency))
        point_trailing(points, self, cfg, end, hours)
        rate, old = m["success_rate"], prev["success_rate"]
        targets = self.targets(cfg, rows, (since, end, hours))
        found = {
            "period": period,
            "readiness": "partial" if m["completed"] else "unavailable",
            "metrics": m,
            "uptime": up,
            "tests": {name: sum(w["tests"][name] for w in windows) for name in TESTS},
            "pool": pool([w["pool"] for w in windows]),
            "targets": targets,
            "series": points,
            "success_delta": rate - old if rate is not None and old is not None else None,
            "scores": {},
            "overlap": None,
            "_runs": sorted({r["run_key"] for r in rows}),
            "_lead": self.lead["windows"].get(label, {}),
            "_hours": standing.hours(rows, end),
            "_success": self.card_counts(cfg, card_success.http_counts(cfg, rows), end, hours),
        }
        made = [g["windows"][label].get("breakdowns_as_of") for g in groups]
        if any(made):  # its census-pulse member's list's cutoff, the oldest (CALC-SCALE-4)
            found["breakdowns_as_of"] = min(m for m in made if m)
        if self.basket:  # LATENCY-BASKET-1: latency_basket.apply() in summaries()
            axis = (series_axis(end, hours), [p["at"] for p in points])
            own = (rows, self.target, self.minimum, set(self.basket), axis)
            found["_basket"] = latency_basket.measure(*own)
        if label == "30d":  # LATENCY-DAY-STORE-1: attach() makes its month with the stored days
            own = (self.rows, self.target, since, end, self.minimum)
            found["_latency_days"] = latency_store.day_records(*own)
        if label in PERIOD_DAYS:  # LATENCY-DELTA-24H-1, -PERIOD-1; 30d: its week or day here
            days = min(PERIOD_DAYS[label], 7)
            found["latency_day_delta"] = window_delta(
                self.rows, self.target, end, self.minimum, days
            )
        return found

    def days(self, groups, cfg):
        """The 30d window's days (none without one): each {"start", "targets"}, the targets()
        of the day's rows as a 24h window's, which target_scores.score_days() scores."""
        if not groups:
            return []
        period = groups[0]["windows"]["30d"]["period"]
        since, end = period["start"], period["end"]
        rows, browser_rows = defaultdict(list), defaultdict(list)
        for r in self.inside(self.rows, since, end):
            rows[(r["bucket_start"] - since) // DAY_STEP].append(r)
        # The last day is the 24h window, with the daily tests it reaches back to (in_window()).
        last, cut = math.ceil((end - since) / DAY_STEP) - 1, end - DAY_STEP
        rows[last] += [r for r in in_window(self.rows, cut, end, 24) if r["bucket_start"] < cut]
        for r in self.browser:  # browser.counted_at(): a whole run, in the day that holds its end
            if since <= (at := browser.counted_at(cfg, r)) < end:
                browser_rows[(at - since) // DAY_STEP].append(r)
        days = []
        for k in range(math.ceil((end - since) / DAY_STEP)):
            start = since + k * DAY_STEP
            span = (start, min(start + DAY_STEP, end), 24)
            targets = self.targets(cfg, rows[k], span, browser_rows[k])
            own = self.card_counts(cfg, card_success.http_counts(cfg, rows[k]), span[1], 24)
            days.append({"start": start, "targets": targets, "_success": own})
        return days

    def targets(self, cfg, rows, span, browser_rows=None):
        """Each target's metrics over the union, as a group's (calculator.build()): browser
        targets from the browser rows (browser_rows, every one by default), latency as
        aggregate.target_timed() gives it."""
        since, end, hours = span
        found = {}
        own_browser = self.browser if browser_rows is None else browser_rows
        for target, own, extra in browser.targets(cfg, rows, own_browser, since, end):
            batches = self.target_batches[target]
            slots = self.target_pulses[target]
            slots = [d for d in slots if since <= d["scheduled_for"] < end]
            timing = (self.minimum, self.minutes, self.target)
            cut = browser.timed_since(cfg, extra, since)
            tm = target_timed((target, own, extra), hours, slots, timing, cut)
            tm["uptime"] = uptime(batches, since, end, self.threshold)["value"]
            tm["units"] = tm["units"] | {"uptime": "fraction of pulse batches"}
            if self.scheduled and not extra:  # a browser target's coverage stays its buckets'
                slot_coverage(tm, batches, since, end)
            readiness = "measured" if tm["completed"] else "unavailable"
            found[target] = {"metrics": tm, "readiness": readiness, **extra}
        return found

    def card_counts(self, cfg, http, end, hours):
        """card_success.counts() of a span of `hours` ending at end: http, its HTTP targets'
        counts, and its browser targets' over its browser window."""
        since = card_success.browser_since(cfg, end, hours)
        return card_success.counts(cfg, http, self.browser, since, end)

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


def point_trailing(points, net, cfg, end, hours):
    """Each series point's "_success" (card_success.counts()) and "trailing_success_rate" over
    the TRAILING window of the headline's length ending at the point's end (SUCCESS-TRAILING-1,
    #1301): the headline's own rows (in_window()), pulses and time-slot votes (time_slots(),
    slot_rate(), as timed() makes the window's success_rate), so the newest point is the
    window's and a steady network's points read its headline. score_networks() scores
    "_success" as the window's success_overall; a trailing window reaching before the raw rows
    kept has the rows that exist. A point whose trailing window holds less than one whole day of
    the network's HTTP card target rows (before or on its first day of data) has no counts and
    no rate: "_short", not_enough_data (SUCCESS-FULL-DAY-1), never a score from a few hours.
    net: the Network (its rows, browser rows and pulses); cfg: its lead group's config."""
    first, step = series_axis(end, hours)
    span = timedelta(hours=hours)
    oldest = first + step - span  # the first point's trailing window starts there
    trailing = card_success.Trailing(cfg, net.rows)
    pulses = [d for d in net.pulses if oldest <= d["scheduled_for"] < end]
    votes = sorted(
        (key, rate)
        for key, s in time_slots(in_window(net.rows, oldest, end, hours), pulses).items()
        if (rate := slot_rate(s)) is not None
    )
    keys = [key for key, _ in votes]
    since = trailing.first  # its first HTTP card target row
    for k, point in enumerate(points):
        stop = min(first + (k + 1) * step, end)
        start = stop - span
        if since is None or stop - since < timedelta(days=1):
            point |= {"_success": {}, "_short": True, "trailing_success_rate": None}
            continue
        own = card_success.browser_since(cfg, stop, hours)
        http = trailing.counts(start, stop, hours)
        point["_success"] = card_success.counts(cfg, http, net.browser, own, stop)
        rates = [rate for _, rate in votes[bisect_left(keys, start) : bisect_left(keys, stop)]]
        point["trailing_success_rate"] = sum(rates) / len(rates) if rates else None


def attach(conn, payload, written, overlaps=None):
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
            first, step = series_axis(end, w["period"]["hours"])
            for part, frm, width in (("pool", since, end - since), ("series", first, step)):
                runs[entry["id"], label, part] = w["_runs"]
                buckets[entry["id"], label, part] = (frm, end, width)
    counted = hourly.bucket_ips(conn, runs, buckets)
    latency_store.complete(conn, payload["networks"], payload["as_of"])
    previous = parts.previous(conn, groups=False)
    shared = network_scores.overlap(conn, payload, previous, overlaps)
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
    score_history.spark(conn, networks, payload["as_of"], payload["methodology"]["id"])
    standing.hold(networks, previous)
    target_scores.previous_scores(networks, previous, payload["as_of"])
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
    if {"_runs", "_lead", "_held", "_hours"} & set(window) or not rates:
        raise ValueError(INVALID)
    if not _fraction(m["uptime"]) or not all(_fraction(p.get("uptime")) for p in window["series"]):
        raise ValueError("Availability series must contain fractions or null")
    for metrics in (m, *[t["metrics"] for t in window["targets"].values()]):
        counts = (metrics["completed"], metrics["successes"], metrics["errors"])
        if counts[0] != counts[1] + counts[2] or min(counts) < 0:
            raise ValueError("Snapshot counters do not reconcile")
    # A frozen window's IP pool is the current window's, its metrics its last values (standing.py).
    frozen = (window.get("standing") or {}).get("status") == standing.FROZEN
    if type(ps["unique_ips"]) is not int or (ps["observations"] > m["successes"] and not frozen):
        raise ValueError("IP observations exceed successful requests")
    if found is not None and (
        type(found["id"]) is not int
        or not all(type(n) is int and n >= 0 for n in found["entries"].values())
    ):
        raise ValueError(INVALID)


def _fraction(value):
    return value is None or (type(value) in (int, float) and 0 <= value <= 1)
