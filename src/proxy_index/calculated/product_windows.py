"""One product group's windows, measured for calculator.build(): each window's metrics, pool,
uptime, targets, series and the raw inputs of its scores (its fleet member).

build() hands every step its shared state (calculator._Build: the settings, the groups and the
source of IP-derived values); measure() fills a group's windows, the IPs of each window and its
fleet member, in the order build() always did.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import browser, network
from .aggregate import (
    PRICES,
    UNAVAILABLE,
    SeriesSpec,
    census_split,
    daily_average,
    latency_detail,
    load_factors,
    metrics,
    outage_list,
    outage_uptime,
    product_inputs,
    series,
    slot_coverage,
    target_score,
    target_timed,
    timed,
    uptime,
    with_under_load,
)
from .inputs import requested_geo
from .parts import sparse
from .policy import effective, hosting_usage_types
from .window_scoring import fresh, pricing


@dataclass(frozen=True)
class _Product:
    """What every window of one product group shares (measure())."""

    key: tuple
    p: dict
    cfg: dict
    raw: list
    browser_raw: list
    scheduled: bool
    batches: list
    slot_runs: list
    price_rows: list
    down: list
    days: object
    tested: dict
    monitored: object
    geo: list


@dataclass(frozen=True)
class _Window:
    """One window of a product group (_read_window()): its rows, observations (seen), time slots and
    metrics, and the latency target's rows and load factors."""

    hours: int
    since: datetime
    rows: list
    seen: list
    slots_now: list
    metrics: dict
    timed_rows: list
    factors: object
    reference: object
    excluded: object


def measure(step, key, p):
    """Measure product group p (key: (product_id, cohort)): its census, pricing, outages and
    every window, each window's IPs in step.sets and its member in step.fleet_groups."""
    as_of, product_id = step.as_of, p["product_id"]
    scheduled_groups, pulse_groups, outage_groups, tested_groups, monitored_groups = step.by_product
    cfg = effective(step.config, p["provider"], p["product"])
    raw, browser_raw = browser.split(step.bucket_groups[key])  # browser rows: per target only
    p["census"] = sorted(step.census_groups[product_id], key=lambda c: c["country"])
    # Uptime is a product fact measured by pulse batches; it belongs to the scheduled cohort.
    scheduled = scheduled_groups.get(product_id) == key
    batches = pulse_groups[product_id] if scheduled else []
    # Done pulse runs in their time slots.
    slot_runs = network.slot_runs(batches, step.slot_minutes * 60, step.flagged_batches)
    p["pricing"], price_rows = pricing(step.evidence, product_id, p["cohort"], as_of)
    down = (
        [o | {"country": o["country"].strip()} for o in outage_groups[product_id]]
        if scheduled
        else []
    )
    p["outages"] = outage_list(down, as_of - timedelta(hours=720), as_of)
    # Daily pools of the last 30 days; every window averages its own days of them.
    days = step.source.days(key, as_of, max(step.config["windows"]) // 24)
    tested = tested_groups[product_id] if scheduled else {}
    monitored = monitored_groups[product_id] if scheduled else ()
    geo = step.geo_groups[key]  # read now, per product, and dropped once measured (CALC-MEM-2)
    geo = [*geo, *requested_geo(raw, as_of, step.country_names)]  # a daily test's requests
    product = _Product(
        key,
        p,
        cfg,
        raw,
        browser_raw,
        scheduled,
        batches,
        slot_runs,
        price_rows,
        down,
        days,
        tested,
        monitored,
        geo,
    )
    for hours in step.config["windows"]:
        _window(step, product, hours)


def _window(step, product, hours):
    """Measure window `hours` of product group product.p: its entry in p["windows"], its IPs in
    step.sets and its member in step.fleet_groups."""
    key, p = product.key, product.p
    label = {24: "24h", 168: "7d", 720: "30d"}[hours]
    win = _read_window(step, product, hours)
    ps, ips, kept, up = _pool_and_uptime(step, product, win)
    ev = _evidence(step, p, win.since)
    values = {e["metric"]: e["value"] for e in ev}
    member = _member(step, product, win, label, ev)
    targets = _targets(step, product, win, member)
    member["inputs"] = product_inputs(ps, p["pricing"], product.cfg, p["kind"], (up, values))
    member["observed"] = win.metrics["last_observed"]
    prev_m = _previous_period(step, product, win)
    w = (
        _window_head(step, win)
        | {
            "pool": ps,
            "uptime": up,
            **kept,  # "breakdowns", or a reused list (incremental.py)
            "targets": targets,
            "scores": {},
            "evidence": values,
        }
        | _window_sources(step, product, win, ev, values)
        | _window_comparison(win, prev_m)
    )
    w["series"] = step.source.ips(key, hours, w["series"])  # incremental: counted IPs
    p["windows"][label] = w
    step.sets[(key, label)] = set(ips)
    step.fleet_groups[(p["cohort"], label)].append((p, win.rows, win.seen, member))


def _read_window(step, product, hours):
    """The rows, observations and time slots of window `hours`, its metrics and load factors."""
    as_of, minimum = step.as_of, step.minimum
    since = as_of - timedelta(hours=hours)
    rows = [r for r in product.raw if since <= r["bucket_start"] < as_of]
    seen = step.source.seen(product.key, since, as_of)
    slots_now = [d for d in product.slot_runs if since <= d["scheduled_for"] < as_of]
    # Every request counts (census, pulse and load alike); success is time-weighted.
    m = timed(rows, hours, minimum, slots_now, step.slot_minutes)
    timed_rows = [r for r in rows if r["target"] == step.timed_target]
    factors, reference, excluded = load_factors(timed_rows, minimum)
    with_under_load(m, rows, factors, minimum, excluded)
    return _Window(hours, since, rows, seen, slots_now, m, timed_rows, factors, reference, excluded)


def _pool_and_uptime(step, product, win):
    """The window's pool counts (with its lists), IPs, breakdowns and uptime; the uptime and
    the scheduled cohort's slot coverage go into its metrics."""
    as_of, key, since, hours, m = step.as_of, product.key, win.since, win.hours, win.metrics
    located = [g for g in product.geo if g["hours"] <= hours]
    host = hosting_usage_types(product.cfg)
    ps, ips, lists, kept = step.source.window(
        key, win.seen, since, hours, host, located, step.build_id
    )
    ps["daily"] = daily_average(product.days, hours)
    # Pulse-slot counts stay for reference; the published value is outage time.
    up = uptime(product.batches, since, as_of, step.threshold)
    up |= outage_uptime(product.down, product.tested, since, as_of)
    m["uptime"] = up["value"]
    if product.scheduled:
        # Census/pulse cohorts: coverage is the share of pulse slots filled, not of
        # 5-minute buckets (pulses run every 30 minutes, all countries at one tick).
        slot_coverage(m, product.batches, since, as_of)
    ps |= lists
    return ps, ips, kept, up


def _evidence(step, p, since):
    """The evidence rows of product group p valid over the whole window from since (no prices)."""
    return [
        e
        for e in step.evidence
        if e["product_id"] == p["product_id"]
        and e["cohort"] in (p["cohort"], "*")
        and e["metric"] not in PRICES
        and e["period_start"] <= since
        and e["period_end"] >= step.as_of
        and e["valid_until"] > step.as_of
    ]


def _member(step, product, win, label, ev):
    """Raw inputs of the window's scores; the peer pass (calculator._fleet_rows()) scores them."""
    cfg = product.cfg
    return {
        "config": cfg,
        "fresh": fresh(win.metrics["last_observed"], step.as_of, cfg["score_valid_hours"]),
        "targets": {},
        "old": step.prev_products.get(product.p["id"], {}).get("windows", {}).get(label, {}),
        "evidence": ev + product.price_rows,
    }


def _targets(step, product, win, member):
    """Each target's entry of the window; its score inputs go to member["targets"]."""
    p, as_of = product.p, step.as_of
    targets = {}
    for target, own, extra in browser.targets(
        product.cfg, win.rows, product.browser_raw, win.since, as_of
    ):
        tm = _target_metrics(step, product, win, (target, own, extra))
        tc = effective(step.config, p["provider"], p["product"], target)
        member["targets"][target] = target_score(tm, tc, target) | {
            "fresh": fresh(tm["last_observed"], as_of, tc["score_valid_hours"]),
            "observed": tm["last_observed"],
            "valid_hours": tc["score_valid_hours"],
        }
        targets[target] = {
            # benchmark is kept for older readers: every request is the benchmark now.
            "metrics": tm,
            "benchmark": tm,
            "score": None,
            "readiness": "measured" if tm["completed"] else "unavailable",
            **extra,  # browser targets: K-BP6 outcomes, page loads, group and sources
        }
    return targets


def _target_metrics(step, product, win, found):
    """The window's metrics of one target (found: browser.targets()'s target, its own rows and
    its extra fields)."""
    target, own, extra = found
    as_of, since = step.as_of, win.since
    pulses = [d for d in win.slots_now if d["target"] == target]
    timing = (step.minimum, step.slot_minutes, step.timed_target)
    tm = target_timed(found, win.hours, pulses, timing, (win.factors, win.excluded))
    # Outages are per product and country, so a target keeps its pulse-slot share.
    tm["uptime"] = uptime(
        [b for b in product.batches if b["target_id"] == target], since, as_of, step.threshold
    )["value"]
    tm["units"] = tm["units"] | {"uptime": "fraction of pulse batches"}
    if product.scheduled and not extra:  # a browser target's coverage stays its buckets'
        slot_coverage(tm, [b for b in product.batches if b["target_id"] == target], since, as_of)
    return tm


def _previous_period(step, product, win):
    """The metrics of the period of the window's length just before it."""
    since = win.since
    previous_rows = [
        r for r in product.raw if since - timedelta(hours=win.hours) <= r["bucket_start"] < since
    ]
    before = since - timedelta(hours=win.hours)
    prev_m = timed(
        previous_rows,
        win.hours,
        step.minimum,
        [d for d in product.slot_runs if before <= d["scheduled_for"] < since],
        step.slot_minutes,
    )
    if product.scheduled:
        slot_coverage(prev_m, product.batches, since - timedelta(hours=win.hours), since)
    return prev_m


def _window_head(step, win):
    """The start of a window's entry: its period, metrics, latency and census overlap."""
    m, since, as_of = win.metrics, win.since, step.as_of
    return {
        "period": {"start": since, "end": as_of, "hours": win.hours, "boundary": "[start,end)"},
        "readiness": None,  # once scored
        "metrics": m,
        # Kept for older readers; identical to metrics (every request counts).
        "benchmark": m,
        "latency": latency_detail(
            win.timed_rows,
            win.factors,
            win.reference,
            step.minimum,
            win.excluded,
            [
                c
                for c in step.references
                if since.date().isoformat() <= c["day"] <= as_of.date().isoformat()
            ],
        )
        | {"target": step.timed_target},
        # Pulses (and load waves) that ran during a census, and the clean ones.
        "census_overlap": census_split(
            win.rows, win.slots_now, step.minimum, step.slot_minutes, step.timed_target
        ),
    }


def _window_sources(step, product, win, ev, values):
    """A window entry's evidence sources, unavailable metrics and series."""
    return {
        "evidence_sources": [
            {k: e[k] for k in ("metric", "source", "period_start", "period_end", "valid_until")}
            for e in ev + product.price_rows
        ],
        "unavailable": {k: v for k, v in UNAVAILABLE.items() if k not in values},
        "series": sparse(
            series(
                win.rows,
                win.seen,
                SeriesSpec(
                    win.since,
                    step.as_of,
                    win.hours,
                    outages=product.down,
                    tested=product.monitored,
                    latency_target=step.timed_target,
                    minimum=step.minimum,
                    factors=win.factors,
                    excluded=win.excluded,
                ),
            ),
            every=product.scheduled or win.hours != 24,  # only daily groups' 24h series are sparse
        ),
    }


def _window_comparison(win, prev_m):
    """The end of a window's entry: its previous period, overlap, roles and tests."""
    m, rows, hours = win.metrics, win.rows, win.hours
    return {
        "previous_period": prev_m,
        "success_delta": m["success_rate"] - prev_m["success_rate"]
        if m["success_rate"] is not None and prev_m["success_rate"] is not None
        else None,
        "overlap": {"exclusive_share": None, "shared_share": None, "compared_products": 0},
        "roles": {
            role: metrics([r for r in rows if r["role"] == role], hours)["completed"]
            for role in ("exploratory", "pool_sampling", "benchmark", "monitor")
        },
        # Completed requests per test; all of them count for every metric.
        "tests": {
            name: sum(
                r["counters"]["completed"] for r in rows if (r["test"] or "unscheduled") == name
            )
            for name in ("census", "pulse", "load", "unscheduled")
        },
    }
