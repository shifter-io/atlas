"""Offline calculation over facts, followed by immutable, atomic publication."""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .. import detect, traffic
from ..storage import connect
from ..targets import registry
from . import browser, incremental
from .aggregate import LATENCY as PERCENTILES
from .aggregate import (
    NOT_LATENCY_TARGET,
    PLACE,
    PRICES,
    UNAVAILABLE,
    census_overlaps,
    census_split,
    daily_average,
    floor_time,
    latency_detail,
    load_factors,
    metrics,
    outage_list,
    outage_uptime,
    peer_scores,
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
from .lists import CRITERIA
from .parts import previous as previous_payload
from .parts import read as stored_payload
from .parts import sparse
from .policy import (
    digest,
    effective,
    hosting_usage_types,
    latency_target,
    load,
    slot_settings,
    uptime_settings,
)
from .releases import snapshot_fields
from .store import discard, expire, prune

SCHEMA_VERSION = 3
# The calculation's modules (and detect, traffic, browser): a change to any changes the id, so the
# build fingerprint and every list signature.
SOURCES = "calculator aggregate policy slim inputs lists incremental hourly fleetsql rollups "
SOURCES += "sqlwindow poolfig"
IMPLEMENTATION_ID = hashlib.sha256(
    b"".join(Path(__file__).with_name(f"{name}.py").read_bytes() for name in SOURCES.split())
    + b"".join(Path(m.__file__).read_bytes() for m in (detect, traffic, browser))
).hexdigest()
# Census and pulse runs (linked to a test batch) of a product share one cohort, whatever the
# country, target, node or concurrency: every provider runs them at the same moment, so they are
# comparable as a whole, and the breakdowns, uptime and census summary span all countries.
SCHEDULED = "census_pulse"
# A run's role label, when no calculation_roles row assigns one, follows its batch kind. Roles
# are display labels: every request counts for every metric whatever its role.
BATCH_ROLES = {"census": "pool_sampling", "pulse": "monitor", "load": "benchmark"} | browser.ROLES
EVENTS_IN_SNAPSHOT = 200
# The resolution of stored buckets: a time slot is a whole number of them (and of the pulse
# interval), so no stored request is ever placed in the wrong slot.
BUCKET_MINUTES = 5


def cohort_for(scope, kind, scheduled=False):
    if scheduled:
        criteria = dict.fromkeys(CRITERIA) | {"kind": kind, "schedule": SCHEDULED}
        return digest(criteria)[:20], criteria
    c = scope["config"]
    adapter = c.get("provider_config") or {}
    # Exclude provider implementation credentials/gateways; include all semantic parameters.
    # Custom URLs/version changes form distinct cohorts, never silently join the default target.
    target = scope["target"]
    known = registry.get(target["id"])
    variant = (
        None
        if (
            target["url"] == known.build_request().url
            and str(target["version"]) == str(known.version)
        )
        else target
    )
    criteria = {
        "kind": kind,
        "country": c.get("requested_country"),
        "runner_location": c.get("runner_location"),
        "concurrency": c["concurrency"],
        "timeout_seconds": c["timeout_seconds"],
        "rate": c.get("requests_per_second"),
        "proxy_protocol": c.get("proxy_protocol"),
        "session_policy": adapter.get("session_policy", "unspecified"),
        "parameters": adapter.get("parameters", {}),
        "flags": adapter.get("extra_flags", {}),
        "ttl_seconds": adapter.get("effective_ttl_seconds"),
        "target_variant": variant,
    }
    return digest(criteria)[:20], criteria


def retain(score, previous, as_of, valid_hours, observed, method_id=None):
    score["previous_rank"] = previous.get("rank") if previous else None
    score["methodology_id"] = method_id
    expires = (observed or as_of) + timedelta(hours=valid_hours)
    if score["score"] is not None and expires <= as_of:
        score.update(score=None, grade=None, state="expired", valid_until=expires)
        score.setdefault("reasons", []).append("Required measurement freshness deadline expired")
    if score["score"] is not None:
        # Validity follows the last measurement, not the time a calculation was rerun.
        score["scored_at"] = as_of
        score["valid_until"] = expires
    elif previous and previous["score"] is not None:
        score.update({k: previous[k] for k in ("score", "grade", "scored_at", "valid_until")})
        score["previous_components"] = previous.get("previous_components", previous["components"])
        score["state"] = (
            "retained" if datetime.fromisoformat(previous["valid_until"]) > as_of else "expired"
        )
    if previous and score["state"] in ("retained", "expired"):
        score["methodology_id"] = previous.get("methodology_id", method_id)
        if score["state"] == "retained" and score["methodology_id"] != method_id:
            score["state"] = "historical"
    return score


def calculate(database, methodology, *, as_of=None, publish=False, mode="full"):
    """Calculate (and publish) a build. mode "full" reads every observation of the window, the
    oracle; "incremental" (the CLI's default) reads only the missed windows' (incremental.py)."""
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


def scheduled_inputs(conn, as_of, timed_target="generic_http"):
    """The census/pulse history a v2 payload adds, read in the calculation's snapshot.

    geo: run_geo_buckets of the 30-day window summed per product group (scope_id for runs
    outside a batch, NULL for census/pulse runs), smallest window (hours), location, network and,
    for requests without an exit location, the country the run requested. pulses: finished
    pulse batch runs (uptime). census: the latest finished census per product and country.
    outages: the outages overlapping the 30-day window, as known at the cutoff. tested: per
    product and country, the spans of started census or pulse runs (uptime's denominator
    and per-bucket monitoring evidence). events: the newest test_events before the cutoff.
    traffic: wire bytes per test kind. pulse_minutes: the cohort's pulse interval, the shortest
    pulse_interval_minutes of pulse-enabled targets (else the shortest gap between pulse slots, else
    None); slot_length() turns it into the time slot. overlaps: the pulse and load runs that ran
    while a census of the same product was running (census_overlaps()), from stored run start
    and finish times (the ingested run's, else the orchestrator's); their histograms stay out of
    the breakdowns' (light-load) latency, which is the latency target's (timed_target) only.
    """
    start = as_of - timedelta(hours=720)
    bounds = {
        "start": start,
        "end": as_of,
        "d1": as_of - timedelta(hours=24),
        "d7": as_of - timedelta(hours=168),
    }
    keys = (
        "CASE WHEN r.batch_id IS NULL THEN r.scope_id END AS scope_id,s.product_id,"
        "CASE WHEN g.bucket_start>=%(d1)s THEN 24 WHEN g.bucket_start>=%(d7)s THEN 168 "
        "ELSE 720 END AS hours,g.location_id,g.network_id,CASE WHEN l.country_code IS NULL "
        "THEN coalesce(b.country_code,upper(ps.requested_country)) END AS fallback"
    )
    source = (
        "FROM run_geo_buckets g JOIN test_runs r USING(run_key) "
        "JOIN scope_stats s ON s.scope_id=r.scope_id JOIN pool_stats ps ON ps.pool_id=s.pool_id "
        "JOIN enrichment_location_fields l ON l.location_id=g.location_id "
        "LEFT JOIN test_batches b ON b.batch_id=r.batch_id "
    )
    window = f"WHERE g.bucket_start>=%(start)s AND g.bucket_start<%(end)s AND {browser.NOT_BROWSER}"
    # Census runs of every product (a census still running at the cutoff ends there), and the
    # pulse and load runs they may overlap; a day of margin covers runs that began earlier.
    times = (
        "coalesce(r.started_at,br.started_at) AS started_at,"
        "least(coalesce(r.finished_at,br.finished_at,%(end)s),%(end)s) AS finished_at "
        "FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "LEFT JOIN test_runs r ON r.run_key=br.run_key WHERE br.started_at IS NOT NULL "
        "AND br.started_at<%(end)s AND coalesce(br.finished_at,%(end)s)>=%(margin)s "
    )
    bounds["margin"] = start - timedelta(days=1)
    census_runs = conn.execute(
        f"SELECT br.product_id,br.batch_id,{times}AND b.kind='census' ORDER BY 1,2", bounds
    ).fetchall()
    tested_runs = conn.execute(
        f"SELECT br.product_id,br.batch_id,br.run_key,{times}AND b.kind IN ('pulse','load') "
        "ORDER BY 1,2",
        bounds,
    ).fetchall()
    overlaps = census_overlaps(census_runs, tested_runs)
    bounds["flagged"] = sorted(o["run_key"] for o in overlaps if o["run_key"] is not None)
    # Breakdown latency is light-load latency of the latency target: its pulses and runs outside
    # a batch, never census or load runs (census concurrency), nor pulses that ran during a
    # census. Counters keep every request of every target.
    bounds["latency_target"] = timed_target
    light = (
        "AND (b.kind IS NULL OR b.kind='pulse') AND NOT g.run_key=ANY(%(flagged)s) "
        "AND s.target=%(latency_target)s "
    )
    totals = conn.execute(
        f"SELECT {keys},sum(g.completed)::bigint AS completed,"
        "sum(g.successes)::bigint AS successes,sum(g.challenges)::bigint AS challenges,"
        f"sum(g.blocks)::bigint AS blocks {source}{window}"
        "GROUP BY 1,2,3,4,5,6 ORDER BY 1,2,3,4,5,6",
        bounds,
    ).fetchall()
    histograms = defaultdict(list)
    for row in conn.execute(
        f"SELECT {keys},h.ms,sum(h.n)::bigint AS n {source}CROSS JOIN LATERAL "
        f"histogram(g.histogram_steps,g.histogram_counts::bigint[]) h {window}{light}"
        "GROUP BY 1,2,3,4,5,6,7 ORDER BY 1,2,3,4,5,6,7",
        bounds,
    ):
        key = tuple(row[k] for k in ("scope_id", "product_id", "hours", "location_id"))
        histograms[key + (row["network_id"], row["fallback"])].append([row["ms"], row["n"]])
    locations = {
        r["location_id"]: tuple(r[f] for f in PLACE)
        for r in conn.execute(
            "SELECT * FROM enrichment_location_fields WHERE location_id=ANY(%s)",
            (sorted({t["location_id"] for t in totals}),),
        )
    }
    networks = {
        r["network_id"]: (r["asn"], r["asn_organization"], r["usage_type"])
        for r in conn.execute(
            "SELECT * FROM enrichment_network_fields WHERE network_id=ANY(%s)",
            (sorted({t["network_id"] for t in totals}),),
        )
    }
    names = {
        r["country_code"]: r["name"]
        for r in conn.execute("SELECT country_code,name FROM countries")
    }
    geo = []
    for t in totals:
        key = tuple(
            t[k]
            for k in ("scope_id", "product_id", "hours", "location_id", "network_id", "fallback")
        )
        code = t["fallback"]
        geo.append(
            {
                "scope_id": t["scope_id"],
                "product_id": t["product_id"],
                "hours": t["hours"],
                "location": locations[t["location_id"]],
                "network": networks[t["network_id"]],
                "fallback": (code, names.get(code))
                if code and re.fullmatch("[A-Z]{2}", code)
                else None,
                **{k: t[k] for k in ("completed", "successes", "challenges", "blocks")},
                "histogram": histograms[key],
            }
        )
    # Pulse slots a product was due in and that ended before the cutoff: its done runs. A run
    # is only ever 'failed' by our side (no node, never started, went silent, the node shut
    # down or erred, the upload was rejected), so those never count against the provider; a
    # provider that is down shows as a done run whose requests failed. Operator-skipped or
    # killed batches are not slots either, and load batches never are.
    pulses = conn.execute(
        "SELECT br.product_id,br.batch_id,b.target_id,b.scheduled_for,"
        "trim(b.country_code) AS country,br.status,br.requests,br.successes,br.run_key "
        "FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "WHERE b.kind='pulse' AND b.status NOT IN ('skipped','killed') "
        "AND b.scheduled_for>=%s AND b.scheduled_for<%s AND br.finished_at<%s "
        "AND br.status='done' ORDER BY 3,1,2",
        (start, as_of, as_of),
    ).fetchall()
    census = []
    for r in conn.execute(
        "SELECT DISTINCT ON (br.product_id,b.country_code) br.product_id,"
        "b.country_code AS country,rr.slug AS region,b.target_id AS target,br.started_at,"
        "br.finished_at,br.requests,br.successes,br.unique_ips,br.stop_reason,"
        "CASE WHEN br.run_key IS NOT NULL THEN (SELECT count(DISTINCT (l.country_code,"
        "l.region_code,l.region,l.city)) FROM run_geo_buckets g JOIN enrichment_location_fields l "
        "USING(location_id) WHERE g.run_key=br.run_key AND g.successes>0 AND l.city IS NOT NULL) "
        "END AS cities,CASE WHEN br.run_key IS NOT NULL THEN (SELECT count(DISTINCT n.asn) "
        "FROM run_geo_buckets g JOIN enrichment_network_fields n USING(network_id) "
        "WHERE g.run_key=br.run_key AND g.successes>0) END AS asns "
        "FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "JOIN runner_regions rr ON rr.region_id=b.region_id "
        "WHERE b.kind='census' AND br.status='done' AND br.finished_at<%s "
        "ORDER BY br.product_id,b.country_code,br.finished_at DESC,b.batch_id DESC",
        (as_of,),
    ):
        started = r.pop("started_at")
        r["duration_s"] = (
            (r["finished_at"] - started).total_seconds() if started and r["finished_at"] else None
        )
        census.append(r)
    # An outage closed after the cutoff was still open as of it.
    outages = conn.execute(
        "SELECT o.outage_id,o.product_id,o.country_code AS country,r.slug AS region,o.source,"
        "o.started_at,CASE WHEN o.ended_at<=%(end)s THEN o.ended_at END AS ended_at "
        "FROM outages o LEFT JOIN runner_regions r ON r.region_id=o.region_id "
        "WHERE o.started_at<%(end)s AND (o.ended_at IS NULL OR o.ended_at>%(start)s) "
        "ORDER BY o.product_id,o.started_at,o.outage_id",
        {"start": start, "end": as_of},
    ).fetchall()
    tested = conn.execute(
        "SELECT br.product_id,b.country_code AS country,"
        "br.started_at,least(coalesce(br.finished_at,%(end)s),%(end)s) AS last "
        "FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "WHERE b.kind NOT IN ('load','browser') AND br.started_at IS NOT NULL "
        "AND br.started_at<%(end)s AND coalesce(br.finished_at,%(end)s)>=%(start)s "
        "ORDER BY 1,2,br.started_at",
        {"start": start, "end": as_of},
    ).fetchall()
    pulse_minutes = conn.execute(
        "SELECT coalesce((SELECT min(pulse_interval_minutes) FROM targets WHERE pulse_enabled),"
        "(SELECT min(extract(epoch FROM d))/60 FROM (SELECT scheduled_for-lag(scheduled_for) "
        "OVER (ORDER BY scheduled_for) AS d FROM (SELECT DISTINCT scheduled_for FROM "
        "test_batches WHERE kind='pulse' AND scheduled_for>=%s AND scheduled_for<%s) s) g "
        "WHERE d>interval '0')) AS minutes",
        (start, as_of),
    ).fetchone()["minutes"]
    report = traffic.summary(conn, start=start, end=as_of, finished_before=as_of)
    return {
        "pulse_minutes": float(pulse_minutes) if pulse_minutes is not None else None,
        "overlaps": overlaps,
        # The daily reference country of every runner region (the load waves' choice basis).
        "references": conn.execute(
            "SELECT l.day::text AS day,r.slug AS region,trim(l.country_code) AS country,l.basis,"
            "l.p50_ms,"
            "l.successes,l.candidates FROM load_references l JOIN runner_regions r "
            "USING(region_id) WHERE l.day>=%s AND l.day<=%s ORDER BY 1,2",
            (start.date(), as_of.date()),
        ).fetchall(),
        "geo": geo,
        "pulses": pulses,
        "census": census,
        "outages": outages,
        "tested": tested,
        "events": detect.list_events(conn, before=as_of, limit=EVENTS_IN_SNAPSHOT),
        "traffic": {
            "period": {"start": start, "end": as_of},
            "by_test": traffic.snapshot(report["by_test"]),
        },
    }


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
        # Done pulse runs in their time slots (time_slots() adds the observed live counters of
        # one whose artifact was never ingested).
        slot_runs = [
            {
                "slot": floor_time(b["scheduled_for"], slot_seconds),
                "scheduled_for": b["scheduled_for"],
                "target": b["target_id"],
                "requests": b["requests"],
                "successes": b["successes"],
                "counted": b.get("run_key") is not None,
                "during_census": bool(flagged_batches.get((b.get("batch_id"), b["product_id"]))),
                "census_batches": flagged_batches.get((b.get("batch_id"), b["product_id"]), []),
            }
            for b in batches
        ]
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
            hosting = hosting_usage_types(cfg)
            ps, ips, lists, kept = source.window(key, seen, since, hours, hosting, located)
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
                    tm["latency_ms"] = dict.fromkeys(PERCENTILES)
                    tm["latency_under_load_ms"] = dict.fromkeys(PERCENTILES)
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
    fleets += fleet_totals(fleet_groups, as_of, minimum, slot_minutes, timed_target)
    # Full: the counts of these IP sets; incremental: its IP counts across groups (fleetsql.py).
    fleets, overlaps = source.cohorts(fleets, overlaps, fleet_groups, as_of)
    return {
        "schema_version": SCHEMA_VERSION,
        "implementation_id": IMPLEMENTATION_ID,
        "snapshot_id": str(build_id),
        "as_of": as_of,
        "generated_at": datetime.now(UTC),
        "methodology": {"id": method_id, "settings": config},
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
        **snapshot_fields(release),
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
        "events": list(events),
        "traffic": traffic
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
            for r in runs
            if r.get("batch_id") is None
        ],
    }


def slot_length(pulse_minutes):
    """Minutes in a time slot: the pulse interval, made a whole multiple of the 5-minute stored
    bucket (the least common multiple: 30 stays 30; staging's 2 becomes 10), so a slot always
    holds whole pulse intervals and whole buckets and a census request is never placed in a
    slot it did not run in. Without a pulse interval, the 5-minute bucket."""
    if not pulse_minutes:
        return BUCKET_MINUTES
    return math.lcm(max(1, round(pulse_minutes)), BUCKET_MINUTES)


def pricing(evidence_rows, product_id, cohort, as_of):
    """The product's list prices in effect at as_of (USD per GB of the pool that is tested).

    Prices are effective-dated metric_evidence (price_per_gb_lowest, price_per_gb_entry): per
    metric the newest record for the product (its cohort or "*") that started by as_of and is
    still valid. lowest_per_gb scores value; entry_per_gb is the smallest plan's rate.
    Returns (the published pricing, the evidence rows it used).
    """
    chosen = {}
    for e in sorted(
        evidence_rows, key=lambda e: (e["period_start"], e["valid_until"], e.get("evidence_id", 0))
    ):
        if (
            e["metric"] in PRICES
            and e["product_id"] == product_id
            and e["cohort"] in (cohort, "*")
            and e["period_start"] <= as_of < e["valid_until"]
        ):
            chosen[e["metric"]] = e
    return {
        "currency": "USD",
        **{
            name: chosen[metric]["value"] if metric in chosen else None
            for metric, name in PRICES.items()
        },
        "effective_from": max((e["period_start"] for e in chosen.values()), default=None),
        "valid_until": min((e["valid_until"] for e in chosen.values()), default=None),
        "sources": sorted({e["source"] for e in chosen.values()}),
    }, [chosen[m] for m in PRICES if m in chosen]


def fresh(observed, as_of, valid_hours):
    """Whether measurements last observed then are recent enough for a new score (retain())."""
    return (observed or as_of) + timedelta(hours=valid_hours) > as_of


def score_window(members, label, config, as_of, method_id):
    """Score one window of a peer group by ratio to the leader, then retain and cap validity.

    members are (product, rows, observations, member) of one cohort; member holds the raw inputs
    peer_scores() needs and what retain() needs: the previous window ("old"), the evidence used
    and the observation times.
    """
    results = peer_scores([m for *_, m in members], config)
    for (p, *_, member), result in zip(members, results, strict=True):
        w = p["windows"][label]
        old = member["old"]
        for target, score in result["targets"].items():
            raw = member["targets"][target]
            retain(
                score,
                old.get("targets", {}).get(target, {}).get("score"),
                as_of,
                raw["valid_hours"],
                raw["observed"],
                method_id,
            )
            w["targets"][target]["score"] = score
        cfg = member["config"]
        for profile, score in result["scores"].items():
            retain(
                score,
                old.get("scores", {}).get(profile),
                as_of,
                cfg["score_valid_hours"],
                member["observed"],
                method_id,
            )
            if score["state"] == "ready" and member["evidence"]:
                score["valid_until"] = min(
                    score["valid_until"], *(e["valid_until"] for e in member["evidence"])
                )
            if score["state"] == "ready":
                target_expiries = [
                    w["targets"][t]["score"]["valid_until"] for t in cfg["required_targets"]
                ]
                score["valid_until"] = min(score["valid_until"], *target_expiries)
        w["scores"] = result["scores"]
        w["readiness"] = (
            "ready"
            if w["scores"]["balanced"]["state"] == "ready"
            else "partial"
            if w["metrics"]["completed"]
            else "unavailable"
        )


def rank(scores):
    eligible = sorted(
        (s for s in scores if s["state"] in ("ready", "retained")), key=lambda s: -s["score"]
    )
    # A single eligible product isn't a meaningful competitive ranking.
    if len(eligible) < 2:
        return
    for s in eligible:
        s["rank"] = 1 + sum(other["score"] > s["score"] for other in eligible)
