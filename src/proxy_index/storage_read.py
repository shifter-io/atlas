"""The read side of storage: a run directory read and reconciled before any write, and DB reads.

It imports neither storage nor storage_ingest; both import these names back from here.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .providers.credentials import read_credentials
from .stats import Histogram

# Request outcomes and wire bytes (v6 artifacts), per run, bucket and geography bucket.
OUTCOME_KEYS = ("challenges", "blocks", "timeouts", "proxy_errors", "bytes_up", "bytes_down")
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


def connect(credentials_file):
    credentials = read_credentials(credentials_file, "postgres")
    if set(credentials) - {"host", "port", "dbname", "user", "password", "sslmode"}:
        raise ValueError("Unsupported database connection field")
    return psycopg.connect(**credentials, connect_timeout=5)


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
        if (
            type(latency) is not int
            or not 0 <= latency < 2**31
            or type(count) is not int
            or not 0 < count < 2**31
        ):
            raise ValueError("Invalid latency histogram")
        counts[latency] += count
    return Histogram(counts)


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


def _dimension(value, fields):
    """Typed column values for a location or network; null (no exit address) is all NULL."""
    if value is None:
        return (None,) * len(fields)
    if not isinstance(value, dict) or set(value) - set(fields):
        raise ValueError("Invalid geo dimension")
    return tuple(_dimension_item(value.get(field), ENRICHMENT_FIELDS[field]) for field in fields)


def _dimension_item(item, kind):
    """One dimension field as its column type; null stays NULL."""
    if item is None:
        return None
    if kind == "text" and type(item) is str:
        return item
    if kind == "bigint" and type(item) is int and -(2**63) <= item < 2**63:
        return item
    if kind == "double precision" and type(item) in (int, float) and math.isfinite(item):
        return float(item)
    raise ValueError("Invalid geo dimension")


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


def _reconcile_counters(summary):
    """The run's attempted and in-country counts and its histogram, reconciled with its counters."""
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
    return attempted, in_country, histogram


def _scope(summary):
    """The run's provider and product labels, and its scope with the scope's hash."""
    config = summary["config"]
    provider, product = config.get("provider"), config.get("product")
    if not provider or not product:
        raise ValueError("Persistence requires provider and product labels")
    scope = {
        "target": summary["target"],
        "config": {key: value for key, value in config.items() if key not in RUN_LENGTH},
    }
    scope_hash = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    return provider, product, scope, scope_hash


def _read_buckets(summary, histogram):
    """The run's buckets by start and their rows, reconciled with the run's counters."""
    buckets = {_time(b["bucket_start"]): b for b in summary["buckets"]}
    if (
        len(buckets) != len(summary["buckets"])
        or sum(b["completed"] for b in buckets.values()) != summary["completed"]
        or sum(b["successes"] for b in buckets.values()) != summary["successes"]
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
    return buckets, bucket_rows


def _read_run(directory):
    """Read and reconcile a run directory's summary, buckets and geo rows before any write."""
    summary_path = directory / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary_hash = _digest(summary_path)
    if summary["schema_version"] != "1":
        raise ValueError("Unsupported measurement schema")
    attempted, in_country, histogram = _reconcile_counters(summary)
    source = directory / "ips.jsonl.gz"
    if _digest(source) != summary["artifacts"]["ips"]["sha256"]:
        raise ValueError("IP artifact checksum mismatch")
    provider, product, scope, scope_hash = _scope(summary)
    run_id = summary["run_id"]
    buckets, bucket_rows = _read_buckets(summary, histogram)
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
