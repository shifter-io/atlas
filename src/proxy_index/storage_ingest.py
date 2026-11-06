"""The database steps of storage.ingest: each runs inside the one transaction ingest opens."""

from __future__ import annotations

import gzip
import hashlib
import ipaddress
import json
import re
import uuid
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from . import names
from .partitions import partition

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


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Artifact timestamps require a timezone")
    return result.astimezone(UTC)


def _canonical(value):
    return json.dumps(value, sort_keys=True)


def load_ips(conn, run, ip_rows):
    """Copy the IP observations into incoming_ips and reconcile them; return their row count."""
    summary, run_id, buckets = run.summary, run.run_id, run.buckets
    conn.execute(
        "CREATE TEMP TABLE incoming_ips (bucket timestamptz, ip inet, count bigint, "
        "first_seen timestamptz, last_seen timestamptz, PRIMARY KEY(bucket,ip)) "
        "ON COMMIT DROP"
    )
    with (
        conn.cursor().copy("COPY incoming_ips FROM STDIN") as copy,
        gzip.open(run.source, "rt") as f,
    ):
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
            copy.write_row(
                (bucket, str(ipaddress.ip_address(row["ip"])), row["count"], first, last)
            )
    rows, observations, unique = conn.execute(
        "SELECT count(*),coalesce(sum(count),0),count(DISTINCT ip) FROM incoming_ips"
    ).fetchone()
    if ip_rows and (
        rows != summary["artifacts"]["ips"]["rows"]
        or observations != (run.in_country if summary["pool_eligible"] else 0)
        or unique != (summary["unique_ips"] or 0)
    ):
        raise ValueError("IP observations do not reconcile with the run")
    return rows


def insert_run(conn, run, scope_id, batch_id):
    """Insert the test_runs row with the summary keys its columns cannot rebuild; its run_key."""
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
    run_key = conn.execute(
        "INSERT INTO test_runs (run_id,scope_id,"
        "started_at,finished_at,status,summary_sha256,summary,batch_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING run_key",
        (
            run_id,
            scope_id,
            started_at,
            finished_at,
            summary["status"],
            run.summary_hash,
            Jsonb(compact),
            batch_id,
        ),
    )
    return run_key.fetchone()[0]


def insert_buckets(conn, run, run_key):
    """Add the run's buckets, creating the daily partitions they and their IPs need."""
    for bucket, values in run.bucket_rows.items():
        partition(conn, "run_buckets", bucket)
        partition(conn, "ip_observations", bucket)
        conn.execute(
            "INSERT INTO run_buckets(bucket_start,run_key,completed,successes,"
            "response_body_bytes,errors,status_codes,histogram_steps,histogram_counts,"
            "extra,outcome_counters,"
            + ",".join(OUTCOME_KEYS)
            + ") VALUES ("
            + ",".join(["%s"] * (len(values) + 2))
            + ")",
            (bucket, run_key, *values),
        )


def check_stored(conn, run, run_key):
    """Raise unless the stored rows rebuild the summary exactly."""
    # The run histogram is stored only as bucket bins, exactly as in v4.
    expected = run.summary | {"latency_histogram": run.histogram.export()}
    stored = conn.execute("SELECT stored_run_summary(%s)", (run_key,)).fetchone()[0]
    if _canonical(stored) != _canonical(expected):
        raise ValueError("Run summary cannot be stored losslessly")


def add_observations(conn, run_key, pool_id):
    """Add the run's IP observations and count the IPs new to its pool."""
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


def enrich(conn, directory, run_id, run_key, expected_rows):
    """Import each enrichment report of the run once; return how many releases were added."""
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
        _load_enrichment(conn, artifact, run_id, release, expected_rows)
        _store_enrichment(conn, release_id)
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


def _load_enrichment(conn, artifact, run_id, release, expected_rows):
    """Copy one enrichment artifact into incoming_geo; it must match incoming_ips row for row."""
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


def _store_enrichment(conn, release_id):
    """Add incoming_geo's per-IP enrichment for a release; it must agree with any stored."""
    # Cast and compare the same typed values used on insert; missing and null
    # known fields both map to SQL NULL. Unknown attributes remain lossless.
    values = [f"(g.attributes->>'{key}')::{kind}" for key, kind in ENRICHMENT_FIELDS.items()]
    extras = (
        "g.attributes - ARRAY[" + ",".join(f"'{key}'" for key in ENRICHMENT_FIELDS) + "]::text[]"
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
    joins = names.resolve(
        conn, "incoming_geo g", lambda f: f"(g.attributes->>'{f}')::{ENRICHMENT_FIELDS[f]}"
    )
    conn.execute(
        "INSERT INTO ip_enrichment(ip_id,release_id,location_id,network_id,matched,"
        "extra_attributes) SELECT DISTINCT a.ip_id,%s,location_id.location_id,"
        "network_id.network_id,(g.attributes->>'matched')::boolean,nullif(("
        + extras
        + "),'{}'::jsonb) FROM incoming_geo g JOIN ip_addresses a USING(ip) "
        + joins
        + " ON CONFLICT DO NOTHING",
        (release_id,),
    )
