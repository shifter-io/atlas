"""Offline calculation over facts, followed by immutable, atomic publication."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path

from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb

from .. import detect, traffic
from ..storage import column, connect
from ..targets import registry
from .aggregate import (
    BREAKDOWNS,
    NETWORK,
    PLACE,
    UNAVAILABLE,
    breakdowns,
    metrics,
    outage_list,
    outage_uptime,
    pool,
    pool_lists,
    product_scores,
    series,
    slot_coverage,
    target_score,
    uptime,
)
from .policy import digest, effective, load, scored_roles, uptime_settings
from .store import jsonable, prune, validate_payload

SCHEMA_VERSION = 2
IMPLEMENTATION_ID = hashlib.sha256(
    b"".join(
        Path(__file__).with_name(name).read_bytes()
        for name in ("calculator.py", "aggregate.py", "policy.py")
    )
    + b"".join(Path(m.__file__).read_bytes() for m in (detect, traffic))
).hexdigest()
# Census and pulse runs (linked to a test batch) of a product share one cohort, whatever the
# country, target, node or concurrency: every provider runs them at the same moment, so they are
# comparable as a whole, and the breakdowns, uptime and census summary span all countries.
SCHEDULED = "census_pulse"
# A run's role, when no calculation_roles row assigns one, follows its batch kind.
BATCH_ROLES = {"census": "pool_sampling", "pulse": "monitor"}
EVENTS_IN_SNAPSHOT = 200
# batch_runs.error of runs the orchestrator failed for its own reasons (no node in the region,
# never started, went silent); they do not count against a product's coverage.
INFRA_ERRORS = ("no_node", "not_started", "stale")


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


def calculate(database, methodology, *, as_of=None, publish=False):
    config, method_id = load(methodology)
    as_of = as_of or datetime.now(UTC)
    if isinstance(as_of, str):
        as_of = datetime.fromisoformat(as_of)
    if as_of.tzinfo is None:
        raise ValueError("Measurement cutoff requires a timezone")
    # Existing facts have 5-minute resolution. Exact half-open windows use closed buckets.
    as_of = datetime.fromtimestamp(int(as_of.timestamp() // 300) * 300, UTC)
    start = as_of - timedelta(hours=1440)
    with connect(database) as conn:
        conn.row_factory = dict_row
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.execute("SELECT pg_advisory_xact_lock(817452902)")
        runs = conn.execute(
            "SELECT r.run_key,r.run_id::text,r.summary_sha256,r.summary,r.started_at,"
            "r.finished_at,s.scope,s.scope_id,p.product_id,p.provider,p.name,r.batch_id,"
            "b.kind AS test,coalesce(cr.role,CASE b.kind "
            + " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in BATCH_ROLES.items())
            + " END,'exploratory') AS role FROM test_runs r "
            "JOIN scope_details s USING(scope_id) JOIN products p ON p.product_id=s.product_id "
            "LEFT JOIN test_batches b ON b.batch_id=r.batch_id "
            "LEFT JOIN calculation_roles cr USING(run_key) ORDER BY r.run_key"
        ).fetchall()
        release = conn.execute(
            "SELECT release_id,sha256,metadata FROM dbip_releases ORDER BY release_id DESC LIMIT 1"
        ).fetchone()
        buckets = conn.execute(
            "SELECT bucket_start,run_key,run_bucket_counters(b) AS counters,"
            "outcome_counters AS outcome FROM run_buckets b "
            "WHERE bucket_start>=%s AND bucket_start<%s ORDER BY bucket_start,run_key",
            (start, as_of),
        ).fetchall()
        observations = conn.execute(
            "SELECT o.*,host(a.ip) AS ip,e.matched,e.country_code,e.country,e.region_code,e.region,"
            "e.city,e.latitude,e.longitude,e.isp,e.organization,e.asn,e.asn_organization,"
            "e.connection_type,e.usage_type,m.first_seen AS product_first_seen "
            "FROM ip_observations o JOIN ip_addresses a USING(ip_id) "
            "JOIN test_runs r USING(run_key) JOIN scope_stats s USING(scope_id) "
            "JOIN product_ips m ON m.product_id=s.product_id AND m.ip_id=o.ip_id "
            "LEFT JOIN ip_enrichment_fields e ON e.ip_id=o.ip_id AND e.release_id=%s "
            "WHERE o.bucket_start>=%s AND o.bucket_start<%s "
            "ORDER BY o.run_key,o.bucket_start,o.ip_id",
            (release["release_id"] if release else None, start, as_of),
        ).fetchall()
        evidence_rows = conn.execute(
            "SELECT * FROM metric_evidence ORDER BY evidence_id"
        ).fetchall()
        scheduled = scheduled_inputs(conn, as_of)
        previous_row = conn.execute(
            "SELECT payload FROM calculated_snapshots JOIN current_calculation USING(build_id)"
        ).fetchone()
        previous = previous_row["payload"] if previous_row else None
        # Hash relevant inputs, including role corrections, enrichment and late evidence.
        fingerprint = digest(
            jsonable(
                {
                    "runs": runs,
                    "release": release,
                    "evidence": evidence_rows,
                    "buckets": buckets,
                    "observations": observations,
                    "scheduled": scheduled,
                    "implementation_id": IMPLEMENTATION_ID,
                }
            )
        )
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
            row = conn.execute(
                "SELECT payload FROM calculated_snapshots WHERE build_id=%s",
                (existing["build_id"],),
            ).fetchone()
            payload = row and row["payload"]
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
                **scheduled,
            )
            payload = jsonable(payload)
            validate_payload(payload)
            persist(conn, payload)
            conn.execute(
                "INSERT INTO calculated_snapshots VALUES (%s,%s,%s)",
                (build_id, digest(payload), Jsonb(payload)),
            )
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
        return payload


def scheduled_inputs(conn, as_of):
    """The census/pulse history a v2 payload adds, read in the calculation's snapshot.

    geo: run_geo_buckets of the 30-day window summed per product group (scope_id for runs
    outside a batch, NULL for census/pulse runs), smallest window (hours), location, network and,
    for requests without an exit location, the country the run requested. pulses: finished
    pulse batch runs (uptime). census: the latest finished census per product and country.
    outages: the outages overlapping the 30-day window, as known at the cutoff. tested: per
    product and country, the end of its latest started batch run (uptime's denominator).
    events: the newest test_events before the cutoff. traffic: wire bytes per test kind.
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
        "JOIN enrichment_locations l ON l.location_id=g.location_id "
        "LEFT JOIN test_batches b ON b.batch_id=r.batch_id "
    )
    window = "WHERE g.bucket_start>=%(start)s AND g.bucket_start<%(end)s "
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
        f"histogram(g.histogram_steps,g.histogram_counts::bigint[]) h {window}"
        "GROUP BY 1,2,3,4,5,6,7 ORDER BY 1,2,3,4,5,6,7",
        bounds,
    ):
        key = tuple(row[k] for k in ("scope_id", "product_id", "hours", "location_id"))
        histograms[key + (row["network_id"], row["fallback"])].append([row["ms"], row["n"]])
    locations = {
        r["location_id"]: tuple(r[f] for f in PLACE)
        for r in conn.execute(
            "SELECT * FROM enrichment_locations WHERE location_id=ANY(%s)",
            (sorted({t["location_id"] for t in totals}),),
        )
    }
    networks = {
        r["network_id"]: (r["asn"], r["asn_organization"], r["usage_type"])
        for r in conn.execute(
            "SELECT * FROM enrichment_networks WHERE network_id=ANY(%s)",
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
    # Pulse slots a product was due in and that ended before the cutoff: done runs, and runs
    # that failed for a reason of the provider's. Operator-skipped or killed batches and runs
    # our infrastructure lost (INFRA_ERRORS) are not slots the product could fill.
    pulses = conn.execute(
        "SELECT br.product_id,b.target_id,b.scheduled_for,br.status,br.requests,br.successes "
        "FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "WHERE b.kind='pulse' AND b.status NOT IN ('skipped','killed') "
        "AND b.scheduled_for>=%s AND b.scheduled_for<%s AND br.finished_at<%s "
        "AND (br.status='done' OR (br.status='failed' AND coalesce(br.error,'')<>ALL(%s))) "
        "ORDER BY 3,1,2",
        (start, as_of, as_of, list(INFRA_ERRORS)),
    ).fetchall()
    census = []
    for r in conn.execute(
        "SELECT DISTINCT ON (br.product_id,b.country_code) br.product_id,"
        "b.country_code AS country,rr.slug AS region,b.target_id AS target,br.started_at,"
        "br.finished_at,br.requests,br.successes,br.unique_ips,br.stop_reason,"
        "CASE WHEN br.run_key IS NOT NULL THEN (SELECT count(DISTINCT (l.country_code,"
        "l.region_code,l.region,l.city)) FROM run_geo_buckets g JOIN enrichment_locations l "
        "USING(location_id) WHERE g.run_key=br.run_key AND g.successes>0 AND l.city IS NOT NULL) "
        "END AS cities,CASE WHEN br.run_key IS NOT NULL THEN (SELECT count(DISTINCT n.asn) "
        "FROM run_geo_buckets g JOIN enrichment_networks n USING(network_id) "
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
        "max(least(coalesce(br.finished_at,%(end)s),%(end)s)) AS last "
        "FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "WHERE br.started_at IS NOT NULL AND br.started_at<%(end)s "
        "AND coalesce(br.finished_at,%(end)s)>=%(start)s GROUP BY 1,2 ORDER BY 1,2",
        {"start": start, "end": as_of},
    ).fetchall()
    report = traffic.summary(conn, start=start, end=as_of, finished_before=as_of)
    return {
        "geo": geo,
        "pulses": pulses,
        "census": census,
        "outages": outages,
        "tested": tested,
        "events": detect.list_events(conn, before=as_of, limit=EVENTS_IN_SNAPSHOT),
        "traffic": {
            "period": {"start": start, "end": as_of},
            "by_test": {
                test: {k: t[k] for k in ("runs", "requests", "bytes_up", "bytes_down")}
                for test, t in report["by_test"].items()
            },
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
    geo=(),
    pulses=(),
    census=(),
    outages=(),
    tested=(),
    events=(),
    traffic=None,
):
    groups = {}
    run_map = {}
    bucket_groups = defaultdict(list)
    obs_groups = defaultdict(list)
    geo_groups = defaultdict(list)
    scheduled_groups = {}
    scope_groups = {}
    for r in runs:
        cfg = effective(config, r["provider"], r["name"])
        linked = r.get("batch_id") is not None
        cohort, criteria = cohort_for(r["scope"], cfg.get("kind", r["name"]), linked)
        key = (r["product_id"], cohort)
        r["group"] = key
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
                        "curves",
                    )
                },
            },
        )
    for b in buckets:
        r = run_map[b["run_key"]]
        bucket_groups[r["group"]].append(
            b
            | {
                "target": r["scope"]["target"]["id"],
                "role": r["role"],
                "last_observed": min(r["finished_at"], b["bucket_start"] + timedelta(minutes=5)),
            }
        )
    for o in observations:
        r = run_map[o["run_key"]]
        obs_groups[r["group"]].append(
            o | {"requested_country": r["scope"]["config"].get("requested_country")}
        )
    for g in geo:
        key = (
            scheduled_groups.get(g["product_id"])
            if g["scope_id"] is None
            else scope_groups.get(g["scope_id"])
        )
        if key:
            geo_groups[key].append(g)
    pulse_groups = defaultdict(list)
    for b in pulses:
        pulse_groups[b["product_id"]].append(b)
    outage_groups = defaultdict(list)
    for o in outages:
        outage_groups[o["product_id"]].append(o)
    tested_groups = defaultdict(dict)
    for t in tested:
        tested_groups[t["product_id"]][t["country"].strip()] = t["last"]
    census_groups = defaultdict(list)
    for c in census:
        census_groups[c["product_id"]].append({k: v for k, v in c.items() if k != "product_id"})
    roles = scored_roles(config)
    threshold = uptime_settings(config)["min_successes"]
    prev_products = (
        {p["id"]: p for p in previous["products"]}
        if previous and datetime.fromisoformat(previous["as_of"]) <= as_of
        else {}
    )
    for old_product in prev_products.values():
        for old_window in old_product["windows"].values():
            old_scores = list(old_window["scores"].values()) + [
                t["score"] for t in old_window["targets"].values()
            ]
            for old_score in old_scores:
                old_score.setdefault("methodology_id", previous["methodology"]["id"])
    sets = {}
    fleet_groups = defaultdict(list)
    for key, p in groups.items():
        cfg = effective(config, p["provider"], p["product"])
        raw = bucket_groups[key]
        obs = obs_groups[key]
        p["census"] = sorted(census_groups[p["product_id"]], key=lambda c: c["country"])
        # Uptime is a product fact measured by pulse batches; it belongs to the scheduled cohort.
        scheduled = scheduled_groups.get(p["product_id"]) == key
        batches = pulse_groups[p["product_id"]] if scheduled else []
        down = (
            [o | {"country": o["country"].strip()} for o in outage_groups[p["product_id"]]]
            if scheduled
            else []
        )
        p["outages"] = outage_list(down, as_of - timedelta(hours=720), as_of)
        for hours in config["windows"]:
            label = {24: "24h", 168: "7d", 720: "30d"}[hours]
            since = as_of - timedelta(hours=hours)
            rows = [r for r in raw if since <= r["bucket_start"] < as_of]
            seen = [o for o in obs if since <= o["bucket_start"] < as_of]
            benchmarks = [r for r in rows if r["role"] in roles]
            m = metrics(rows, hours)
            bm = metrics(benchmarks, hours)
            ps, ips = pool(seen, since, hours)
            # Pulse-slot counts stay for reference; the published value is outage time.
            up = uptime(batches, since, as_of, threshold)
            up |= outage_uptime(
                down, tested_groups[p["product_id"]] if scheduled else {}, since, as_of
            )
            m["uptime"] = up["value"]
            if scheduled:
                # Census/pulse cohorts: coverage is the share of pulse slots filled, not of
                # 5-minute buckets (pulses run every 30 minutes, all countries at one tick).
                for target_metrics in (m, bm):
                    slot_coverage(target_metrics, batches, since, as_of)
            dimensions = breakdowns(ips, [g for g in geo_groups[key] if g["hours"] <= hours])
            ps |= pool_lists(dimensions)
            old = prev_products.get(p["id"], {}).get("windows", {}).get(label, {})
            ev = [
                e
                for e in evidence_rows
                if e["product_id"] == p["product_id"]
                and e["cohort"] == p["cohort"]
                and e["period_start"] <= since
                and e["period_end"] >= as_of
                and e["valid_until"] > as_of
            ]
            values = {e["metric"]: e["value"] for e in ev}
            targets = {}
            ids = set(cfg["required_targets"]) | {r["target"] for r in rows}
            for target in sorted(ids):
                tm = metrics([r for r in rows if r["target"] == target], hours)
                # Outages are per product and country, so a target keeps its pulse-slot share.
                tm["uptime"] = uptime(
                    [b for b in batches if b["target_id"] == target], since, as_of, threshold
                )["value"]
                tm["units"] = tm["units"] | {"uptime": "fraction of pulse batches"}
                tb = metrics([r for r in benchmarks if r["target"] == target], hours)
                if scheduled:
                    slots = [b for b in batches if b["target_id"] == target]
                    for target_metrics in (tm, tb):
                        slot_coverage(target_metrics, slots, since, as_of)
                tc = effective(config, p["provider"], p["product"], target)
                score = target_score(tb, tc, benchmark=bool(tb["completed"]))
                retain(
                    score,
                    old.get("targets", {}).get(target, {}).get("score"),
                    as_of,
                    tc["score_valid_hours"],
                    tb["last_observed"],
                    method_id,
                )
                targets[target] = {
                    "metrics": tm,
                    "benchmark": tb,
                    "score": score,
                    "readiness": "measured" if tm["completed"] else "unavailable",
                }
            scores = product_scores(targets, ps, values, cfg, p["kind"], up)
            for profile, score in scores.items():
                retain(
                    score,
                    old.get("scores", {}).get(profile),
                    as_of,
                    cfg["score_valid_hours"],
                    bm["last_observed"],
                    method_id,
                )
                if score["state"] == "ready" and ev:
                    score["valid_until"] = min(
                        score["valid_until"], *(e["valid_until"] for e in ev)
                    )
                if score["state"] == "ready":
                    target_expiries = [
                        targets[t]["score"]["valid_until"] for t in cfg["required_targets"]
                    ]
                    score["valid_until"] = min(score["valid_until"], *target_expiries)
            previous_rows = [
                r for r in raw if since - timedelta(hours=hours) <= r["bucket_start"] < since
            ]
            prev_m = metrics(previous_rows, hours)
            if scheduled:
                slot_coverage(prev_m, batches, since - timedelta(hours=hours), since)
            w = {
                "period": {"start": since, "end": as_of, "hours": hours, "boundary": "[start,end)"},
                "readiness": (
                    "ready"
                    if scores["balanced"]["state"] == "ready"
                    else "partial"
                    if m["completed"]
                    else "unavailable"
                ),
                "metrics": m,
                "benchmark": bm,
                "pool": ps,
                "uptime": up,
                "breakdowns": dimensions,
                "targets": targets,
                "scores": scores,
                "evidence": values,
                "evidence_sources": [
                    {
                        k: e[k]
                        for k in ("metric", "source", "period_start", "period_end", "valid_until")
                    }
                    for e in ev
                ],
                "unavailable": {k: v for k, v in UNAVAILABLE.items() if k not in values},
                "series": series(rows, seen, since, as_of, hours),
                "previous_period": prev_m,
                "success_delta": m["success_rate"] - prev_m["success_rate"]
                if m["success_rate"] is not None and prev_m["success_rate"] is not None
                else None,
                "overlap": {"exclusive_share": None, "shared_share": None, "compared_products": 0},
                "ip_samples": [
                    {
                        k: e["row"].get(k)
                        for k in (
                            "ip",
                            "country_code",
                            "country",
                            "region",
                            "city",
                            "isp",
                            "organization",
                            "asn",
                            "asn_organization",
                            "connection_type",
                            "usage_type",
                        )
                    }
                    | {"observations": e["count"], "first_seen": e["first"], "last_seen": e["last"]}
                    for _, e in sorted(ips.items())[: cfg["sample_limit"]]
                ],
                "roles": {
                    role: metrics([r for r in rows if r["role"] == role], hours)["completed"]
                    for role in ("exploratory", "pool_sampling", "benchmark", "monitor")
                },
            }
            p["windows"][label] = w
            sets[(key, label)] = set(ips)
            fleet_groups[(p["cohort"], label)].append((p, rows, seen))
    overlaps = []
    fleets = []
    for (cohort, label), members in fleet_groups.items():
        all_rows = [r for _, rows, _ in members for r in rows]
        all_seen = [o for _, _, obs in members for o in obs]
        hours = members[0][0]["windows"][label]["period"]["hours"]
        fm = metrics(all_rows, hours)
        fleets.append(
            {
                "cohort": cohort,
                "window": label,
                "products": len(members),
                "providers": len({p["provider"] for p, _, _ in members}),
                "metrics": fm,
                "unique_ips": len({o["ip_id"] for o in all_seen}),
            }
        )
        for p, _, _ in members:
            own = sets[((p["product_id"], cohort), label)]
            peers = [
                q
                for q, _, _ in members
                if q["provider"] != p["provider"] and sets[((q["product_id"], cohort), label)]
            ]
            shared = set().union(*(sets[((q["product_id"], cohort), label)] for q in peers)) & own
            if own and peers:
                p["windows"][label]["overlap"] = {
                    "exclusive_share": 1 - len(shared) / len(own),
                    "shared_share": len(shared) / len(own),
                    "compared_products": len(peers),
                }
        for (a, _, _), (b, _, _) in combinations(members, 2):
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
            rank([p["windows"][label]["scores"][profile] for p, _, _ in members])
        for target in {t for p, _, _ in members for t in p["windows"][label]["targets"]}:
            rank(
                [
                    p["windows"][label]["targets"][target]["score"]
                    for p, _, _ in members
                    if target in p["windows"][label]["targets"]
                ]
            )
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
        "dbip_release": release,
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
                for test in ("census", "pulse")
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


def rank(scores):
    eligible = sorted(
        (s for s in scores if s["state"] in ("ready", "retained")), key=lambda s: -s["score"]
    )
    # A single eligible product isn't a meaningful competitive ranking.
    if len(eligible) < 2:
        return
    for s in eligible:
        s["rank"] = 1 + sum(other["score"] > s["score"] for other in eligible)


CRITERIA = {
    "kind": "text",
    "country": "text",
    "runner_location": "text",
    "concurrency": "integer",
    "timeout_seconds": "number",
    "rate": "number",
    "proxy_protocol": "text",
    "session_policy": "text",
    "ttl_seconds": "integer",
    "parameters": "json",
    "flags": "json",
    "target_variant": "json",
}
# Label fields of each breakdown: its place columns and whether it names an ASN.
FIELDS = {
    "country": (PLACE[:2], False),
    "region": (PLACE[:4], False),
    "city": (PLACE, False),
    "asn": ((), True),
    "country_asn": (PLACE[:2], True),
}
COUNTERS = ("ips", "observations", "completed", "successes", "success_rate", "challenges", "blocks")
LATENCY = ("p50", "p90", "p99")
LABELS = {
    "places": ("place_id", PLACE, ("text",) * 5),
    "asn_labels": ("asn_label_id", ("asn", "name", "usage_type"), ("bigint", "text", "text")),
    "score_reasons": ("reason_id", ("reason",), ("text",)),
}
DERIVED_TABLES = (
    "calculated_score_reasons",
    "calculated_score_components",
    "calculated_scores",
    "calculated_breakdowns",
    "calculated_series",
)


def discard(conn, build_id):
    for table in DERIVED_TABLES:
        conn.execute(
            f"DELETE FROM {table} WHERE result_id IN "
            "(SELECT result_id FROM calculated_windows WHERE build_id=%s)",
            (build_id,),
        )
    for table in ("calculated_windows", "calculated_overlaps", "calculation_builds"):
        conn.execute(f"DELETE FROM {table} WHERE build_id=%s", (build_id,))


def labels(conn, table, wanted):
    """Map label tuples to shared ids, inserting only the missing ones."""
    key, columns, types = LABELS[table]
    cur = conn.cursor(row_factory=tuple_row)
    query = f"SELECT {key},{','.join(columns)} FROM {table} WHERE {columns[0]}=ANY(%s)"
    firsts = sorted({w[0] for w in wanted})
    found = {tuple(row[1:]): row[0] for row in cur.execute(query, (firsts,))}
    missing = sorted(set(wanted) - set(found), key=str)
    if missing:
        cur.execute(
            f"INSERT INTO {table}({','.join(columns)}) SELECT * FROM unnest("
            + ",".join(f"%s::{t}[]" for t in types)
            + ") ON CONFLICT DO NOTHING",
            [list(values) for values in zip(*missing, strict=True)],
        )
        found = {tuple(row[1:]): row[0] for row in cur.execute(query, (firsts,))}
    return found


def store_cohort(conn, cohort, criteria):
    extra = {k: v for k, v in criteria.items() if k not in CRITERIA}
    values = []
    for key, kind in CRITERIA.items():
        try:
            values.append(column(kind, criteria[key]))
        except (KeyError, TypeError):
            values.append(None)
            if key in criteria:
                extra[key] = criteria[key]
    conn.execute(
        "INSERT INTO cohorts(cohort,"
        + ",".join(CRITERIA)
        + ",extra) VALUES ("
        + ",".join(["%s"] * (len(CRITERIA) + 2))
        + ") ON CONFLICT DO NOTHING",
        (cohort, *values, Jsonb(extra) if extra else None),
    )
    rebuilt = conn.execute(
        "SELECT criteria FROM cohort_criteria WHERE cohort=%s", (cohort,)
    ).fetchone()["criteria"]
    if digest(rebuilt) != digest(criteria):
        raise ValueError("Cohort criteria cannot be stored losslessly")


def split_reasons(score, product):
    """Reasons a product score derives from its weighted, not-ready components, plus the rest."""
    if set(score["weights"]) != set(score["components"]):
        raise ValueError("Score weights and components differ")
    derived = [
        c["reason"] or f"{k} not ready"
        for k, c in score["components"].items()
        if product and score["weights"][k] > 0 and c["state"] != "ready"
    ]
    reasons, size = score["reasons"], len(derived)
    start = next(
        (i for i in range(len(reasons) - size + 1) if reasons[i : i + size] == derived), None
    )
    if start is None:
        raise ValueError("Score reasons cannot be rebuilt from their components")
    return [(i - start, r) for i, r in enumerate(reasons[:start])] + [
        (i, r) for i, r in enumerate(reasons[start + size :], 1)
    ]


def persist(conn, payload):
    build_id = int(payload["snapshot_id"])
    for p in payload["products"]:
        store_cohort(conn, p["cohort"], p["criteria"])
    texts = set()  # derived reasons are rebuilt from components, never stored
    for p in payload["products"]:
        for w in p["windows"].values():
            for target, score in [("", s) for s in w["scores"].values()] + [
                (t, v["score"]) for t, v in w["targets"].items()
            ]:
                texts.update(c["reason"] for c in score["components"].values() if c["reason"])
                texts.update(r for _, r in split_reasons(score, not target))
    reason_ids = {k[0]: v for k, v in labels(conn, "score_reasons", {(t,) for t in texts}).items()}
    breakdowns = []
    for p in payload["products"]:
        for w in p["windows"].values():
            for target, m, scores in [
                ("", w["metrics"], w["scores"]),
                *[(t, v["metrics"], {"balanced": v["score"]}) for t, v in w["targets"].items()],
            ]:
                ps = w["pool"] if not target else {}
                result = conn.execute(
                    "INSERT INTO calculated_windows(build_id,product_id,cohort,target,window_hours,"
                    "completed,successes,errors,body_bytes,success_rate,timeout_rate,"
                    "p50,p90,p95,p99,unique_ips,new_ips,"
                    "recurring_share,countries,regions,cities,asns,"
                    "coverage,last_observed,readiness,challenge_rate,block_rate,uptime,"
                    "bytes_up,bytes_down) "
                    "VALUES (" + ",".join(["%s"] * 30) + ") RETURNING result_id",
                    (
                        build_id,
                        p["product_id"],
                        p["cohort"],
                        target,
                        w["period"]["hours"],
                        *[
                            m[k]
                            for k in (
                                "completed",
                                "successes",
                                "errors",
                                "body_bytes",
                                "success_rate",
                                "timeout_rate",
                            )
                        ],
                        *[m["latency_ms"][k] for k in ("p50", "p90", "p95", "p99")],
                        *[
                            ps.get(k)
                            for k in (
                                "unique_ips",
                                "new_ips",
                                "recurring_share",
                                "countries",
                                "regions",
                                "cities",
                                "asns",
                            )
                        ],
                        m["coverage"],
                        m["last_observed"],
                        w["readiness"],
                        *[
                            m[k]
                            for k in (
                                "challenge_rate",
                                "block_rate",
                                "uptime",
                                "bytes_up",
                                "bytes_down",
                            )
                        ],
                    ),
                ).fetchone()["result_id"]
                for profile, s in scores.items():
                    explicit = split_reasons(s, not target)
                    conn.execute(
                        "INSERT INTO calculated_scores(result_id,profile,score,grade,rank,"
                        "previous_rank,state,scored_at,valid_until) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            result,
                            profile,
                            *[
                                s[k]
                                for k in (
                                    "score",
                                    "grade",
                                    "rank",
                                    "previous_rank",
                                    "state",
                                    "scored_at",
                                    "valid_until",
                                )
                            ],
                        ),
                    )
                    with conn.cursor() as cur:
                        cur.executemany(
                            "INSERT INTO calculated_score_components(result_id,profile,component,"
                            "position,state,weight,value,inputs,reason_id) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            [
                                (
                                    result,
                                    profile,
                                    k,
                                    i,
                                    c["state"],
                                    column("number", s["weights"][k]),
                                    column("number", c["value"]),
                                    column("json", c["inputs"]),
                                    reason_ids.get(c["reason"]),
                                )
                                for i, (k, c) in enumerate(s["components"].items())
                            ],
                        )
                        cur.executemany(
                            "INSERT INTO calculated_score_reasons VALUES (%s,%s,%s,%s)",
                            [(result, profile, i, reason_ids[r]) for i, r in explicit],
                        )
                if target:
                    continue
                for name, code in BREAKDOWNS.items():
                    places, named = FIELDS[name]
                    for position, row in enumerate(w["breakdowns"][name]):
                        label = set(row) - set(COUNTERS) - set(LATENCY)
                        if label != set(places) | (set(NETWORK) if named else set()):
                            raise ValueError("Unexpected breakdown label")
                        place = tuple(row.get(f) for f in PLACE) if places else None
                        network = tuple(row[f] for f in NETWORK) if named else None
                        breakdowns.append((result, code, position, place, network, row))
                for row in w["series"]:
                    conn.execute(
                        "INSERT INTO calculated_series VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            result,
                            row["at"],
                            row["completed"],
                            row["successes"],
                            *[row["latency_ms"][k] for k in ("p50", "p90", "p99")],
                            row["unique_ips"],
                        ),
                    )
    places = labels(conn, "places", {b[3] for b in breakdowns if b[3]})
    networks = labels(conn, "asn_labels", {b[4] for b in breakdowns if b[4]})
    with conn.cursor().copy(
        "COPY calculated_breakdowns(result_id,dimension,position,place_id,asn_label_id,"
        "ips,observations,completed,successes,challenges,blocks,p50,p90,p99) FROM STDIN"
    ) as copy:
        for result, code, position, place, network, row in breakdowns:
            copy.write_row(
                (
                    result,
                    code,
                    position,
                    places[place] if place else None,
                    networks[network] if network else None,
                    *[row[k] for k in ("ips", "observations", "completed", "successes")],
                    *[row[k] for k in ("challenges", "blocks", *LATENCY)],
                )
            )
    for o in payload["overlap"]:
        a, b = o["product_a"], o["product_b"]
        na, nb = o["ips_a"], o["ips_b"]
        if a > b:
            a, b, na, nb = b, a, nb, na
        conn.execute(
            "INSERT INTO calculated_overlaps VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                build_id,
                o["cohort"],
                payload["periods"][o["window"]]["hours"],
                a,
                b,
                o["shared_ips"],
                na,
                nb,
            ),
        )
