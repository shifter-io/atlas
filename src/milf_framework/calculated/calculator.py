"""Offline calculation over facts, followed by immutable, atomic publication."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..storage import connect
from ..targets import registry
from .aggregate import UNAVAILABLE, metrics, pool, product_scores, series, target_score
from .policy import digest, effective, load
from .store import jsonable, validate_payload

IMPLEMENTATION_ID = hashlib.sha256(
    b"".join(
        Path(__file__).with_name(name).read_bytes()
        for name in ("calculator.py", "aggregate.py", "policy.py")
    )
).hexdigest()


def cohort_for(scope, kind):
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
            "r.finished_at,s.scope,p.product_id,p.provider,p.name,"
            "coalesce(cr.role,'exploratory') AS role FROM test_runs r "
            "JOIN scope_stats s USING(scope_id) JOIN products p USING(product_id) "
            "LEFT JOIN calculation_roles cr USING(run_key) ORDER BY r.run_key"
        ).fetchall()
        release = conn.execute(
            "SELECT release_id,sha256,metadata FROM dbip_releases ORDER BY release_id DESC LIMIT 1"
        ).fetchone()
        buckets = conn.execute(
            "SELECT * FROM run_buckets WHERE bucket_start>=%s AND bucket_start<%s "
            "ORDER BY bucket_start,run_key",
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
        if existing:
            payload = conn.execute(
                "SELECT payload FROM calculated_snapshots WHERE build_id=%s",
                (existing["build_id"],),
            ).fetchone()["payload"]
        else:
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
):
    groups = {}
    run_map = {}
    bucket_groups = defaultdict(list)
    obs_groups = defaultdict(list)
    for r in runs:
        cfg = effective(config, r["provider"], r["name"])
        cohort, criteria = cohort_for(r["scope"], cfg.get("kind", r["name"]))
        key = (r["product_id"], cohort)
        r["group"] = key
        run_map[r["run_key"]] = r
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
        for hours in config["windows"]:
            label = {24: "24h", 168: "7d", 720: "30d"}[hours]
            since = as_of - timedelta(hours=hours)
            rows = [r for r in raw if since <= r["bucket_start"] < as_of]
            seen = [o for o in obs if since <= o["bucket_start"] < as_of]
            benchmarks = [r for r in rows if r["role"] == "benchmark"]
            m = metrics(rows, hours)
            bm = metrics(benchmarks, hours)
            ps, ips = pool(seen, since, hours)
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
                tb = metrics([r for r in benchmarks if r["target"] == target], hours)
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
            scores = product_scores(targets, ps, values, cfg, p["kind"])
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
        "schema_version": 1,
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


def persist(conn, payload):
    build_id = int(payload["snapshot_id"])
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
                    "coverage,last_observed,metadata) "
                    "VALUES (" + ",".join(["%s"] * 25) + ") RETURNING result_id",
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
                        Jsonb({"criteria": p["criteria"], "readiness": w["readiness"]}),
                    ),
                ).fetchone()["result_id"]
                for profile, s in scores.items():
                    conn.execute(
                        "INSERT INTO calculated_scores VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
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
                            Jsonb(s["components"]),
                            Jsonb(s["reasons"]),
                        ),
                    )
                if target:
                    continue
                for dimension, rows in (
                    ("geography", ps["geo"]),
                    ("country", ps["countries_breakdown"]),
                    ("asn", ps["asns_breakdown"]),
                ):
                    for row in rows:
                        labels = {k: v for k, v in row.items() if k not in ("ips", "observations")}
                        conn.execute(
                            "INSERT INTO calculated_breakdowns VALUES (%s,%s,%s,%s,%s,%s)",
                            (
                                result,
                                dimension,
                                digest(labels),
                                row["ips"],
                                row["observations"],
                                Jsonb(labels),
                            ),
                        )
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
