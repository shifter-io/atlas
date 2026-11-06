"""Transactional, replay-safe PostgreSQL ingestion of durable measurement artifacts."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from collections import Counter
from decimal import Decimal
from itertools import accumulate
from pathlib import Path
from types import SimpleNamespace

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from . import names, storage_ingest, storage_migrate
from .partitions import RAW_DAYS as RAW_DAYS  # re-exported for the calculator
from .partitions import partition
from .providers.credentials import read_credentials
from .stats import Histogram
from .storage_ingest import ENRICHMENT_FIELDS as ENRICHMENT_FIELDS
from .storage_ingest import OUTCOME_KEYS as OUTCOME_KEYS
from .storage_ingest import _digest, _time
from .storage_migrate import MIGRATION_CHECKS as MIGRATION_CHECKS
from .storage_migrate import V6_CHECKS as V6_CHECKS

SCHEMA_VERSION = 12
# Only an unmodified v4 to v11 schema (these checksums) is migrated in place; migrate().
V4_CHECKSUM = "0ec5b6579c67bf883dead3e64312899dc6665cdb684ac617f06edb6d9cf1fee5"
V5_CHECKSUM = "2e1d8e89ffcf0748a882e7c4cddfd3e33e86a0024cc757648a4cabb502e64e3b"
V6_CHECKSUM = "1a06ee59c45bcaaaf14b11db107890a94dfc41b9f3b823472f7c8a22c984bc10"
V7_CHECKSUM = "75eb7782f9700e6b57b42d59ef8ffd1f46976d9178432689131c0add5ae5f0a5"
V8_CHECKSUM = "5986ee1ea743a34bf42cb49d3a7d5ded4d0b884bb653cb090e3d4b7f679a4552"
# The v8 schema before the framework rename (one comment differs); databases made then have it.
V8_PRE_RENAME_CHECKSUM = "315af82775eb32324307b33c7f88b18a7fd1a9b443ee1b8cb15d42fa7b1945ca"
V9_CHECKSUM = "bf4c540376649ecf841e5207e2581cda29cba76473761b7e5c27e72cee98b0ee"
V10_CHECKSUM = "9e993488d6062b3a9082bdab5da44d5f1cf20ed849be641a6c6fd41a9ce9b9f9"
V11_CHECKSUM = "e35fda97a06d674a160f277f0807408cbd7435f4eca571ab70773dd20715452c"
MIGRATABLE = {(4, V4_CHECKSUM): 4, (5, V5_CHECKSUM): 5, (6, V6_CHECKSUM): 6, (7, V7_CHECKSUM): 7}
MIGRATABLE |= {(8, V8_CHECKSUM): 8, (8, V8_PRE_RENAME_CHECKSUM): 8, (9, V9_CHECKSUM): 9}
MIGRATABLE |= {(10, V10_CHECKSUM): 10, (11, V11_CHECKSUM): 11}
PERFORMANCE = ("concurrency", "timeout_seconds", "requests_per_second")
# Run-length settings stay out of the scope (and pool) definition; they are kept per run in
# config_overrides. A census (stop_rule unique_ratio, min_sample, stall_seconds, requests null)
# and a pulse of the same product, target and country therefore share one pool.
RUN_LENGTH = (
    "requests",
    "target_url",
    "stop_rule",
    "min_sample",
    "stall_seconds",
    "census_rule_version",
    "census_ratio_basis",
    "retry_http_469",
    "processes",
    "execution_processes",
    "execution_backend",
    "worker_pids",
    "answer_classes",
)
BUCKET_KEYS = (
    "bucket_start",
    "completed",
    "successes",
    "response_body_bytes",
    "errors",
    "status_codes",
    "latency_histogram",
)
GEO_COUNTERS = (
    "completed",
    "successes",
    "challenges",
    "blocks",
    "timeouts",
    "bytes_up",
    "bytes_down",
)
GEO_KEYS = {"bucket_start", "location", "network", "latency_histogram", *GEO_COUNTERS}
LOCATION_FIELDS = (
    "country_code",
    "country",
    "region_code",
    "region",
    "city",
    "latitude",
    "longitude",
)
NETWORK_FIELDS = ("isp", "organization", "asn", "asn_organization", "connection_type", "usage_type")
ADAPTER = {
    "adapter": ("adapter", "text"),
    "version": ("adapter_version", "text"),
    "pool": ("account_pool", "text"),
    "parameters": ("parameters", "json"),
    "session_policy": ("session_policy", "text"),
    "effective_ttl_seconds": ("ttl_seconds", "integer"),
    "extra_flags": ("extra_flags", "json"),
}


def connect(credentials_file):
    credentials = read_credentials(credentials_file, "postgres")
    if set(credentials) - {"host", "port", "dbname", "user", "password", "sslmode"}:
        raise ValueError("Unsupported database connection field")
    return psycopg.connect(**credentials, connect_timeout=5)


def _schema():
    schema = Path(__file__).with_name("schema.sql").read_text()
    return schema, hashlib.sha256(schema.encode()).hexdigest()


def initialize(credentials_file):
    schema, checksum = _schema()
    with connect(credentials_file) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(817452901)")
        if conn.execute("SELECT to_regclass(current_schema||'.schema_versions')").fetchone()[0]:
            versions = conn.execute("SELECT version,checksum FROM schema_versions").fetchall()
            if versions != [(SCHEMA_VERSION, checksum)]:
                raise ValueError(
                    "Schema differs; run db-migrate (v4 to v7) or rebuild into a new database"
                )
        conn.execute(schema)
        conn.execute(
            "INSERT INTO schema_versions(version,checksum) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (SCHEMA_VERSION, checksum),
        )
    return {"schema_version": SCHEMA_VERSION}


def _histogram(value):
    if value["format"] != "ceil_ms_v1":
        raise ValueError("Unsupported latency histogram")
    counts = Counter()
    for latency, count in value["bins"]:
        if (
            type(latency) is not int
            or not 0 <= latency < 2**31
            or type(count) is not int
            or not 0 < count < 2**31
        ):
            raise ValueError("Invalid latency histogram")
        counts[latency] += count
    return Histogram(counts)


def column(kind, value):
    """Return a column value that rebuilds exactly this JSON value, or raise TypeError."""
    if kind == "json" or value is None:
        return None if value is None else Jsonb(value)
    if kind == "text" and type(value) is str:
        return value
    if kind == "integer" and type(value) is int and -(2**31) <= value < 2**31:
        return value
    # numeric keeps 15 and 15.0 apart; floats printed with a positive exponent would not survive.
    if kind == "number" and type(value) in (int, float) and math.isfinite(value):
        if type(value) is int or "e+" not in repr(value):
            return value if type(value) is int else Decimal(repr(value))
    raise TypeError(kind)


def _take(source, key, kind, row, name):
    try:
        row[name] = column(kind, source[key])
        del source[key]
    except (KeyError, TypeError):
        row[name] = None


def _profile(definition, provider, product):
    """Typed pool columns; anything they cannot reproduce stays verbatim in extra."""
    target, config = dict(definition["target"]), dict(definition["config"])
    row = {"target_id": target.pop("id")}
    for key in ("url", "engine", "version"):
        _take(target, key, "text", row, "target_" + key)
    for key, value in (("provider", provider), ("product", product), ("target", row["target_id"])):
        if key in config and config[key] == value:
            del config[key]
    for key in ("requested_country", "runner_location"):
        _take(config, key, "text", row, key)
    # NULL means absent here; an explicit null stays in extra.
    row["proxy_protocol"] = (
        config.pop("proxy_protocol") if type(config.get("proxy_protocol")) is str else None
    )
    adapter = config.get("provider_config")
    typed = {}
    try:
        if set(adapter) != {*ADAPTER, "gateway"} or type(adapter["adapter"]) is not str:
            raise TypeError
        gateway = adapter["gateway"]
        if set(gateway) != {"host", "port"}:
            raise TypeError
        typed["gateway_host"] = column("text", gateway["host"])
        typed["gateway_port"] = column("integer", gateway["port"])
        for key, (name, kind) in ADAPTER.items():
            typed[name] = column(kind, adapter[key])
        del config["provider_config"]
    except (TypeError, AttributeError):
        typed = dict.fromkeys(["gateway_host", "gateway_port", *(n for n, _ in ADAPTER.values())])
    extra = {key: value for key, value in (("config", config), ("target", target)) if value}
    return row | typed | {"extra": Jsonb(extra) if extra else None}


def _count(value):
    return type(value) is int and 0 <= value < 2**63


def _bucket(bucket, counters):
    """Typed column values in run_buckets order: the v5 columns and extra, then the outcomes.

    The six outcome counters are typed only when all are present as counts; then
    outcome_counters is true and run_bucket_counters() emits them. Otherwise any of them that
    appear stay verbatim in extra, so pre-v6 buckets rebuild without them.
    """
    if set(BUCKET_KEYS) - set(counters) or any(
        type(counters[k]) is not int or counters[k] < 0
        for k in ("completed", "successes", "response_body_bytes")
    ):
        raise ValueError("Invalid bucket counters")
    typed = all(_count(counters.get(k)) for k in OUTCOME_KEYS)
    known = (*BUCKET_KEYS, *OUTCOME_KEYS) if typed else BUCKET_KEYS
    extra = {k: v for k, v in counters.items() if k not in known}
    if counters["bucket_start"] != bucket.isoformat(timespec="microseconds"):
        extra["bucket_start"] = counters["bucket_start"]
    histogram = counters["latency_histogram"]
    if set(histogram) != {"format", "bins"}:
        extra["latency_histogram"] = histogram
    return (
        counters["completed"],
        counters["successes"],
        counters["response_body_bytes"],
        Jsonb(counters["errors"]),
        Jsonb(counters["status_codes"]),
        *_steps(histogram["bins"]),
        Jsonb(extra) if extra else None,
        typed,
        *(counters[k] if typed else 0 for k in OUTCOME_KEYS),
    )


def _steps(bins):
    """Millisecond gaps between consecutive bins (the first is absolute) and their counts."""
    ms = [m for m, _ in bins]
    return [b - a for a, b in zip([0, *ms], ms, strict=False)], [n for _, n in bins]


def _add_run_metrics(metrics, summary, attempted, new_ips, histogram):
    """Add one run to a scope's cached metrics; histogram is the merged lifetime one."""
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
        value = attempted if name == "requested" else summary[name]
        metrics[name] = metrics.get(name, 0) + value
    metrics["runs"] = metrics.get("runs", 0) + 1
    metrics["unique_ips"] = (
        metrics.get("unique_ips", 0) + new_ips if summary["pool_eligible"] else None
    )
    for name in ("errors", "status_codes"):
        counts = Counter(metrics.get(name, {}))
        counts.update(summary[name])
        metrics[name] = dict(counts)
    metrics["latency_ms"] = histogram.percentiles()
    metrics["latency_mean_ms"] = histogram.mean()
    metrics["success_rate"] = (
        metrics["successes"] / metrics["completed"] if metrics["completed"] else None
    )


def _dimension(value, fields):
    """Typed column values for a location or network; null (no exit address) is all NULL."""
    if value is None:
        return (None,) * len(fields)
    if not isinstance(value, dict) or set(value) - set(fields):
        raise ValueError("Invalid geo dimension")
    result = []
    for field in fields:
        item = value.get(field)
        kind = ENRICHMENT_FIELDS[field]
        if item is None:
            result.append(None)
        elif kind == "text" and type(item) is str:
            result.append(item)
        elif kind == "bigint" and type(item) is int and -(2**63) <= item < 2**63:
            result.append(item)
        elif kind == "double precision" and type(item) in (int, float) and math.isfinite(item):
            result.append(float(item))
        else:
            raise ValueError("Invalid geo dimension")
    return tuple(result)


def _geo_rows(directory, summary, buckets, bucket_rows):
    """Validated geo.jsonl.gz rows, reconciled with the bucket counters; [] when absent."""
    manifest = summary.get("artifacts", {}).get("geo")
    if manifest is None:
        return []
    name = manifest.get("file")
    if not isinstance(name, str) or Path(name).name != name or not name:
        raise ValueError("Invalid geo artifact")
    path = directory / name
    if _digest(path) != manifest["sha256"]:
        raise ValueError("Geo artifact checksum mismatch")
    rows, seen = [], set()
    totals = {bucket: Counter() for bucket in buckets}
    histograms = {bucket: Histogram(Counter()) for bucket in buckets}
    with gzip.open(path, "rt") as stream:
        for line in stream:
            row = json.loads(line)
            if not isinstance(row, dict) or set(row) != GEO_KEYS:
                raise ValueError("Invalid geo row")
            bucket = _time(row["bucket_start"])
            location = _dimension(row["location"], LOCATION_FIELDS)
            network = _dimension(row["network"], NETWORK_FIELDS)
            counters = [row[k] for k in GEO_COUNTERS]
            histogram = _histogram(row["latency_histogram"])
            if (
                bucket not in buckets
                or not all(_count(n) for n in counters)
                or row["successes"] > row["completed"]
                or histogram.counts.total() != row["successes"]
            ):
                raise ValueError("Invalid geo row")
            if (bucket, location, network) in seen:
                raise ValueError("Duplicate geo bucket")
            seen.add((bucket, location, network))
            totals[bucket].update(dict(zip(GEO_COUNTERS, counters, strict=True)))
            histograms[bucket].merge(histogram)
            rows.append(
                (bucket, *location, *network, *counters, *_steps(histogram.export()["bins"]))
            )
    if len(rows) != manifest["rows"]:
        raise ValueError("Geo artifact row count mismatch")
    for bucket, counters in buckets.items():
        keys = ["completed", "successes"]
        if bucket_rows[bucket][8]:
            keys += [k for k in GEO_COUNTERS if k in OUTCOME_KEYS]
        if any(totals[bucket][k] != counters[k] for k in keys) or (
            histograms[bucket].export() != _histogram(counters["latency_histogram"]).export()
        ):
            raise ValueError("Geo buckets do not reconcile with run buckets")
    return rows


def _store_geo(conn, run_key, rows):
    """Resolve each location and network to its enrichment id and add the geo buckets."""
    if not rows:
        return
    types = ",".join(
        f"{field} {ENRICHMENT_FIELDS[field]}" for field in (*LOCATION_FIELDS, *NETWORK_FIELDS)
    )
    conn.execute(
        f"CREATE TEMP TABLE incoming_geo_buckets (bucket timestamptz, {types}, "
        "completed bigint, successes bigint, challenges bigint, blocks bigint, timeouts bigint, "
        "bytes_up bigint, bytes_down bigint, histogram_steps integer[], "
        "histogram_counts integer[]) ON COMMIT DROP"
    )
    with conn.cursor().copy("COPY incoming_geo_buckets FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)
    for bucket in {row[0] for row in rows}:
        partition(conn, "run_geo_buckets", bucket)
    # The same lists as _enrich: the all-NULL rows are the unknown member.
    joins = names.resolve(conn, "incoming_geo_buckets g", lambda field: f"g.{field}")
    conn.execute(
        "INSERT INTO run_geo_buckets(bucket_start,run_key,location_id,network_id,completed,"
        "successes,challenges,blocks,timeouts,bytes_up,bytes_down,histogram_steps,"
        "histogram_counts) SELECT g.bucket,%s,location_id.location_id,network_id.network_id,"
        "g.completed,g.successes,g.challenges,g.blocks,g.timeouts,g.bytes_up,g.bytes_down,"
        "g.histogram_steps,g.histogram_counts FROM incoming_geo_buckets g " + joins,
        (run_key,),
    )


def _check_bucket_histograms(buckets, histogram):
    """Each bucket's histogram counts its successes, and together they equal the run's."""
    merged = Histogram(Counter())
    for bucket in buckets.values():
        part = _histogram(bucket["latency_histogram"])
        if part.counts.total() != bucket["successes"]:
            raise ValueError("Bucket histogram does not reconcile")
        merged.merge(part)
    if merged.export() != histogram.export():
        raise ValueError("Bucket histograms differ from run histogram")


def _read_run(directory):
    """Read and reconcile a run directory's summary, buckets and geo rows before any write."""
    summary_path = directory / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary_hash = _digest(summary_path)
    if summary["schema_version"] != "1":
        raise ValueError("Unsupported measurement schema")
    requested, started, completed, successes = (
        summary[name] for name in ("requested", "started", "completed", "successes")
    )
    # A census is unbounded; a bounded run also attempted one replacement per rate limit.
    attempted = (
        started + summary["not_started"]
        if requested is None
        else requested + sum(summary.get("rate_limited", {}).values())
    )
    in_country = successes - summary.get("other_country", 0)  # other-country exits: no rows
    if (
        not all(type(n) is int and n >= 0 for n in (attempted, started, completed, successes))
        or attempted != started + summary["not_started"]
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
        "config": {key: value for key, value in config.items() if key not in RUN_LENGTH},
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
    _check_bucket_histograms(buckets, histogram)
    bucket_rows = {bucket: _bucket(bucket, counters) for bucket, counters in buckets.items()}
    if any(key in summary for key in OUTCOME_KEYS):
        if not all(_count(summary.get(key)) for key in OUTCOME_KEYS) or (
            all(row[8] for row in bucket_rows.values())
            and any(sum(buckets[b][key] for b in buckets) != summary[key] for key in OUTCOME_KEYS)
        ):
            raise ValueError("Outcome counters do not reconcile")
    geo_rows = _geo_rows(directory, summary, buckets, bucket_rows)
    return SimpleNamespace(
        summary=summary,
        summary_hash=summary_hash,
        attempted=attempted,
        in_country=in_country,
        histogram=histogram,
        source=source,
        provider=provider,
        product=product,
        scope=scope,
        scope_hash=scope_hash,
        run_id=run_id,
        buckets=buckets,
        bucket_rows=bucket_rows,
        geo_rows=geo_rows,
    )


def _upsert_scope(conn, run, performance):
    """Add the run's provider, product, target, pool and scope where new.

    Returns product_id, pool_id and the scope's (scope_id, metrics, histogram_steps,
    histogram_counts).
    """
    summary, provider, product, scope = run.summary, run.provider, run.product, run.scope
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
        "config": {k: v for k, v in scope["config"].items() if k not in PERFORMANCE},
    }
    pool_hash = hashlib.sha256(json.dumps(pool_definition, sort_keys=True).encode()).hexdigest()
    profile = _profile(pool_definition, provider, product)
    # Pools and scopes reference targets; a target nobody seeded joins unscheduled.
    target = summary["target"]
    conn.execute(
        "INSERT INTO targets(target_id,url,engine,census,pulse_enabled) "
        "VALUES (%s,%s,%s,false,false) ON CONFLICT DO NOTHING",
        (
            profile["target_id"],
            target["url"] if type(target.get("url")) is str else "",
            target["engine"] if type(target.get("engine")) is str else "http",
        ),
    )
    conn.execute(
        "INSERT INTO pool_stats(product_id,pool_hash,"
        + ",".join(profile)
        + ") VALUES ("
        + ",".join(["%s"] * (len(profile) + 2))
        + ") ON CONFLICT DO NOTHING",
        (product_id, pool_hash, *profile.values()),
    )
    pool_id = conn.execute(
        "SELECT pool_id FROM pool_stats WHERE product_id=%s AND pool_hash=%s",
        (product_id, pool_hash),
    ).fetchone()[0]
    scope_key = (product_id, summary["target"]["id"], run.scope_hash)
    conn.execute(
        "INSERT INTO scope_stats(product_id,target,scope_hash,pool_id,"
        + ",".join(PERFORMANCE)
        + ") VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (*scope_key, pool_id, *performance),
    )
    scope_row = conn.execute(
        "SELECT scope_id,metrics,histogram_steps,histogram_counts FROM scope_stats WHERE "
        "product_id=%s AND target=%s AND scope_hash=%s",
        scope_key,
    ).fetchone()
    return product_id, pool_id, scope_row


def ingest(directory, credentials_file, *, batch_id=None, ip_rows=True):
    """Import one finished run directory; replaying the same artifacts is a no-op.

    batch_id links the run to its test_batches row; ip_rows=False (pulse) stores no per-IP rows.
    A census may have requested=null (unbounded); geo.jsonl.gz, if listed, fills run_geo_buckets.
    """
    directory = Path(directory)
    run = _read_run(directory)
    run_id, summary_hash, scope = run.run_id, run.summary_hash, run.scope
    histogram = run.histogram
    try:
        performance = [
            column(kind, scope["config"][key])
            for key, kind in zip(PERFORMANCE, ("integer", "number", "number"), strict=True)
        ]
        if None in performance[:2]:
            raise TypeError
    except (KeyError, TypeError):
        raise ValueError("Unsupported concurrency, timeout or rate setting") from None
    with connect(credentials_file) as conn:
        # Serialize ingestion; measurements and website reads run independently.
        # The entire history/enrichment/cache update commits together.
        conn.execute("SELECT pg_advisory_xact_lock(817452901)")
        if conn.execute("SELECT max(version) FROM schema_versions").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("Database schema requires db-migrate or a rebuild before import")
        existing = conn.execute(
            "SELECT summary_sha256,run_key,batch_id FROM test_runs WHERE run_id=%s", (run_id,)
        ).fetchone()
        if existing and existing[0] != summary_hash:
            raise ValueError("Run ID already exists with different content")
        if existing and batch_id is not None and existing[2] != batch_id:
            if existing[2] is not None:
                raise ValueError("Run already belongs to another batch")
            conn.execute(
                "UPDATE test_runs SET batch_id=%s WHERE run_key=%s", (batch_id, existing[1])
            )
        rows = storage_ingest.load_ips(conn, run, ip_rows)
        if not existing:
            product_id, pool_id, scope_row = _upsert_scope(conn, run, performance)
            scope_id, metrics, histogram_steps, histogram_counts = scope_row
            run_key = storage_ingest.insert_run(conn, run, scope_id, batch_id)
            storage_ingest.insert_buckets(conn, run, run_key)
            _store_geo(conn, run_key, run.geo_rows)
            storage_ingest.check_stored(conn, run, run_key)
            new_ips = _add_addresses(conn, run.provider, product_id, scope_id)
            storage_ingest.add_observations(conn, run_key, pool_id)
            lifetime = zip(accumulate(histogram_steps), histogram_counts, strict=True)
            histogram.merge(Histogram(Counter(dict(lifetime))))
            _add_run_metrics(metrics, run.summary, run.attempted, new_ips, histogram)
            conn.execute(
                "UPDATE scope_stats SET metrics=%s,histogram_steps=%s,histogram_counts=%s,"
                "updated_at=now() WHERE scope_id=%s",
                (Jsonb(metrics), *_steps(histogram.export()["bins"]), scope_id),
            )
        run_key = existing[1] if existing else run_key
        enriched = storage_ingest.enrich(conn, directory, run_id, run_key, rows) if ip_rows else 0
    return {
        "run_id": run_id,
        "run_key": run_key,
        "imported": not bool(existing),
        "observation_rows": rows,
        "enrichment_releases_added": enriched,
        "scope_hash": run.scope_hash,
    }


def _add_addresses(conn, provider, product_id, scope_id):
    """Add the new addresses and product memberships; return the IPs new to this scope."""
    conn.execute(
        "INSERT INTO ip_addresses(ip) SELECT DISTINCT ip FROM incoming_ips "
        "ON CONFLICT(ip) DO NOTHING"
    )
    _membership(conn, provider, product_id)
    # Avoid another persisted membership set per performance configuration.
    # First presence within this exact scope: the anti-join reads via test_runs_scope.
    return conn.execute(
        "SELECT count(*) FROM (SELECT DISTINCT a.ip_id FROM incoming_ips i "
        "JOIN ip_addresses a USING(ip)) n WHERE NOT EXISTS (SELECT 1 FROM "
        "ip_observations o JOIN test_runs r USING(run_key) "
        "WHERE o.ip_id=n.ip_id AND r.scope_id=%s)",
        (scope_id,),
    ).fetchone()[0]


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
            "run_geo_buckets",
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
                    "SELECT provider,product,target,scope_hash,metrics FROM scope_details "
                    "ORDER BY scope_id"
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


def migrate(credentials_file):
    """Migrate an unmodified v4 to v11 database, and its calculated layer, in one transaction.

    v4 runs migrate_v4_v5.sql (and calculated v1 → v2), then migrate_v5_v6.sql to v11_v12.sql in
    turn; v5 to v11 start at the step for their version (v7 and v9 only add a table, v8 moves
    names into the shared lists, v10 adds the batch kind browser, v11 swaps an index). A
    calculated v2 layer then runs calculated/migrate_v2_v3.sql, then calculated v3 → v4
    (availability), v4 → v5 (city/network evidence), v5 → v6 (breakdowns partitioned by build),
    v6 → v7 (names), v7 → v8 (parts), v8 → v9 (lists), v9 → v10 (memo tables); each is verified.
    A v6 or later database with an older calculated layer starts at its step. Every rebuilt pool,
    scope, bucket and run summary, every test batch, location, network and per-IP enrichment, and
    every calculated window, breakdown and score, must equal its original.
    """
    from .calculated import store

    schema, checksum = _schema()
    calculated, calculated_checksum = store.schema()
    with connect(credentials_file) as conn:
        versions, layered, layers = storage_migrate.read_versions(conn)
        current_layer = [(store.SCHEMA_VERSION, calculated_checksum)]
        if versions == [(SCHEMA_VERSION, checksum)]:
            if not layered or layers == current_layer:
                return {"schema_version": SCHEMA_VERSION, "migrated": False}
            start = SCHEMA_VERSION
        else:
            start = storage_migrate.migratable_start(versions, MIGRATABLE)
        upgraded, later = storage_migrate.layer_start(start, layered, layers, current_layer)
        stale = layered and not upgraded
        checks = storage_migrate.migrate_base(
            conn, start, layered, stale and not later, SCHEMA_VERSION
        )
        steps = storage_migrate.migrate_calculated(conn, start, stale, later)
        conn.execute(schema)
        if layered:
            conn.execute(calculated)
        verified = [*storage_migrate.verify(conn, checks, start), *steps]
        storage_migrate.record(
            conn,
            (SCHEMA_VERSION, checksum) if start < SCHEMA_VERSION else None,
            (store.SCHEMA_VERSION, calculated_checksum) if stale else None,
        )
    tables = storage_migrate.compacted(start, layered, upgraded)
    if tables:
        with connect(credentials_file) as conn:
            conn.autocommit = True
            conn.execute("VACUUM (FULL, ANALYZE) " + ",".join(tables))
    return {
        "schema_version": SCHEMA_VERSION,
        "from_schema_version": start,
        "calculated_schema_version": store.SCHEMA_VERSION if layered else None,
        "from_calculated_schema_version": layers[0][0] if layered else None,
        "migrated": True,
        "verified": verified,
    }


def seed(
    credentials_file,
    regions_path=None,
    targets_path=None,
    *,
    countries_path=None,
    reset_times=False,
):
    """Upsert runner_regions, countries and targets from configs/ (proxy-index db-seed).

    Re-seeding moves countries between regions and refreshes names, patterns and target
    definitions, but keeps each country's census_time and enabled flag and each target's schedule
    as operators left them. reset_times puts every census_time back on the staggered default.
    """
    from .regions import load_regions, load_targets

    regions = load_regions(regions_path, countries_path)
    targets = load_targets(targets_path)
    times = regions.census_times()
    with connect(credentials_file) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(817452901)")
        if conn.execute("SELECT max(version) FROM schema_versions").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("Database schema requires db-migrate or a rebuild before seeding")
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO runner_regions(region_id,slug,cloud,pattern) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT(region_id) DO UPDATE SET slug=excluded.slug,cloud=excluded.cloud,"
                "pattern=excluded.pattern",
                [(r.region_id, r.slug, "digitalocean", r.pattern) for r in regions.regions],
            )
            cursor.executemany(
                "INSERT INTO countries(country_code,name,region_id,census_time) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT(country_code) DO UPDATE SET "
                "name=excluded.name,region_id=excluded.region_id,census_time=CASE WHEN %s "
                "THEN excluded.census_time ELSE countries.census_time END",
                [
                    (code, regions.names[code], region.region_id, times[code], reset_times)
                    for region in regions.regions
                    for code in region.countries
                ],
            )
            definition = ("label", "url", "engine", "pass_condition", "timeout_ms", "sort_order")
            schedule = (
                "census",
                "census_concurrency",
                "pulse_enabled",
                "pulse_interval_minutes",
                "pulse_requests",
                "pulse_concurrency",
            )
            columns = ("target_id", *definition, *schedule)
            # Ingest-added targets have no label; their schedule comes from the seed once.
            cursor.executemany(
                "INSERT INTO targets("
                + ",".join(columns)
                + ") VALUES ("
                + ",".join(["%s"] * len(columns))
                + ") ON CONFLICT(target_id) DO UPDATE SET "
                + ",".join(f"{c}=excluded.{c}" for c in definition)
                + ","
                + ",".join(
                    f"{c}=CASE WHEN targets.label IS NULL THEN excluded.{c} ELSE targets.{c} END"
                    for c in schedule
                ),
                [tuple(row[c] for c in columns) for row in targets],
            )
    return {
        "regions": len(regions.regions),
        "countries": len(times),
        "targets": [row["target_id"] for row in targets],
        "census_times_reset": reset_times,
    }
