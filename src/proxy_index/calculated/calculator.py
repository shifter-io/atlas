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
from . import browser, incremental, network, product_windows
from .aggregate import floor_time
from .fleet import fleet_entry, fleet_totals
from .inputs import cutoff, group_by_product, light_load, previous_products, tested_by_product
from .parts import previous as previous_payload
from .parts import read as stored_payload
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
SOURCES += "sqlwindow poolfig network network_scores scheduled scores window_scoring hourly_windows"
SOURCES += " product_windows"
IMPLEMENTATION_ID = hashlib.sha256(
    b"".join(Path(__file__).with_name(f"{name}.py").read_bytes() for name in SOURCES.split())
    + b"".join(Path(m.__file__).read_bytes() for m in (detect, traffic, browser))
).hexdigest()
# A run's role label, when no calculation_roles row assigns one, follows its batch kind. Roles
# are display labels: every request counts for every metric whatever its role.
BATCH_ROLES = {"census": "pool_sampling", "pulse": "monitor", "load": "benchmark"} | browser.ROLES


@dataclass(frozen=True)
class _Build:
    """What build() hands its steps: its inputs, its settings and its groups (by_product:
    build()'s scheduled group, pulse batch runs, outages, tested countries and monitored spans of
    each product, as network.summaries() reads them). product_windows.measure() fills
    fleet_groups and sets (the IPs of each (group key, window label))."""

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
    source: object
    evidence: list
    references: object
    census_groups: dict
    geo_groups: dict
    threshold: int
    prev_products: dict
    sets: dict


@dataclass(frozen=True)
class _Groups:
    """build()'s inputs grouped (_group_inputs()): the product groups by key ((product_id,
    cohort)), the runs by run key, each group's bucket and geo rows, and each product's
    scheduled (census/pulse) group."""

    groups: dict
    run_map: dict
    buckets: dict
    geo: dict
    scheduled: dict


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
    """The payload of build build_id at as_of: the runs grouped by product and cohort
    (_group_inputs()), each group's windows measured (product_windows.py) and scored with its
    peers (_fleet_rows()), the previous payload's scores kept as each window's "old"."""
    slot_minutes = slot_length(pulse_minutes)
    flagged_batches = {(o["batch_id"], o["product_id"]): o["census"] for o in overlaps}
    minimum = slot_settings(config)["min_latency_successes"]
    # Every latency figure comes from this one target.
    timed_target = latency_target(config)
    grouped = _group_inputs(runs, buckets, geo, config, overlaps, pulse_minutes)
    groups, bucket_groups, scheduled_groups = grouped.groups, grouped.buckets, grouped.scheduled
    # What build() derives from IPs comes from its source (incremental.py): the observations, all
    # aggregated here (the full calculation), or the incremental calculation's.
    source = observations
    if not isinstance(source, incremental.Full):
        source = incremental.Full(observations)
    source.group(grouped.run_map)
    pulse_groups, outage_groups = group_by_product(pulses), group_by_product(outages)
    tested_groups, monitored_groups = tested_by_product(tested)
    sets = {}
    fleet_groups = defaultdict(list)
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
        source=source,
        evidence=evidence_rows,
        references=references,
        census_groups=group_by_product(census, drop_id=True),
        geo_groups=grouped.geo,
        threshold=uptime_settings(config)["min_successes"],
        prev_products=previous_products(previous, as_of),
        sets=sets,
    )
    for key, p in groups.items():
        product_windows.measure(step, key, p)
    fleets, overlaps = _fleet_rows(step, sets)
    # Full: the counts of these IP sets; incremental: its IP counts across groups (fleetsql.py).
    fleets, overlaps = source.cohorts(fleets, overlaps, fleet_groups, as_of)
    return _payload(step, fleets, overlaps)


def _group_inputs(runs, buckets, geo, config, overlaps, pulse_minutes):
    """build()'s inputs by product group ((product_id, cohort)): the groups, the runs by run key,
    the bucket and geo rows of each group, and each product's scheduled (census/pulse) group.
    Every run gets its group and light flag."""
    slot_seconds = slot_length(pulse_minutes) * 60
    # Pulse and load runs that ran during a census of the same product: the census batch ids.
    flagged_runs = {o["run_key"]: o["census"] for o in overlaps if o["run_key"] is not None}
    # Every latency figure comes from this one target (build()'s timed_target).
    timed_target = latency_target(config)
    groups, run_map, scheduled_groups, scope_groups = _group_runs(
        runs, config, flagged_runs, timed_target, pulse_minutes
    )
    bucket_groups = defaultdict(list)
    geo_groups = defaultdict(list)
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
    for g in geo:
        key = (
            scheduled_groups.get(g["product_id"])
            if g["scope_id"] is None
            else scope_groups.get(g["scope_id"])
        )
        if key:
            geo_groups[key].append(g)
    return _Groups(groups, run_map, bucket_groups, geo_groups, scheduled_groups)


def _group_runs(runs, config, flagged_runs, timed_target, pulse_minutes):
    """The product groups of runs by key, the runs by run key, and each product's scheduled
    group and each scope's group; every run gets its group and light flag."""
    slot_minutes = slot_length(pulse_minutes)
    groups = {}
    run_map = {}
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
    return groups, run_map, scheduled_groups, scope_groups


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
