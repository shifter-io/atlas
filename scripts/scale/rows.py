"""SCALE-GEN-1a: the COPY helpers, row builders and column lists writer.py writes with."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from psycopg.types.json import Jsonb

BODY, UP, DOWN = 14, 180, 420  # bytes per request
BATCH = 20000  # COPY lines per write


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S+00")


def copy_lines(conn, table: str, columns: str, lines) -> None:
    """COPY tab-separated text lines (values never hold tabs, newlines or backslashes)."""
    with conn.cursor().copy(f"COPY {table}({columns}) FROM STDIN") as copy:
        batch = []
        for line in lines:
            batch.append(line)
            if len(batch) == BATCH:
                copy.write("\n".join(batch) + "\n")
                batch.clear()
        if batch:
            copy.write("\n".join(batch) + "\n")


def copy_rows(conn, table: str, columns: str, rows) -> None:
    with conn.cursor().copy(f"COPY {table}({columns}) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def steps(bins) -> tuple[list[int], list[int]]:
    """Histogram bins as stored: millisecond gaps (the first absolute) and counts."""
    ms = [m for m, _ in bins]
    return [b - a for a, b in zip([0, *ms], ms, strict=False)], [n for _, n in bins]


def batch_id(day: int, hour: int, country: int, countries: int) -> int:
    return ((day - 1) * 24 + hour) * countries + country + 1


def bucket_row(moment, run_key, successes, errors, bins) -> tuple:
    """A run_buckets row with typed outcome counters (timeouts are the only errors)."""
    completed = successes + errors
    counters = (
        Jsonb({"timeout": errors} if errors else {}),
        Jsonb({"200": successes} if successes else {}),
    )
    outcome = (None, True, 0, 0, errors, 0, completed * UP, successes * DOWN)
    return (
        moment,
        run_key,
        completed,
        successes,
        successes * BODY,
        *counters,
        *steps(bins),
        *outcome,
    )


def geo_line(slot, run_key, row) -> str:
    location, network, completed, successes, ms = row
    timeouts = completed - successes
    histogram = f"{{{ms}}}\t{{{successes}}}" if successes else "{}\t{}"
    return (
        f"{stamp(slot)}\t{run_key}\t{location}\t{network}\t{completed}\t{successes}\t0\t0\t"
        f"{timeouts}\t{completed * UP}\t{successes * DOWN}\t{histogram}"
    )


PLACE_COLUMNS = "place_id,country_code,country,region_code,region,city"
LOCATION_COLUMNS = "location_id,place_id,latitude,longitude"
NETWORK_COLUMNS = "network_id,asn_name_id,isp,organization,connection_type,usage_type"
POOL_COLUMNS = (
    "pool_id,product_id,pool_hash,unique_ips,target_id,target_url,target_engine,target_version,"
    "proxy_protocol,requested_country,runner_location"
)
SCOPE_COLUMNS = (
    "scope_id,pool_id,product_id,target,scope_hash,metrics,updated_at,concurrency,timeout_seconds"
)
RUN_COLUMNS = (
    "run_key,run_id,scope_id,started_at,finished_at,status,summary_sha256,summary,imported_at,"
    "batch_id"
)
BUCKET_COLUMNS = (
    "bucket_start,run_key,completed,successes,response_body_bytes,errors,status_codes,"
    "histogram_steps,histogram_counts,extra,outcome_counters,challenges,blocks,timeouts,"
    "proxy_errors,bytes_up,bytes_down"
)
BUCKET_INSERT = f"INSERT INTO run_buckets({BUCKET_COLUMNS}) VALUES ({','.join(['%s'] * 17)})"
OBSERVATION_COLUMNS = "bucket_start,run_key,ip_id,count,first_seen,last_seen"
GEO_COLUMNS = (
    "bucket_start,run_key,location_id,network_id,completed,successes,challenges,blocks,timeouts,"
    "bytes_up,bytes_down,histogram_steps,histogram_counts"
)
BATCH_COLUMNS = (
    "batch_id,kind,country_code,region_id,target_id,scheduled_for,start_at,started_at,"
    "finished_at,status"
)
MEMBER_COLUMNS = (
    "batch_id,product_id,status,started_at,finished_at,last_progress_at,requests,successes,"
    "unique_ips,challenges,blocks,timeouts,proxy_errors,bytes_up,bytes_down,stop_reason,run_key"
)
ESTIMATE_COLUMNS = (
    "product_id,country_code,day,hour,run_key,measured_at,measured_ips,measured_requests,"
    "hour_factor,unique_ips,requests,day_unique_ips,curve,formula_version,computed_at,released_at"
)
