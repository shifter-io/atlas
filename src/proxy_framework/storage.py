"""Transactional, replay-safe PostgreSQL ingestion of durable measurement artifacts."""

from __future__ import annotations

import gzip
import hashlib
import ipaddress
import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .providers.credentials import read_credentials
from .stats import Histogram

SCHEMA_VERSION = 4

ENRICHMENT_FIELDS = {
    "matched": "boolean",
    "country_code": "text",
    "country": "text",
    "region_code": "text",
    "region": "text",
    "city": "text",
    "latitude": "double precision",
    "longitude": "double precision",
    "isp": "text",
    "organization": "text",
    "asn": "bigint",
    "asn_organization": "text",
    "connection_type": "text",
    "usage_type": "text",
}


def connect(credentials_file):
    credentials = read_credentials(credentials_file, "postgres")
    if set(credentials) - {"host", "port", "dbname", "user", "password", "sslmode"}:
        raise ValueError("Unsupported database connection field")
    return psycopg.connect(**credentials, connect_timeout=5)


def initialize(credentials_file):
    schema = Path(__file__).with_name("schema.sql").read_text()
    checksum = hashlib.sha256(schema.encode()).hexdigest()
    with connect(credentials_file) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(817452901)")
        if conn.execute("SELECT to_regclass('public.schema_versions')").fetchone()[0]:
            versions = conn.execute("SELECT version,checksum FROM schema_versions").fetchall()
            if versions != [(SCHEMA_VERSION, checksum)]:
                raise ValueError("Schema differs; rebuild into a new database before importing")
        conn.execute(schema)
        conn.execute(
            "INSERT INTO schema_versions(version,checksum) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (SCHEMA_VERSION, checksum),
        )
    return {"schema_version": SCHEMA_VERSION}


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Artifact timestamps require a timezone")
    return result.astimezone(UTC)


def _histogram(value):
    if value["format"] != "ceil_ms_v1":
        raise ValueError("Unsupported latency histogram")
    counts = Counter()
    for latency, count in value["bins"]:
        if type(latency) is not int or latency < 0 or type(count) is not int or count < 1:
            raise ValueError("Invalid latency histogram")
        counts[latency] += count
    return Histogram(counts)


def _partition(conn, table, bucket):
    start = bucket.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    name = f"{table}_{start:%Y_%m}"
    conn.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {} PARTITION OF {} FOR VALUES FROM ({}) TO ({})"
        ).format(sql.Identifier(name), sql.Identifier(table), sql.Literal(start), sql.Literal(end))
    )


def ingest(directory, credentials_file):
    directory = Path(directory)
    summary_path = directory / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary_hash = _digest(summary_path)
    if summary["schema_version"] != "1":
        raise ValueError("Unsupported measurement schema")
    requested, started, completed, successes = (
        summary[name] for name in ("requested", "started", "completed", "successes")
    )
    if (
        not all(type(n) is int and n >= 0 for n in (requested, started, completed, successes))
        or requested != started + summary["not_started"]
        or started != completed + summary["cancelled"]
        or completed != successes + summary["total_errors"]
        or sum(summary["errors"].values()) != summary["total_errors"]
    ):
        raise ValueError("Measurement counters do not reconcile")
    histogram = _histogram(summary["latency_histogram"])
    if histogram.counts.total() != successes:
        raise ValueError("Latency histogram does not reconcile")
    source = directory / "ips.jsonl.gz"
    if _digest(source) != summary["artifacts"]["ips"]["sha256"]:
        raise ValueError("IP artifact checksum mismatch")
    config = summary["config"]
    provider, product = config.get("provider"), config.get("product")
    if not provider or not product:
        raise ValueError("Persistence requires provider and product labels")
    scope = {
        "target": summary["target"],
        "config": {
            key: value for key, value in config.items() if key not in ("requests", "target_url")
        },
    }
    scope_hash = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    run_id = summary["run_id"]
    buckets = {_time(b["bucket_start"]): b for b in summary["buckets"]}
    if (
        len(buckets) != len(summary["buckets"])
        or sum(b["completed"] for b in buckets.values()) != completed
        or sum(b["successes"] for b in buckets.values()) != successes
    ):
        raise ValueError("Bucket counters do not reconcile")
    bucket_histogram = Histogram(Counter())
    for bucket in buckets.values():
        part = _histogram(bucket["latency_histogram"])
        if part.counts.total() != bucket["successes"]:
            raise ValueError("Bucket histogram does not reconcile")
        bucket_histogram.merge(part)
    if bucket_histogram.export() != histogram.export():
        raise ValueError("Bucket histograms differ from run histogram")
    with connect(credentials_file) as conn:
        # Serialize ingestion; measurements and website reads run independently.
        # The entire history/enrichment/cache update commits together.
        conn.execute("SELECT pg_advisory_xact_lock(817452901)")
        if conn.execute("SELECT max(version) FROM schema_versions").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("Database schema requires rebuilding before import")
        existing = conn.execute(
            "SELECT summary_sha256,run_key FROM test_runs WHERE run_id=%s", (run_id,)
        ).fetchone()
        if existing and existing[0] != summary_hash:
            raise ValueError("Run ID already exists with different content")
        conn.execute(
            "CREATE TEMP TABLE incoming_ips (bucket timestamptz, ip inet, count bigint, "
            "first_seen timestamptz, last_seen timestamptz, PRIMARY KEY(bucket,ip)) "
            "ON COMMIT DROP"
        )
        with (
            conn.cursor().copy("COPY incoming_ips FROM STDIN") as copy,
            gzip.open(source, "rt") as f,
        ):
            for line in f:
                row = json.loads(line)
                bucket, first, last = (
                    _time(row[k]) for k in ("bucket_start", "first_seen", "last_seen")
                )
                if (
                    row["run_id"] != run_id
                    or row["schema_version"] != "1"
                    or bucket not in buckets
                    or bucket.timestamp() % 300
                    or not bucket <= first <= last < bucket + timedelta(seconds=300)
                    or type(row["count"]) is not int
                    or row["count"] <= 0
                ):
                    raise ValueError("Invalid IP observation")
                copy.write_row(
                    (bucket, str(ipaddress.ip_address(row["ip"])), row["count"], first, last)
                )
        rows, observations, unique = conn.execute(
            "SELECT count(*),coalesce(sum(count),0),count(DISTINCT ip) FROM incoming_ips"
        ).fetchone()
        if (
            rows != summary["artifacts"]["ips"]["rows"]
            or observations != (successes if summary["pool_eligible"] else 0)
            or unique != (summary["unique_ips"] or 0)
        ):
            raise ValueError("IP observations do not reconcile with the run")
        if not existing:
            conn.execute("INSERT INTO providers VALUES (%s) ON CONFLICT DO NOTHING", (provider,))
            conn.execute(
                "INSERT INTO products(provider,name) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (provider, product),
            )
            product_id = conn.execute(
                "SELECT product_id FROM products WHERE provider=%s AND name=%s", (provider, product)
            ).fetchone()[0]
            pool_definition = {
                "target": scope["target"],
                "config": {
                    k: v
                    for k, v in scope["config"].items()
                    if k not in ("concurrency", "timeout_seconds", "requests_per_second")
                },
            }
            pool_hash = hashlib.sha256(
                json.dumps(pool_definition, sort_keys=True).encode()
            ).hexdigest()
            conn.execute(
                "INSERT INTO pool_stats(product_id,pool_hash,definition) VALUES (%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (product_id, pool_hash, Jsonb(pool_definition)),
            )
            pool_id = conn.execute(
                "SELECT pool_id FROM pool_stats WHERE product_id=%s AND pool_hash=%s",
                (product_id, pool_hash),
            ).fetchone()[0]
            scope_key = (product_id, summary["target"]["id"], scope_hash)
            conn.execute(
                "INSERT INTO scope_stats(product_id,target,scope_hash,scope,pool_id) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (*scope_key, Jsonb(scope), pool_id),
            )
            scope_id, metrics = conn.execute(
                "SELECT scope_id,metrics FROM scope_stats WHERE "
                "product_id=%s AND target=%s AND scope_hash=%s",
                scope_key,
            ).fetchone()
            compact = {
                key: value
                for key, value in summary.items()
                if key not in ("buckets", "latency_histogram", "config", "target")
            }
            compact["config_overrides"] = {
                key: value for key, value in config.items() if key not in scope["config"]
            }
            run_key = conn.execute(
                "INSERT INTO test_runs (run_id,scope_id,"
                "started_at,finished_at,status,summary_sha256,summary) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING run_key",
                (
                    run_id,
                    scope_id,
                    _time(summary["started_at"]),
                    _time(summary["finished_at"]),
                    summary["status"],
                    summary_hash,
                    Jsonb(compact),
                ),
            )
            run_key = run_key.fetchone()[0]
            for bucket, counters in buckets.items():
                _partition(conn, "run_buckets", bucket)
                _partition(conn, "ip_observations", bucket)
                conn.execute(
                    "INSERT INTO run_buckets VALUES (%s,%s,%s)", (bucket, run_key, Jsonb(counters))
                )
            conn.execute(
                "INSERT INTO ip_addresses(ip) SELECT DISTINCT ip FROM incoming_ips "
                "ON CONFLICT(ip) DO NOTHING"
            )
            _membership(conn, provider, product_id)
            # Avoid another persisted membership set per performance configuration.
            # The history indexes determine first presence within this exact scope.
            new_ips = conn.execute(
                "SELECT count(*) FROM (SELECT DISTINCT a.ip_id FROM incoming_ips i "
                "JOIN ip_addresses a USING(ip)) n WHERE NOT EXISTS (SELECT 1 FROM "
                "ip_observations o JOIN test_runs r USING(run_key) "
                "WHERE o.ip_id=n.ip_id AND r.scope_id=%s)",
                (scope_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO ip_observations SELECT i.bucket,%s,a.ip_id,i.count,"
                "i.first_seen,i.last_seen FROM incoming_ips i JOIN ip_addresses a USING(ip)",
                (run_key,),
            )
            pool_new = conn.execute(
                "WITH added AS (INSERT INTO pool_ips SELECT DISTINCT %s,a.ip_id FROM "
                "incoming_ips i JOIN ip_addresses a USING(ip) ON CONFLICT DO NOTHING RETURNING 1) "
                "SELECT count(*) FROM added",
                (pool_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE pool_stats SET unique_ips=unique_ips+%s WHERE pool_id=%s",
                (pool_new, pool_id),
            )
            for name in (
                "requested",
                "started",
                "completed",
                "successes",
                "total_errors",
                "cancelled",
                "not_started",
                "response_body_bytes",
                "duration_seconds",
            ):
                metrics[name] = metrics.get(name, 0) + summary[name]
            metrics["runs"] = metrics.get("runs", 0) + 1
            metrics["unique_ips"] = (
                metrics.get("unique_ips", 0) + new_ips if summary["pool_eligible"] else None
            )
            for name in ("errors", "status_codes"):
                counts = Counter(metrics.get(name, {}))
                counts.update(summary[name])
                metrics[name] = dict(counts)
            if "latency_histogram" in metrics:
                histogram.merge(_histogram(metrics["latency_histogram"]))
            metrics["latency_histogram"] = histogram.export()
            metrics["latency_ms"] = histogram.percentiles()
            metrics["success_rate"] = (
                metrics["successes"] / metrics["completed"] if metrics["completed"] else None
            )
            conn.execute(
                "UPDATE scope_stats SET metrics=%s,updated_at=now() WHERE scope_id=%s",
                (Jsonb(metrics), scope_id),
            )
        run_key = existing[1] if existing else run_key
        enriched = _enrich(conn, directory, run_id, run_key, rows)
    return {
        "run_id": run_id,
        "imported": not bool(existing),
        "observation_rows": rows,
        "enrichment_releases_added": enriched,
        "scope_hash": scope_hash,
    }


def _membership(conn, provider, product_id):
    # Only first-ever presence at this provider contributes to lifetime overlap.
    conn.execute(
        "CREATE TEMP TABLE new_provider_ips ON COMMIT DROP AS "
        "SELECT DISTINCT a.ip_id FROM incoming_ips i JOIN ip_addresses a USING(ip) "
        "WHERE NOT EXISTS (SELECT 1 FROM product_ips m JOIN products p USING(product_id) "
        "WHERE m.ip_id=a.ip_id AND p.provider=%s)",
        (provider,),
    )
    peers = conn.execute(
        "SELECT p.provider,count(DISTINCT n.ip_id) FROM new_provider_ips n "
        "JOIN product_ips m USING(ip_id) JOIN products p USING(product_id) "
        "WHERE p.provider<>%s GROUP BY p.provider",
        (provider,),
    ).fetchall()
    for peer, count in peers:
        a, b = sorted((provider, peer))
        conn.execute(
            "INSERT INTO provider_overlap(provider_a,provider_b,shared_ips) "
            "VALUES (%s,%s,%s) ON CONFLICT(provider_a,provider_b) DO UPDATE SET "
            "shared_ips=provider_overlap.shared_ips+excluded.shared_ips,updated_at=now()",
            (a, b, count),
        )
    conn.execute(
        "INSERT INTO product_ips SELECT %s,a.ip_id,min(i.first_seen),max(i.last_seen),"
        "sum(i.count),1 FROM incoming_ips i JOIN ip_addresses a USING(ip) GROUP BY a.ip_id "
        "ON CONFLICT(product_id,ip_id) DO UPDATE SET "
        "first_seen=least(product_ips.first_seen,excluded.first_seen),"
        "last_seen=greatest(product_ips.last_seen,excluded.last_seen),"
        "observation_count=product_ips.observation_count+excluded.observation_count,"
        "run_count=product_ips.run_count+1",
        (product_id,),
    )


def _enrich(conn, directory, run_id, run_key, expected_rows):
    added = 0
    for report_path in sorted(directory.glob("enrichment-*.json")):
        report = json.loads(report_path.read_text())
        release = report["release"]["sha256"]
        if (
            not re.fullmatch(r"[a-f0-9]{64}", release)
            or report["run_id"] != run_id
            or report["file"] != f"enriched-{release}.jsonl.gz"
            or report["rows"] != expected_rows
        ):
            raise ValueError("Invalid enrichment manifest")
        artifact = directory / report["file"]
        checksum = _digest(artifact)
        conn.execute(
            "INSERT INTO dbip_releases(sha256,metadata) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (release, Jsonb(report["release"])),
        )
        release_id = conn.execute(
            "SELECT release_id FROM dbip_releases WHERE sha256=%s", (release,)
        ).fetchone()[0]
        previous = conn.execute(
            "SELECT artifact_sha256 FROM run_enrichments WHERE run_key=%s AND release_id=%s",
            (run_key, release_id),
        ).fetchone()
        if previous:
            if previous[0] != checksum:
                raise ValueError("Previously imported enrichment has changed")
            continue
        conn.execute(
            "CREATE TEMP TABLE incoming_geo (bucket timestamptz, ip inet, count bigint, "
            "attributes jsonb, PRIMARY KEY(bucket,ip)) ON COMMIT DROP"
        )
        with (
            conn.cursor().copy("COPY incoming_geo FROM STDIN") as copy,
            gzip.open(artifact, "rt") as f,
        ):
            for line in f:
                row = json.loads(line)
                if row["run_id"] != run_id or row["dbip_release"] != release:
                    raise ValueError("Enrichment row belongs to another run or release")
                copy.write_row(
                    (
                        _time(row["bucket_start"]),
                        str(ipaddress.ip_address(row["ip"])),
                        row["count"],
                        Jsonb(row["enrichment"]),
                    )
                )
        count, matched = conn.execute(
            "SELECT count(*),count(i.ip) FROM incoming_geo g LEFT JOIN incoming_ips i "
            "ON (g.bucket,g.ip,g.count)=(i.bucket,i.ip,i.count)"
        ).fetchone()
        if count != expected_rows or matched != expected_rows:
            raise ValueError("Enrichment does not match IP observations")
        if conn.execute(
            "SELECT 1 FROM incoming_geo GROUP BY ip HAVING count(DISTINCT attributes)>1 LIMIT 1"
        ).fetchone():
            raise ValueError("Conflicting enrichment for the same IP")
        # Cast and compare the same typed values used on insert; missing and null
        # known fields both map to SQL NULL. Unknown attributes remain lossless.
        values = [f"(g.attributes->>'{key}')::{kind}" for key, kind in ENRICHMENT_FIELDS.items()]
        extras = (
            "g.attributes - ARRAY["
            + ",".join(f"'{key}'" for key in ENRICHMENT_FIELDS)
            + "]::text[]"
        )
        comparisons = [
            f"e.{key} IS DISTINCT FROM {value}"
            for key, value in zip(ENRICHMENT_FIELDS, values, strict=True)
        ]
        comparisons.append(f"e.extra_attributes IS DISTINCT FROM ({extras})")
        if conn.execute(
            "SELECT 1 FROM incoming_geo g JOIN ip_addresses a USING(ip) "
            "JOIN ip_enrichment_fields e USING(ip_id) WHERE e.release_id=%s AND ("
            + " OR ".join(comparisons)
            + ") LIMIT 1",
            (release_id,),
        ).fetchone():
            raise ValueError("Enrichment release conflicts with prior data")
        dimensions = {
            "enrichment_locations": (
                "location_id",
                (
                    "country_code",
                    "country",
                    "region_code",
                    "region",
                    "city",
                    "latitude",
                    "longitude",
                ),
            ),
            "enrichment_networks": (
                "network_id",
                ("isp", "organization", "asn", "asn_organization", "connection_type", "usage_type"),
            ),
        }
        joins = []
        for table, (identity, fields) in dimensions.items():
            columns = ",".join(fields)
            expressions = [f"(g.attributes->>'{f}')::{ENRICHMENT_FIELDS[f]}" for f in fields]
            conn.execute(
                f"INSERT INTO {table} ({columns}) SELECT DISTINCT "
                + ",".join(expressions)
                + " FROM incoming_geo g ON CONFLICT DO NOTHING"
            )
            # A hashable NULL-safe tuple comparison avoids a nested scan for every IP.
            # Values remain typed columns; these JSON arrays are transient join expressions.
            dimension_tuple = "jsonb_build_array(" + ",".join(f"d.{f}" for f in fields) + ")"
            incoming_tuple = "jsonb_build_array(" + ",".join(expressions) + ")"
            joins.append(
                f"JOIN LATERAL (SELECT {identity} FROM {table} d WHERE "
                f"{dimension_tuple}={incoming_tuple}) {identity} ON true"
            )
        conn.execute(
            "INSERT INTO ip_enrichment(ip_id,release_id,location_id,network_id,matched,"
            "extra_attributes) SELECT DISTINCT a.ip_id,%s,location_id.location_id,"
            "network_id.network_id,(g.attributes->>'matched')::boolean,nullif(("
            + extras
            + "),'{}'::jsonb) FROM incoming_geo g JOIN ip_addresses a USING(ip) "
            + " ".join(joins)
            + " ON CONFLICT DO NOTHING",
            (release_id,),
        )
        countries = {
            country: {"requests": int(requests), "unique_ips": unique}
            for country, requests, unique in conn.execute(
                "SELECT coalesce(attributes->>'country_code','Unknown'),sum(count),"
                "count(DISTINCT ip) FROM incoming_geo GROUP BY 1 ORDER BY 1"
            )
        }
        conn.execute(
            "INSERT INTO run_enrichments VALUES (%s,%s,%s,%s)",
            (run_key, release_id, checksum, Jsonb(countries)),
        )
        conn.execute("DROP TABLE incoming_geo")
        added += 1
    return added


def status(credentials_file):
    with connect(credentials_file) as conn:
        counts = {}
        for table in (
            "test_runs",
            "ip_addresses",
            "product_ips",
            "provider_overlap",
            "ip_observations",
            "run_buckets",
            "dbip_releases",
            "ip_enrichment",
            "run_enrichments",
            "scope_stats",
        ):
            counts[table] = conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()[0]
        return {
            "counts": counts,
            "cached_scopes": [
                {
                    "provider": row[0],
                    "product": row[1],
                    "target": row[2],
                    "scope_hash": row[3],
                    "metrics": row[4],
                }
                for row in conn.execute(
                    "SELECT p.provider,p.name,s.target,s.scope_hash,s.metrics "
                    "FROM scope_stats s JOIN products p USING(product_id)"
                )
            ],
            "cached_pools": [
                {"provider": p, "product": product, "pool_hash": digest, "unique_ips": count}
                for p, product, digest, count in conn.execute(
                    "SELECT p.provider,p.name,s.pool_hash,s.unique_ips FROM pool_stats s "
                    "JOIN products p USING(product_id) ORDER BY p.provider,p.name,s.pool_hash"
                )
            ],
            "provider_overlaps": [
                {"provider_a": a, "provider_b": b, "shared_ips": count, "window": "lifetime"}
                for a, b, count in conn.execute(
                    "SELECT provider_a,provider_b,shared_ips FROM provider_overlap "
                    "ORDER BY provider_a,provider_b"
                )
            ],
        }


def read_summary(run_id, credentials_file):
    """Reconstruct the complete artifact-shaped summary from authoritative DB rows."""
    with connect(credentials_file) as conn:
        row = conn.execute(
            "SELECT stored_run_summary(run_key) FROM test_runs WHERE run_id=%s", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return row[0]
