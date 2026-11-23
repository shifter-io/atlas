"""Offline calculation over facts, followed by immutable, atomic publication."""

from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .. import detect, no_exit_answers, timing, traffic, verdicts
from ..storage import connect
from ..targets import registry
from . import (
    browser,
    implementation,
    incremental,
    netlists,
    network,
    parallel_measure,
    policy,
    product_windows,
    rollups,
    run_lock,
    score_history,
    standing,
    target_scores,
)
from .fleet import fleet_entry, fleet_totals
from .inputs import (
    BucketRow,
    GeoGroups,
    RunFields,
    cutoff,
    group_by_product,
    light_load,
    previous_products,
    tested_by_product,
    under_load_run,
)
from .parallel_reads import Prefetcher, temp_written
from .parts import previous as previous_payload
from .parts import read as stored_payload
from .parts import write_api_head
from .policy import (
    browser_difficulty,
    browser_min_requests,
    effective,
    hosting_usage_types,
    http_difficulty,
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
# The calculation's modules (and detect, traffic, browser, verdicts, no_exit_answers): a change to
# the code of any changes the id, so the build fingerprint, and of any but
# implementation.NOT_LISTS the list id, so every list signature.
SOURCES = "calculator aggregate policy slim inputs lists list_write incremental hourly fleetsql "
SOURCES += "rollups sqlwindow poolfig network network_scores scheduled scores window_scoring "
SOURCES += "hourly_windows product_windows target_scores batch value_rank value_all fleet_verdicts"
SOURCES += " latency_days latency_store latency_carry score_history rollup_days carried_lists"
SOURCES += " card_success latency_basket standing"
IMPLEMENTATION_ID, LISTS_ID = implementation.ids(
    [Path(__file__).with_name(f"{name}.py") for name in SOURCES.split()]
    + [Path(m.__file__) for m in (detect, traffic, browser, verdicts, no_exit_answers)]
)
# A run's role label, when no calculation_roles row assigns one, follows its batch kind. Roles
# are display labels: every request counts for every metric whatever its role.
BATCH_ROLES = {"census": "pool_sampling", "pulse": "monitor", "load": "benchmark"} | browser.ROLES
# build()'s scheduled inputs (scheduled_inputs()) and the value of each one an Inputs lacks.
# counting: the spans in which each network's country counted (verdicts.spans()); without it
# (None) no uptime is clipped to them. verdicts: each network's latest (verdicts.latest()).
SCHEDULED = (
    dict.fromkeys(("pulse_minutes", "traffic", "counting", "verdicts"))
    | dict.fromkeys("overlaps references geo pulses census outages tested events".split(), ())
    | {"countries": {}}
)


@dataclass(frozen=True)
class _Build:
    """What build() hands its steps: its inputs, its settings and its groups (by_product:
    build()'s scheduled group, pulse batch runs, outages, tested countries, monitored spans,
    counting spans and latest verdicts (both None without country_verdicts) of each product, as
    network.summaries() reads them).
    product_windows.measure() fills fleet_groups and sets (the IPs of each (group key, window
    label))."""

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
    geo_groups: object
    country_names: dict
    threshold: int
    uptime_basis: str
    prev_products: dict
    sets: dict
    measure: object  # parallel_measure.Measure, or None: in this process
    held: dict  # standing.sources(): the stored windows a network may keep
    reach: frozenset = frozenset()  # the daily tests 24h reaches back to (verdicts.REACH)


@dataclass(frozen=True)
class _Groups:
    """build()'s inputs grouped (_group_inputs()): the product groups by key ((product_id,
    cohort)), the runs by run key, each group's bucket rows, its geo rows (inputs.GeoGroups,
    read per product) and each product's scheduled (census/pulse) group."""

    groups: dict
    run_map: dict
    buckets: dict
    geo: dict
    scheduled: dict


def calculate(database, methodology, **options):
    """Calculate (and publish) a build (_calculate()), holding run_lock's locks for the whole run:
    run_lock.Busy, before anything is read, while another calculation runs."""
    with run_lock.hold(database):
        return _calculate(database, methodology, **options)


def _calculate(
    database, methodology, *, as_of=None, publish=False, mode="full", parallel_reads=None
):
    """Calculate (and publish) a build. mode "full" reads every observation of the window into
    Python, the oracle (`calculate --oracle`); "incremental" (`calculate --incremental`) reads
    only the missed windows', over parallel_reads helper connections (else the
    calculate.parallel_reads setting; see parallel_reads.py), or in calculate.workers processes
    (parallel_measure.py); "recount" (`calculate --full`) is the incremental one reusing no
    stored list (incremental.Recount). Each stage prints its timing line (atlas/timing.py).
    publish may instead be a function of (conn, payload), called in the transaction once the
    payload is stored (score_backfill.py reads its scores there and rolls the build back)."""
    if mode not in incremental.MODES:
        raise ValueError(f"calculation mode must be one of {incremental.MODES}, not {mode!r}")
    config, method_id = load(methodology)
    as_of, latest = as_of or datetime.now(UTC), as_of is None
    if isinstance(as_of, str):
        as_of = datetime.fromisoformat(as_of)
    if as_of.tzinfo is None:
        raise ValueError("Measurement cutoff requires a timezone")
    if mode != "full":  # the geo rollups, a day per transaction (rollups.fill())
        with timing.stage("calculate", "rollups"):
            rollups.fill(database, as_of, policy.country_verdicts(config))
    # The helpers close before the transaction ends, on success and on failure; the temp bytes
    # written are printed once every connection is closed.
    with temp_written(database), connect(database) as conn, Prefetcher(conn) as prefetch:
        conn.row_factory = dict_row
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.execute(f"SELECT pg_advisory_xact_lock({rollups.LOCK})")
        prefetch.configure(parallel_reads)  # the calculate.* settings; SET LOCAL work_mem
        # Workers read their missed windows themselves, and fork with no helper thread running.
        measure = parallel_measure.measure(conn, mode)
        as_of = cutoff(conn, as_of, latest)  # on the 5-minute grid of the stored facts
        with timing.stage("calculate", "read", mode=mode):
            ahead = prefetch if measure is None else None
            inputs = incremental.read(conn, config, method_id, as_of, mode, ahead)
        previous = previous_payload(conn)  # only the groups' ids and scores
        if previous:  # and the stored windows a network not measured keeps (standing.py)
            previous["standing"] = standing.sources(conn, as_of, config)
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
            with timing.stage("calculate", "build"):
                payload = build(inputs, config, method_id, build_id, previous, measure)
            with timing.stage("calculate", "store"):
                payload = inputs.store(conn, build_id, payload, config)
        if callable(publish):
            publish(conn, payload)
        elif publish:
            published(conn, payload, previous, as_of)
        with timing.stage("calculate", "prune"):
            prune(conn)
            if publish is True:
                expire(conn)
        return payload


def published(conn, payload, previous, as_of):
    """Make the built payload the current build (calculate(publish=True)); previous is the
    current build's head before it (parts.previous()), whose cutoff may not be later."""
    if previous and datetime.fromisoformat(previous["as_of"]) > as_of:
        raise ValueError("Cannot replace current snapshot with an older cutoff")
    conn.execute(
        "INSERT INTO current_calculation VALUES (true,%s) ON CONFLICT(singleton) "
        "DO UPDATE SET build_id=excluded.build_id",
        (int(payload["snapshot_id"]),),
    )
    netlists.restore(conn)  # a retained build published again (store.publish())
    write_api_head(conn)  # what the API reads of its head
    score_history.record(conn)  # its network scores, for the later builds' sparks
    conn.execute(
        "UPDATE calculation_builds SET published=true WHERE build_id=%s",
        (int(payload["snapshot_id"]),),
    )


def build(inputs, config, method_id, build_id, previous, measure=None):
    """The payload of build build_id at inputs.as_of (incremental.read()'s Inputs: its runs,
    buckets, source, evidence, release and scheduled inputs, SCHEDULED where one is missing): the
    runs grouped by product and cohort (_group_inputs()), each group's windows measured
    (product_windows.py; measure: parallel_measure.Measure's workers) and scored with its peers
    (_fleet_rows()), the previous payload's scores kept as each window's "old"."""
    runs, as_of, scheduled = inputs.runs, inputs.as_of, SCHEDULED | inputs.scheduled
    pulse_minutes, overlaps = scheduled["pulse_minutes"], scheduled["overlaps"]
    slot_minutes = slot_length(pulse_minutes)
    flagged_batches = {(o["batch_id"], o["product_id"]): o["census"] for o in overlaps}
    minimum = slot_settings(config)["min_latency_successes"]
    # Every latency figure comes from this one target.
    timed_target = latency_target(config)
    grouped = _group_inputs(runs, inputs.buckets, scheduled["geo"], config, overlaps, pulse_minutes)
    groups, bucket_groups, scheduled_groups = grouped.groups, grouped.buckets, grouped.scheduled
    # What build() derives from IPs comes from its source (incremental.py): the observations, all
    # aggregated here (the full calculation), or the incremental calculation's.
    source = inputs.source
    if not isinstance(source, incremental.Full):
        source = incremental.Full(source)
    source.group(grouped.run_map)
    pulse_groups = group_by_product(scheduled["pulses"])
    outage_groups = group_by_product(scheduled["outages"])
    tested_groups, monitored_groups = tested_by_product(scheduled["tested"])
    counting = scheduled["counting"]
    counting_groups = None if counting is None else verdicts.by_country(counting)
    sets = {}
    fleet_groups = defaultdict(list)
    step = _Build(
        runs=runs,
        config=config,
        method_id=method_id,
        as_of=as_of,
        build_id=build_id,
        release=inputs.release,
        events=scheduled["events"],
        traffic=scheduled["traffic"],
        minimum=minimum,
        slot_minutes=slot_minutes,
        timed_target=timed_target,
        groups=groups,
        fleet_groups=fleet_groups,
        bucket_groups=bucket_groups,
        by_product=(
            scheduled_groups,
            pulse_groups,
            outage_groups,
            tested_groups,
            monitored_groups,
            counting_groups,
            scheduled["verdicts"],
        ),
        flagged_batches=flagged_batches,
        source=source,
        evidence=inputs.evidence,
        references=scheduled["references"],
        census_groups=group_by_product(scheduled["census"], drop_id=True),
        geo_groups=grouped.geo,
        country_names=scheduled["countries"],
        threshold=uptime_settings(config)["min_successes"],
        uptime_basis=uptime_settings(config)["basis"],
        prev_products=previous_products(previous, as_of),
        sets=sets,
        measure=measure,
        held=(previous or {}).get("standing", {}),
        reach=incremental.reached(runs),
    )
    _measure(step, groups, measure)
    fleets, overlaps = _fleet_rows(step, sets)
    # Browser target scores rank every product group of a window label together (K-BP6); the
    # network summaries the API reads are ranked per kind (network.summaries()).
    groups_of = config.get("target_groups", {})
    difficulty = browser_difficulty(config)
    minimum = browser_min_requests(config)
    target_scores.score(groups.values(), difficulty, groups_of, http_difficulty(config), minimum)
    # Full: the counts of these IP sets; incremental: its IP counts across groups (fleetsql.py).
    fleets, overlaps = source.cohorts(fleets, overlaps, fleet_groups, as_of)
    return _payload(step, fleets, overlaps)


def _measure(step, groups, measure=None):
    """Measure every group (product_windows.measure()), each product's groups together so its
    geo rows are read once (inputs.GeoGroups), here or in measure's workers, and order
    step.fleet_groups as before."""
    by_product = defaultdict(list)
    for key in groups:
        by_product[key[0]].append(key)
    if measure is not None:
        measure(step, groups, list(by_product.values()))
    else:
        for keys in by_product.values():
            for key in keys:
                product_windows.measure(step, key, groups[key])
    _in_group_order(step.fleet_groups, groups)


def _in_group_order(fleet_groups, groups):
    """Order fleet_groups, and each one's members, as measuring the groups in their order fills
    them (build() measures them product by product)."""
    rank = {key: n for n, key in enumerate(groups)}
    order = {}
    for (_, cohort), p in groups.items():
        for label in p["windows"]:
            order.setdefault((cohort, label), len(order))
    found = sorted(fleet_groups.items(), key=lambda item: order[item[0]])
    fleet_groups.clear()
    for k, members in found:
        fleet_groups[k] = sorted(members, key=lambda m: rank[m[0]["product_id"], m[0]["cohort"]])


def _group_inputs(runs, buckets, geo, config, overlaps, pulse_minutes):
    """build()'s inputs by product group ((product_id, cohort)): the groups, the runs by run key,
    the bucket rows of each group (inputs.BucketRow: each bucket with its run's fields), its geo
    rows read per product (inputs.GeoGroups), and each product's scheduled (census/pulse) group.
    Every run gets its group and light flag."""
    slot_seconds = slot_length(pulse_minutes) * 60
    # Pulse and load runs that ran during a census of the same product: the census batch ids.
    flagged_runs = {o["run_key"]: o["census"] for o in overlaps if o["run_key"] is not None}
    # Every latency figure comes from this one target (build()'s timed_target).
    timed_target = latency_target(config)
    groups, run_map, scheduled_groups, scope_groups = _group_runs(
        runs, config, flagged_runs, timed_target, pulse_minutes
    )
    # Each group's rows are made together, so they share their memory pages with no other
    # group's: a worker (parallel_measure.py) copies the pages of its groups only.
    by_group, fields, bucket_groups = defaultdict(list), {}, {}
    for b in buckets:
        by_group[run_map[b["run_key"]]["group"]].append(b)
    for group, found in by_group.items():
        bucket_groups[group] = rows = []
        for b in found:
            if b["run_key"] not in fields:
                r, overlapped = run_map[b["run_key"]], flagged_runs.get(b["run_key"], [])
                fields[b["run_key"]] = _run_fields(r, overlapped, slot_seconds)
            rows.append(BucketRow(b, fields[b["run_key"]]))
    bucket_groups = defaultdict(list, bucket_groups)
    geo_groups = GeoGroups(geo, scheduled_groups, scope_groups)
    return _Groups(groups, run_map, bucket_groups, geo_groups, scheduled_groups)


def _run_fields(r, overlapped, slot_seconds):
    """What run r adds to each of its bucket rows (inputs.RunFields); overlapped: the censuses
    it ran during."""
    test = r.get("test")
    # Pulse and load requests belong to their batch's slot; census and unbatched requests
    # to the slot their bucket starts in.
    at = r.get("slot_at") if test in ("pulse", "load") and r.get("slot_at") else None
    country = r.get("batch_country") or r["scope"]["config"].get("requested_country")
    fields = {
        "target": r["scope"]["target"]["id"],
        "role": r["role"],
        "test": test,
        # Pulses and load waves that ran during a census of the product: requests, not light.
        "during_census": bool(overlapped),
        "census_batches": overlapped,
        "light": r["light"],
        # The latency target's daily tests, census and load runs: latency under load.
        "loaded": r.get("loaded", False),
        "country": country.strip().upper() if country else None,
        "region": r.get("batch_region"),
        "concurrency": r["scope"]["config"].get("concurrency"),
        # A browser run's engine (K-BP3): its figures are never added to another engine's.
        "engine": browser.run_engine(r["scope"]["config"]),
        # A run outside a batch without exit-place rows (a daily test stores none): its requests
        # count towards its requested country (inputs.requested_geo()).
        "placeless": r.get("batch_id") is None and "geo" not in _artifacts(r),
        # A daily test the 24h window reaches back to (verdicts.REACH, aggregate.in_window()).
        "reach": bool(r.get("reach")),
    }
    return RunFields(fields, at, slot_seconds, r["finished_at"])


def _artifacts(r):
    """The artifacts run r's stored summary names ({} when it has no summary)."""
    return (r.get("summary") or {}).get("artifacts") or {}


def run_grouping(config):
    """A function giving a run (with its provider, name, scope and batch_id) its effective
    settings (policy.effective()) and its cohort and criteria (cohort_for()), each made once per
    network or scope and kind: CALC-SCALE-2, made for every run they took ~0.5 ms a run, and the
    30-day window holds millions of runs (a scope's runs share one scope dict: _interned()).
    The values are shared: a caller keeps its own copy of what it changes."""
    settings, cohorts = {}, {}

    def grouping(r):
        name = (r["provider"], r["name"])
        if name not in settings:
            settings[name] = effective(config, *name)
        cfg = settings[name]
        kind, linked = cfg.get("kind", r["name"]), r.get("batch_id") is not None
        key = (kind, linked, None if linked else id(r["scope"]))
        if key not in cohorts:
            cohorts[key] = (r["scope"], cohort_for(r["scope"], kind, linked))
        return cfg, *cohorts[key][1]

    return grouping


def _group_runs(runs, config, flagged_runs, timed_target, pulse_minutes):
    """The product groups of runs by key, the runs by run key, and each product's scheduled
    group and each scope's group; every run gets its group and its light and loaded flags."""
    slot_minutes = slot_length(pulse_minutes)
    groups = {}
    run_map = {}
    scheduled_groups = {}
    scope_groups = {}
    grouping = run_grouping(config)
    for r in runs:
        cfg, cohort, criteria = grouping(r)
        linked = r.get("batch_id") is not None
        key = (r["product_id"], cohort)
        r["group"], r["light"] = key, light_load(r, flagged_runs, timed_target)
        r["loaded"] = under_load_run(r, flagged_runs, timed_target)
        run_map[r["run_key"]] = r
        if linked:
            scheduled_groups[r["product_id"]] = key
        elif "scope_id" in r:
            scope_groups[r["scope_id"]] = key
        if key in groups:
            continue
        cfg, criteria = copy.deepcopy(cfg), copy.deepcopy(criteria)  # the group's own
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


def _networks(step):
    """The payload's networks: one summary per network over all its groups, scored (network.py,
    contract K9), a held one's standing kept (standing.py)."""
    found = network.summaries(
        step.fleet_groups,
        step.bucket_groups,
        step.by_product,
        step.flagged_batches,
        step.as_of,
        step.config,
        step.measure and step.measure.networks,
    )
    return network.scored(found, step.held, step.as_of, step.config)


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
        "networks": _networks(step),
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
            if r.get("batch_id") is None and r.get("counted", True)
        ],
    }
