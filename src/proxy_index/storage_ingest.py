"""The steps of storage.ingest_many: a run's reads before the transaction, and the set-based
statements over the batch's temp tables (storage_batch) inside it."""

from __future__ import annotations

import gzip
import ipaddress
import json
import math
import re
import uuid
from datetime import timedelta
from decimal import Decimal

from psycopg.types.json import Jsonb

from . import names
from .partitions import DAILY, creation, holder
from .storage_batch import many
from .storage_read import (
    ENRICHMENT_FIELDS,
    LOCATION_FIELDS,
    NETWORK_FIELDS,
    OUTCOME_KEYS,
    _digest,
    _time,
)

ADAPTER = {
    "adapter": ("adapter", "text"),
    "version": ("adapter_version", "text"),
    "pool": ("account_pool", "text"),
    "parameters": ("parameters", "json"),
    "session_policy": ("session_policy", "text"),
    "effective_ttl_seconds": ("ttl_seconds", "integer"),
    "extra_flags": ("extra_flags", "json"),
}
BUCKET_COLUMNS = (
    "completed,successes,response_body_bytes,errors,status_codes,histogram_steps,"
    "histogram_counts,extra,outcome_counters," + ",".join(OUTCOME_KEYS)
)


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


def _canonical(value):
    return json.dumps(value, sort_keys=True)


def read_ips(run, ip_rows):
    """The run's IP observation rows, checked and reconciled with its summary; none for a pulse."""
    summary, run_id, buckets, rows = run.summary, run.run_id, run.buckets, []
    with gzip.open(run.source, "rt") as f:
        for line in f if ip_rows else ():
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
            rows.append((bucket, str(ipaddress.ip_address(row["ip"])), row["count"], first, last))
    observations, unique = sum(row[2] for row in rows), len({row[1] for row in rows})
    if ip_rows and (
        len(rows) != summary["artifacts"]["ips"]["rows"]
        or observations != (run.in_country if summary["pool_eligible"] else 0)
        or unique != (summary["unique_ips"] or 0)
    ):
        raise ValueError("IP observations do not reconcile with the run")
    return rows


def run_row(run, batch_id):
    """The test_runs values of a run, with the summary keys its columns cannot rebuild."""
    summary, run_id = run.summary, run.run_id
    compact = {
        key: value
        for key, value in summary.items()
        if key not in ("buckets", "latency_histogram", "config", "target")
    }
    compact["config_overrides"] = {
        key: value for key, value in summary["config"].items() if key not in run.scope["config"]
    }
    started_at, finished_at = _time(summary["started_at"]), _time(summary["finished_at"])
    # Keys the columns reproduce exactly are not stored twice.
    for key, value in (
        ("run_id", str(uuid.UUID(run_id))),
        ("status", summary["status"]),
        ("started_at", started_at.isoformat(timespec="microseconds")),
        ("finished_at", finished_at.isoformat(timespec="microseconds")),
    ):
        if compact[key] == value:
            del compact[key]
    status, summary_hash = summary["status"], run.summary_hash
    return run_id, started_at, finished_at, status, summary_hash, Jsonb(compact), batch_id


def insert_runs(conn):
    """Insert the new runs' test_runs rows in batch order; each gets its run_key."""
    conn.execute(
        "INSERT INTO test_runs (run_id,scope_id,started_at,finished_at,status,summary_sha256,"
        "summary,batch_id) SELECT run_id,scope_id,started_at,finished_at,status,summary_sha256,"
        "summary,batch_id FROM incoming_runs ORDER BY idx"
    )
    conn.execute(
        "UPDATE incoming_runs i SET run_key=t.run_key FROM test_runs t WHERE t.run_id=i.run_id"
    )


def partitions(runs):
    """{(table, moment)} of the daily partitions the runs' buckets, IPs and geo rows need."""
    found = {(t, b) for run in runs for b in run.bucket_rows for t in DAILY[:2]}
    return found | {(DAILY[2], row[0]) for run in runs for row in run.geo_rows}


def insert_buckets(conn, runs, existing):
    """Add the new runs' buckets, creating the daily partitions they, their IPs and geo rows need
    where partition() would: existing holds the names found under the lock."""
    wanted = {(t, *holder(t, moment, existing)) for t, moment in partitions(runs)}
    for table, name, start, end in sorted(wanted):
        if name not in existing:
            for query in creation(table, name, start, end):
                conn.execute(query)
    conn.execute(
        f"INSERT INTO run_buckets(bucket_start,run_key,{BUCKET_COLUMNS}) SELECT b.bucket_start,"
        f"r.run_key,b.{BUCKET_COLUMNS.replace(',', ',b.')} FROM incoming_buckets b "
        "JOIN incoming_runs r USING(idx)"
    )


def store_geo(conn, runs):
    """Resolve each location and network to its enrichment id and add the geo buckets."""
    rows = [(run.idx, *row) for run in runs for row in run.geo_rows]
    if not rows:
        return
    types = ",".join(
        f"{field} {ENRICHMENT_FIELDS[field]}" for field in (*LOCATION_FIELDS, *NETWORK_FIELDS)
    )
    conn.execute(
        f"CREATE TEMP TABLE incoming_geo_buckets (idx integer, bucket timestamptz, {types}, "
        "completed bigint, successes bigint, challenges bigint, blocks bigint, timeouts bigint, "
        "bytes_up bigint, bytes_down bigint, histogram_steps integer[], "
        "histogram_counts integer[]) ON COMMIT DROP"
    )
    many(conn, "INSERT INTO incoming_geo_buckets VALUES ", "", None, rows)
    # The same lists as _enrich: the all-NULL rows are the unknown member.
    joins = names.resolve(conn, "incoming_geo_buckets g", lambda field: f"g.{field}")
    conn.execute(
        "INSERT INTO run_geo_buckets(bucket_start,run_key,location_id,network_id,completed,"
        "successes,challenges,blocks,timeouts,bytes_up,bytes_down,histogram_steps,"
        "histogram_counts) SELECT g.bucket,r.run_key,location_id.location_id,network_id.network_id,"
        "g.completed,g.successes,g.challenges,g.blocks,g.timeouts,g.bytes_up,g.bytes_down,"
        "g.histogram_steps,g.histogram_counts FROM incoming_geo_buckets g "
        "JOIN incoming_runs r USING(idx) " + joins
    )


def check_stored(run, stored):
    """Raise unless the stored rows (stored_run_summary) rebuild the summary exactly."""
    # The run histogram is stored only as bucket bins, exactly as in v4.
    expected = run.summary | {"latency_histogram": run.histogram.export()}
    if _canonical(stored) != _canonical(expected):
        raise ValueError("Run summary cannot be stored losslessly")


def add_observations(conn):
    """Add the new runs' IP observations and count the IPs new to each pool (pool_ips in key
    order, as every insert into a table imports share)."""
    conn.execute(
        "INSERT INTO ip_observations SELECT bucket,run_key,ip_id,count,first_seen,last_seen "
        "FROM incoming_observations"
    )
    conn.execute(
        "WITH added AS (INSERT INTO pool_ips SELECT DISTINCT pool_id,ip_id FROM "
        "incoming_observations ORDER BY pool_id,ip_id ON CONFLICT DO NOTHING RETURNING pool_id) "
        "UPDATE pool_stats p SET unique_ips=p.unique_ips+a.n FROM (SELECT pool_id,count(*) AS n "
        "FROM added GROUP BY pool_id) a WHERE p.pool_id=a.pool_id"
    )


def read_enrichments(run):
    """The run's enrichment reports checked against run.ips: (release, metadata, checksum, rows)."""
    directory, run_id, expected = run.source.parent, run.run_id, len(run.ips)
    observed = {(bucket, ip): count for bucket, ip, count, _, _ in run.ips}
    reports = []
    for report_path in sorted(directory.glob("enrichment-*.json")):
        report = json.loads(report_path.read_text())
        release = report["release"]["sha256"]
        if (
            not re.fullmatch(r"[a-f0-9]{64}", release)
            or report["run_id"] != run_id
            or report["file"] != f"enriched-{release}.jsonl.gz"
            or report["rows"] != expected
        ):
            raise ValueError("Invalid enrichment manifest")
        artifact = directory / report["file"]
        rows = _enrichment_rows(artifact, run_id, release)
        matched = sum(type(n) is int and observed.get((b, ip)) == n for b, ip, n, _ in rows)
        if len(rows) != expected or matched != expected:
            raise ValueError("Enrichment does not match IP observations")
        reports.append((release, Jsonb(report["release"]), _digest(artifact), rows))
    return reports


def _enrichment_rows(artifact, run_id, release):
    """The rows of one enrichment artifact; each must belong to the run and the release."""
    rows = []
    with gzip.open(artifact, "rt") as f:
        for line in f:
            row = json.loads(line)
            if row["run_id"] != run_id or row["dbip_release"] != release:
                raise ValueError("Enrichment row belongs to another run or release")
            ip = str(ipaddress.ip_address(row["ip"]))
            rows.append((_time(row["bucket_start"]), ip, row["count"], Jsonb(row["enrichment"])))
    return rows


def enrich(conn):
    """Add the batch's new enrichment reports; the cursors of their two checks."""
    conn.execute(
        "INSERT INTO dbip_releases(sha256,metadata) SELECT DISTINCT ON(release) release,metadata "
        "FROM incoming_releases ORDER BY release,idx ON CONFLICT DO NOTHING"
    )
    conn.execute(
        "UPDATE incoming_releases i SET release_id=d.release_id FROM dbip_releases d "
        "WHERE d.sha256=i.release"
    )
    conn.execute(
        "UPDATE incoming_releases i SET run_key=r.run_key FROM incoming_runs r WHERE r.idx=i.idx"
    )
    conflicting = conn.execute(
        "SELECT DISTINCT idx FROM incoming_enrichment GROUP BY idx,release,ip "
        "HAVING count(DISTINCT attributes)>1"
    )
    conflicts = _store_enrichment(conn)
    conn.execute(
        "INSERT INTO run_enrichments SELECT r.run_key,r.release_id,r.checksum,coalesce((SELECT "
        "jsonb_object_agg(c.country,jsonb_build_object('requests',c.requests,'unique_ips',c.ips)) "
        "FROM (SELECT coalesce(g.attributes->>'country_code','Unknown') AS country,"
        "sum(g.count) AS requests,count(DISTINCT g.ip) AS ips FROM incoming_enrichment g "
        "WHERE (g.idx,g.release)=(r.idx,r.release) GROUP BY 1) c),'{}') FROM incoming_releases r"
    )
    return conflicting, conflicts


def _store_enrichment(conn):
    """Add incoming_enrichment's per-IP enrichment; the cursor of the check that it agrees with
    any stored for its release, and the batch's reports of one release with each other."""
    # Cast and compare the same typed values used on insert; missing and null
    # known fields both map to SQL NULL. Unknown attributes remain lossless.
    values = [f"(g.attributes->>'{key}')::{kind}" for key, kind in ENRICHMENT_FIELDS.items()]
    extras = (
        "g.attributes - ARRAY[" + ",".join(f"'{key}'" for key in ENRICHMENT_FIELDS) + "]::text[]"
    )
    comparisons = [
        f"e.{key} IS DISTINCT FROM b.{key}" for key in (*ENRICHMENT_FIELDS, "extra_attributes")
    ]
    # Each IP's distinct typed values per release; more than one conflicts inside the batch.
    # Each is looked up by key (LATERAL ... LIMIT 1), whatever the planner knows of the tables.
    typed = [f"{value} AS {key}" for key, value in zip(ENRICHMENT_FIELDS, values, strict=True)]
    conflicts = conn.execute(
        "WITH b AS MATERIALIZED (SELECT DISTINCT a.ip_id,r.release_id,"
        + ",".join(typed)
        + f",({extras}) AS extra_attributes FROM incoming_enrichment g JOIN incoming_releases r "
        "USING(idx,release) JOIN ip_addresses a USING(ip)) SELECT EXISTS (SELECT 1 FROM b, "
        "LATERAL (SELECT 1 FROM ip_enrichment_fields e WHERE (e.ip_id,e.release_id)="
        "(b.ip_id,b.release_id) AND ("
        + " OR ".join(comparisons)
        + ") LIMIT 1) x) OR EXISTS (SELECT 1 FROM b GROUP BY ip_id,release_id HAVING count(*)>1)"
    )
    joins = names.resolve(
        conn, "incoming_enrichment g", lambda f: f"(g.attributes->>'{f}')::{ENRICHMENT_FIELDS[f]}"
    )
    conn.execute(
        "INSERT INTO ip_enrichment(ip_id,release_id,location_id,network_id,matched,"
        "extra_attributes) SELECT DISTINCT a.ip_id,r.release_id,location_id.location_id,"
        "network_id.network_id,(g.attributes->>'matched')::boolean,nullif(("
        + extras
        + "),'{}'::jsonb) FROM incoming_enrichment g JOIN incoming_releases r USING(idx,release) "
        "JOIN ip_addresses a USING(ip) " + joins + " ORDER BY 1,2 ON CONFLICT DO NOTHING"
    )
    return conflicts
