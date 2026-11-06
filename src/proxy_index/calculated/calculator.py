"""Offline calculation over facts, followed by immutable, atomic publication."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .. import detect, traffic
from ..storage import connect
from ..targets import registry
from . import browser, incremental, network
from .aggregate import (
    NOT_LATENCY_TARGET,
    PRICES,
    UNAVAILABLE,
    census_split,
    daily_average,
    floor_time,
    latency_detail,
    load_factors,
    metrics,
    outage_list,
    outage_uptime,
    product_inputs,
    series,
    slot_coverage,
    target_score,
    timed,
    uptime,
    with_under_load,
)
from .fleet import fleet_entry, fleet_totals
from .inputs import cutoff, group_by_product, light_load, previous_products, tested_by_product
from .latency import KEYS
from .parts import previous as previous_payload
from .parts import read as stored_payload
from .parts import sparse
from .policy import (
    effective,
    hosting_usage_types,
    latency_target,
    load,
    slot_settings,
    uptime_settings,
)
from .releases import snapshot_fields
from .scheduled import EVENTS_IN_SNAPSHOT as EVENTS_IN_SNAPSHOT
from .scheduled import scheduled_inputs as scheduled_inputs
from .store import discard, expire, prune
from .window_scoring import cohort_for as cohort_for
from .window_scoring import fresh as fresh
from .window_scoring import pricing as pricing
from .window_scoring import rank as rank
from .window_scoring import retain as retain
from .window_scoring import score_window as score_window
from .window_scoring import slot_length as slot_length

SCHEMA_VERSION = 4
# The calculation's modules (and detect, traffic, browser): a change to any changes the id, so the
# build fingerprint and every list signature.
SOURCES = "calculator aggregate policy slim inputs lists incremental hourly fleetsql rollups "
SOURCES += "sqlwindow poolfig network network_scores scheduled scores window_scoring"
IMPLEMENTATION_ID = hashlib.sha256(
    b"".join(Path(__file__).with_name(f"{name}.py").read_bytes() for name in SOURCES.split())
    + b"".join(Path(m.__file__).read_bytes() for m in (detect, traffic, browser))
).hexdigest()
# A run's role label, when no calculation_roles row assigns one, follows its batch kind. Roles
# are display labels: every request counts for every metric whatever its role.
BATCH_ROLES = {"census": "pool_sampling", "pulse": "monitor", "load": "benchmark"} | browser.ROLES


@dataclass(frozen=True)
class _Build:
    """What build() hands its fleet and payload steps: its inputs, its settings and its groups
    (by_product: build()'s scheduled group, pulse batch runs, outages, tested countries and
    monitored spans of each product, as network.summaries() reads them)."""

    runs: list
    config: dict
    method_id: str
    as_of: datetime
    build_id: int
    release: object
    events: object
    traffic: dict | None
    minimum: int
    slot_minutes: int
    timed_target: str
    groups: dict
    fleet_groups: dict
    bucket_groups: dict
    by_product: tuple
    flagged_batches: dict



def calculate(database, methodology, *, as_of=None, publish=False, mode="full"):
    """Calculate (and publish) a build. mode "full" reads every observation of the window, the
    oracle; "incremental" (`calculate --incremental`) reads only the missed windows'."""
    if mode not in incremental.MODES:
        raise ValueError(f"calculation mode must be one of {incremental.MODES}, not {mode!r}")
    config, method_id = load(methodology)
    as_of, latest = as_of or datetime.now(UTC), as_of is None
    if isinstance(as_of, str):
        as_of = datetime.fromisoformat(as_of)
    if as_of.tzinfo is None:
        raise ValueError("Measurement cutoff requires a timezone")
    with connect(database) as conn:
        conn.row_factory = dict_row
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.execute("SELECT pg_advisory_xact_lock(817452902)")
        as_of = cutoff(conn, as_of, latest)  # on the 5-minute grid of the stored facts
        inputs = incremental.read(conn, config, method_id, as_of, mode)
        previous = previous_payload(conn)  # only the groups' ids and scores
        fingerprint = inputs.fingerprint
        conn.execute(
            "INSERT INTO methodologies VALUES (%s,%s,now()) ON CONFLICT DO NOTHING",
            (method_id, Jsonb(config)),
        )
        existing = conn.execute(
            "SELECT build_id FROM calculation_builds "
            "WHERE methodology_id=%s AND as_of=%s AND input_hash=%s",
            (method_id, as_of, fingerprint),
        ).fetchone()
        payload = None
        if existing:
            payload = stored_payload(conn, existing["build_id"])
            if payload is None:
                # A pruned draft is recalculated as a new, and therefore newest, build.
                discard(conn, existing["build_id"])
        if payload is None:
            build_id = conn.execute(
                "INSERT INTO calculation_builds(methodology_id,as_of,input_hash) "
                "VALUES (%s,%s,%s) RETURNING build_id",
                (method_id, as_of, fingerprint),
            ).fetchone()["build_id"]
            payload = build(
                inputs.runs,
                inputs.buckets,
                inputs.source,
                inputs.evidence,
                config,
                method_id,
                as_of,
                build_id,
                inputs.release,
                previous,
                **inputs.scheduled,
            )
            payload = inputs.store(conn, build_id, payload, config)
        if publish:
            if previous and datetime.fromisoformat(previous["as_of"]) > as_of:
                raise ValueError("Cannot replace current snapshot with an older cutoff")
            conn.execute(
                "INSERT INTO current_calculation VALUES (true,%s) ON CONFLICT(singleton) "
                "DO UPDATE SET build_id=excluded.build_id",
                (int(payload["snapshot_id"]),),
            )
            conn.execute(
                "UPDATE calculation_builds SET published=true WHERE build_id=%s",
                (int(payload["snapshot_id"]),),
            )
        prune(conn)
        if publish:
            expire(conn)
        return payload


def build(
    runs,
    buckets,
    observations,
    evidence_rows,
    config,
    method_id,
    as_of,
    build_id,
    release,
    previous,
    *,
    pulse_minutes=None,
    overlaps=(),
    references=(),
    geo=(),
    pulses=(),
    census=(),
    outages=(),
    tested=(),
    events=(),
    traffic=None,
):
    slot_minutes = slot_length(pulse_minutes)
    slot_seconds = slot_minutes * 60
    # Pulse and load runs that ran during a census of the same product: the census batch ids.
    flagged_runs = {o["run_key"]: o["census"] for o in overlaps if o["run_key"] is not None}
    flagged_batches = {(o["batch_id"], o["product_id"]): o["census"] for o in overlaps}
    minimum = slot_settings(config)["min_latency_successes"]
    # Every latency figure comes from this one target.
    timed_target = latency_target(config)
    groups = {}
    run_map = {}
    bucket_groups = defaultdict(list)
    geo_groups = defaultdict(list)
    scheduled_groups = {}
    scope_groups = {}
    for r in runs:
        cfg = effective(config, r["provider"], r["name"])
        linked = r.get("batch_id") is not None
        cohort, criteria = cohort_for(r["scope"], cfg.get("kind", r["name"]), linked)
        key = (r["product_id"], cohort)
        r["group"], r["light"] = key, light_load(r, flagged_runs, timed_target)
        run_map[r["run_key"]] = r
        if linked:
            scheduled_groups[r["product_id"]] = key
        elif "scope_id" in r:
            scope_groups[r["scope_id"]] = key
        groups.setdefault(
            key,
            {
                "id": f"{r['product_id']}:{cohort}",
                "product_id": r["product_id"],
                "provider": r["provider"],
                "product": r["name"],
                "name": cfg.get("display_name", r["provider"].title()),
                "kind": cfg.get("kind", r["name"]),
                "cohort": cohort,
                "criteria": criteria,
                "windows": {},
                "scoring": {
                    k: cfg[k]
                    for k in (
                        "weights",
                        "required_targets",
                        "benchmark_weights",
                        "minimum_requests",
                        "minimum_coverage",
                        "score_valid_hours",
                        "peer",
                    )
                }
                | {
                    "hosting_usage_types": hosting_usage_types(cfg),
                    "slots": slot_settings(cfg)
                    | {"minutes": slot_minutes, "pulse_interval_minutes": pulse_minutes},
                },
            },
        )
    for b in buckets:
        r = run_map[b["run_key"]]
        test = r.get("test")
        # Pulse and load requests belong to their batch's slot; census and unbatched requests
        # to the slot their bucket starts in.
        at = r.get("slot_at") if test in ("pulse", "load") and r.get("slot_at") else None
        country = r.get("batch_country") or r["scope"]["config"].get("requested_country")
        # Pulses and load waves that ran during a census of the product: requests, not light.
        overlapped = flagged_runs.get(b["run_key"], [])
        bucket_groups[r["group"]].append(
            b
            | {
                "target": r["scope"]["target"]["id"],
                "role": r["role"],
                "test": test,
                "during_census": bool(overlapped),
                "census_batches": overlapped,
                "light": r["light"],
                "slot": floor_time(at or b["bucket_start"], slot_seconds),
                "country": country.strip().upper() if country else None,
                "region": r.get("batch_region"),
                "concurrency": r["scope"]["config"].get("concurrency"),
                "last_observed": min(r["finished_at"], b["bucket_start"] + timedelta(minutes=5)),
            }
        )
    # What build() derives from IPs comes from its source (incremental.py): the observations, all
    # aggregated here (the full calculation), or the incremental calculation's.
    source = observations
    if not isinstance(source, incremental.Full):
        source = incremental.Full(observations)
    source.group(run_map)
    for g in geo:
        key = (
            scheduled_groups.get(g["product_id"])
            if g["scope_id"] is None
            else scope_groups.get(g["scope_id"])
        )
        if key:
            geo_groups[key].append(g)
    pulse_groups, outage_groups = group_by_product(pulses), group_by_product(outages)
    census_groups = group_by_product(census, drop_id=True)
    tested_groups, monitored_groups = tested_by_product(tested)
    threshold = uptime_settings(config)["min_successes"]
    prev_products = previous_products(previous, as_of)
    sets = {}
    fleet_groups = defaultdict(list)
    for key, p in groups.items():
        cfg = effective(config, p["provider"], p["product"])
        raw, browser_raw = browser.split(bucket_groups[key])  # browser rows: per target only
        p["census"] = sorted(census_groups[p["product_id"]], key=lambda c: c["country"])
        # Uptime is a product fact measured by pulse batches; it belongs to the scheduled cohort.
        scheduled = scheduled_groups.get(p["product_id"]) == key
        batches = pulse_groups[p["product_id"]] if scheduled else []
        # Done pulse runs in their time slots.
        slot_runs = network.slot_runs(batches, slot_seconds, flagged_batches)
        p["pricing"], price_rows = pricing(evidence_rows, p["product_id"], p["cohort"], as_of)
        down = (
            [o | {"country": o["country"].strip()} for o in outage_groups[p["product_id"]]]
            if scheduled
            else []
        )
        p["outages"] = outage_list(down, as_of - timedelta(hours=720), as_of)
        # Daily pools of the last 30 days; every window averages its own days of them.
        days = source.days(key, as_of, max(config["windows"]) // 24)
        for hours in config["windows"]:
            label = {24: "24h", 168: "7d", 720: "30d"}[hours]
            since = as_of - timedelta(hours=hours)
            rows = [r for r in raw if since <= r["bucket_start"] < as_of]
            seen = source.seen(key, since, as_of)
            slots_now = [d for d in slot_runs if since <= d["scheduled_for"] < as_of]
            # Every request counts (census, pulse and load alike); success is time-weighted.
            m = timed(rows, hours, minimum, slots_now, slot_minutes)
            timed_rows = [r for r in rows if r["target"] == timed_target]
            factors, reference, excluded = load_factors(timed_rows, minimum)
            with_under_load(m, rows, factors, minimum, excluded)
            located = [g for g in geo_groups[key] if g["hours"] <= hours]
            host = hosting_usage_types(cfg)
            ps, ips, lists, kept = source.window(key, seen, since, hours, host, located, build_id)
            ps["daily"] = daily_average(days, hours)
            # Pulse-slot counts stay for reference; the published value is outage time.
            up = uptime(batches, since, as_of, threshold)
            up |= outage_uptime(
                down, tested_groups[p["product_id"]] if scheduled else {}, since, as_of
            )
            m["uptime"] = up["value"]
            if scheduled:
                # Census/pulse cohorts: coverage is the share of pulse slots filled, not of
                # 5-minute buckets (pulses run every 30 minutes, all countries at one tick).
                slot_coverage(m, batches, since, as_of)
            ps |= lists
            old = prev_products.get(p["id"], {}).get("windows", {}).get(label, {})
            ev = [
                e
                for e in evidence_rows
                if e["product_id"] == p["product_id"]
                and e["cohort"] in (p["cohort"], "*")
                and e["metric"] not in PRICES
                and e["period_start"] <= since
                and e["period_end"] >= as_of
                and e["valid_until"] > as_of
            ]
            values = {e["metric"]: e["value"] for e in ev}
            targets = {}
            # Raw inputs of this window's scores; the peer pass below scores them.
            member = {
                "config": cfg,
                "fresh": fresh(m["last_observed"], as_of, cfg["score_valid_hours"]),
                "targets": {},
                "old": old,
                "evidence": ev + price_rows,
            }
            for target, own, extra in browser.targets(cfg, rows, browser_raw, since, as_of):
                tm = timed(
                    own,
                    hours,
                    minimum,
                    [d for d in slots_now if d["target"] == target],
                    slot_minutes,
                )
                # Latency is the latency target's alone; every other target has none.
                with_under_load(tm, own, factors, minimum, excluded)
                if target != timed_target:
                    tm["latency_ms"] = dict.fromkeys(KEYS)
                    tm["latency_under_load_ms"] = dict.fromkeys(KEYS)
                    tm["latency_reason"] = NOT_LATENCY_TARGET
                    tm["under_load"] = {"reason": NOT_LATENCY_TARGET}
                # Outages are per product and country, so a target keeps its pulse-slot share.
                tm["uptime"] = uptime(
                    [b for b in batches if b["target_id"] == target], since, as_of, threshold
                )["value"]
                tm["units"] = tm["units"] | {"uptime": "fraction of pulse batches"}
                if scheduled and not extra:  # a browser target's coverage stays its buckets'
                    slot_coverage(
                        tm, [b for b in batches if b["target_id"] == target], since, as_of
                    )
                tc = effective(config, p["provider"], p["product"], target)
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
            member["inputs"] = product_inputs(ps, p["pricing"], cfg, p["kind"], up, values)
            member["observed"] = m["last_observed"]
            previous_rows = [
                r for r in raw if since - timedelta(hours=hours) <= r["bucket_start"] < since
            ]
            before = since - timedelta(hours=hours)
            prev_m = timed(
                previous_rows,
                hours,
                minimum,
                [d for d in slot_runs if before <= d["scheduled_for"] < since],
                slot_minutes,
            )
            if scheduled:
                slot_coverage(prev_m, batches, since - timedelta(hours=hours), since)
            w = {
                "period": {"start": since, "end": as_of, "hours": hours, "boundary": "[start,end)"},
                "readiness": None,  # once scored
                "metrics": m,
                # Kept for older readers; identical to metrics (every request counts).
                "benchmark": m,
                "latency": latency_detail(
                    timed_rows,
                    factors,
                    reference,
                    minimum,
                    excluded,
                    [
                        c
                        for c in references
                        if since.date().isoformat() <= c["day"] <= as_of.date().isoformat()
                    ],
                )
                | {"target": timed_target},
                # Pulses (and load waves) that ran during a census, and the clean ones.
                "census_overlap": census_split(
                    rows, slots_now, minimum, slot_minutes, timed_target
                ),
                "pool": ps,
                "uptime": up,
                **kept,  # "breakdowns", or a reused list (incremental.py)
                "targets": targets,
                "scores": {},
                "evidence": values,
                "evidence_sources": [
                    {
                        k: e[k]
                        for k in ("metric", "source", "period_start", "period_end", "valid_until")
                    }
                    for e in ev + price_rows
                ],
                "unavailable": {k: v for k, v in UNAVAILABLE.items() if k not in values},
                "series": sparse(
                    series(
                        rows,
                        seen,
                        since,
                        as_of,
                        hours,
                        outages=down,
                        tested=monitored_groups[p["product_id"]] if scheduled else (),
                        latency_target=timed_target,
                        minimum=minimum,
                        factors=factors,
                        excluded=excluded,
                    ),
                    every=scheduled or hours != 24,  # only daily groups' 24h series are sparse
                ),
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
                        r["counters"]["completed"]
                        for r in rows
                        if (r["test"] or "unscheduled") == name
                    )
                    for name in ("census", "pulse", "load", "unscheduled")
                },
            }
            w["series"] = source.ips(key, hours, w["series"])  # incremental: counted IPs
            p["windows"][label] = w
            sets[(key, label)] = set(ips)
            fleet_groups[(p["cohort"], label)].append((p, rows, seen, member))
    step = _Build(
        runs=runs,
        config=config,
        method_id=method_id,
        as_of=as_of,
        build_id=build_id,
        release=release,
        events=events,
        traffic=traffic,
        minimum=minimum,
        slot_minutes=slot_minutes,
        timed_target=timed_target,
        groups=groups,
        fleet_groups=fleet_groups,
        bucket_groups=bucket_groups,
        by_product=(scheduled_groups, pulse_groups, outage_groups, tested_groups, monitored_groups),
        flagged_batches=flagged_batches,
    )
    fleets, overlaps = _fleet_rows(step, sets)
    # Full: the counts of these IP sets; incremental: its IP counts across groups (fleetsql.py).
    fleets, overlaps = source.cohorts(fleets, overlaps, fleet_groups, as_of)
    return _payload(step, fleets, overlaps)


def _fleet_rows(step, sets):
    """Score and rank every window of each peer group of build(), set its overlap with its
    peers, and return the fleet entries (with the fleet totals) and the overlap pairs. sets are
    the IPs of each (group key, window label)."""
    config, as_of, fleet_groups = step.config, step.as_of, step.fleet_groups
    minimum, slot_minutes, method_id = step.minimum, step.slot_minutes, step.method_id
    overlaps = []
    fleets = []
    for (cohort, label), members in fleet_groups.items():
        # A cohort holds one kind of product, so its members are each other's peers.
        score_window(members, label, config, as_of, method_id)
        fleets.append(fleet_entry(cohort, label, members, minimum, slot_minutes))
        for p, *_ in members:
            own = sets[((p["product_id"], cohort), label)]
            peers = [
                q
                for q, *_ in members
                if q["provider"] != p["provider"] and sets[((q["product_id"], cohort), label)]
            ]
            shared = set().union(*(sets[((q["product_id"], cohort), label)] for q in peers)) & own
            # Uniqueness is relative to the measured cohort. With an observed
            # pool and no peers, the empty peer union shares none of our IPs.
            # An empty own pool still has no denominator and remains unavailable.
            if own:
                p["windows"][label]["overlap"] = {
                    "exclusive_share": 1 - len(shared) / len(own),
                    "shared_share": len(shared) / len(own),
                    "compared_products": len(peers),
                }
        for (a, *_), (b, *_) in combinations(members, 2):
            if a["provider"] == b["provider"]:
                continue
            sa = sets[((a["product_id"], cohort), label)]
            sb = sets[((b["product_id"], cohort), label)]
            if sa and sb:
                overlaps.append(
                    {
                        "cohort": cohort,
                        "window": label,
                        "product_a": a["product_id"],
                        "product_b": b["product_id"],
                        "shared_ips": len(sa & sb),
                        "ips_a": len(sa),
                        "ips_b": len(sb),
                    }
                )
        for profile in config["profiles"]:
            rank([p["windows"][label]["scores"][profile] for p, *_ in members])
        for target in {t for p, *_ in members for t in p["windows"][label]["targets"]}:
            rank(
                [
                    p["windows"][label]["targets"][target]["score"]
                    for p, *_ in members
                    if target in p["windows"][label]["targets"]
                ]
            )
    fleets += fleet_totals(fleet_groups, as_of, minimum, slot_minutes, step.timed_target)
    return fleets, overlaps


def _payload(step, fleets, overlaps):
    """The payload build() returns: its products, fleet entries and overlap pairs, the
    networks, the release, targets, events, traffic and the unscheduled runs."""
    as_of, config, groups = step.as_of, step.config, step.groups
    return {
        "schema_version": SCHEMA_VERSION,
        "implementation_id": IMPLEMENTATION_ID,
        "snapshot_id": str(step.build_id),
        "as_of": as_of,
        "generated_at": datetime.now(UTC),
        "methodology": {"id": step.method_id, "settings": config},
        "readiness": (
            "ready"
            if groups and all(p["windows"]["30d"]["readiness"] == "ready" for p in groups.values())
            else "partial"
            if any(p["windows"]["30d"]["metrics"]["completed"] for p in groups.values())
            else "unavailable"
        ),
        "periods": {
            label: {"start": as_of - timedelta(hours=h), "end": as_of, "hours": h}
            for label, h in (("24h", 24), ("7d", 168), ("30d", 720))
        },
        "resolution_seconds": 300,
        "refresh_seconds": config["refresh_seconds"],
        "products": list(groups.values()),
        "fleet": fleets,
        "overlap": overlaps,
        # Payload v4: one summary per network over all its groups (network.py, contract K9).
        "networks": network.summaries(
            step.fleet_groups,
            step.bucket_groups,
            step.by_product,
            step.flagged_batches,
            as_of,
            config,
        ),
        **snapshot_fields(step.release),
        "targets": [
            {
                "id": t.id,
                "label": t.id.replace("generic_", "Generic ")
                .replace("https", "HTTPS")
                .replace("http", "HTTP"),
                "url": t.build_request().url,
                "version": t.version,
            }
            for t in registry.all()
        ],
        "events": list(step.events),
        "traffic": step.traffic
        or {
            "period": {"start": as_of - timedelta(hours=720), "end": as_of},
            "by_test": {
                test: {"runs": 0, "requests": 0, "bytes_up": 0, "bytes_down": 0}
                for test in ("census", "pulse", "load")
            },
        },
        # Census and pulse runs are summarized by census, uptime, events and traffic instead.
        "runs": [
            {
                "run_id": r["run_id"],
                "product_id": r["product_id"],
                "provider": r["provider"],
                "product": r["name"],
                "role": r["role"],
                "target": r["scope"]["target"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                **{
                    k: r["summary"].get(k)
                    for k in ("completed", "successes", "unique_ips", "latency_ms", "total_errors")
                },
            }
            for r in step.runs
            if r.get("batch_id") is None
        ],
    }
