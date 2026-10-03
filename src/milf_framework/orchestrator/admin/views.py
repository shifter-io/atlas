"""The admin UI's data layer: every query the /admin pages and actions run.

The operator UI is the Astro app under src/pages/admin; it reaches these functions over the
JSON API in `routes.py` and never talks to the database itself. Readers return plain dicts and
lists (numbers as numbers, timestamps as datetimes) and the UI decides how to print them.
Everything here reads the live bookkeeping tables (test_batches, batch_runs, nodes, alerts,
job_runs, test_events, outages); none of it scans run_buckets or the calculated layer, so the
dashboard can poll without competing with ingest.

Writers (kill, skip, acknowledge, schedule edits) take an already validated value; `routes.py`
owns the parsing and the HTTP errors.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

import psycopg

from ..batches import census_every, next_slot, run_finished
from ..db import Database, mark_closed, put_setting, setting
from ..events import emit

GB = 10**9
HEATMAP_SLOT = timedelta(minutes=30)
HEATMAP_SLOTS = 48
OPEN_BATCHES = ("scheduled", "running")
OPEN_RUNS = ("queued", "running", "uploading")
BATCH_STATUSES = ("scheduled", "running", "done", "failed", "skipped", "killed")
KINDS = ("census", "pulse", "load")
# Editable per-target schedule fields and their inclusive bounds.
TARGET_FIELDS: dict[str, tuple[int, int] | None] = {
    "pulse_enabled": None,
    "pulse_interval_minutes": (1, 1440),
    "pulse_requests": (1, 100_000),
    "pulse_concurrency": (1, 10_000),
    "census_concurrency": (1, 100_000),
}


async def _rows(conn: psycopg.AsyncConnection, query: str, params: Any = None) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def _row(conn: psycopg.AsyncConnection, query: str, params: Any = None) -> dict | None:
    return await (await conn.execute(query, params)).fetchone()


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def _hhmm(value: time | None) -> str | None:
    return value.strftime("%H:%M") if value is not None else None


def _ratio(successes: int, unique_ips: int) -> float | None:
    """Successes per unique IP: the census stops once this passes 2."""
    return successes / unique_ips if unique_ips else None


def _rate(part: int, whole: int) -> float | None:
    return part / whole if whole else None


# --- readers -----------------------------------------------------------------------------


async def running_batches(
    conn: psycopg.AsyncConnection, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Open census batches, each with every product's live progress."""
    now = _now(now)
    batches = await _rows(
        conn,
        "SELECT b.batch_id,b.kind,b.country_code,c.name AS country,r.slug AS region,b.target_id,"
        "b.scheduled_for,b.start_at,b.started_at,b.status "
        "FROM test_batches b JOIN runner_regions r ON r.region_id=b.region_id "
        "JOIN countries c ON c.country_code=b.country_code "
        "WHERE b.kind='census' AND b.status IN ('scheduled','running') "
        "ORDER BY b.start_at,b.country_code,b.target_id",
    )
    if not batches:
        return []
    runs = await _batch_runs(conn, [b["batch_id"] for b in batches], now)
    for batch in batches:
        batch["runs"] = [run for run in runs if run["batch_id"] == batch["batch_id"]]
        batch["elapsed_s"] = (
            int((now - batch["started_at"]).total_seconds()) if batch["started_at"] else None
        )
        _totals(batch)
    return batches


def _totals(batch: dict[str, Any]) -> None:
    runs = batch["runs"]
    for key in ("requests", "successes", "unique_ips", "bytes_up", "bytes_down"):
        batch[key] = sum(run[key] for run in runs)
    batch["runs_total"] = len(runs)
    batch["runs_open"] = sum(run["status"] in OPEN_RUNS for run in runs)
    batch["runs_done"] = sum(run["status"] == "done" for run in runs)
    batch["runs_failed"] = sum(run["status"] == "failed" for run in runs)


async def _batch_runs(
    conn: psycopg.AsyncConnection, batch_ids: list[int], now: datetime
) -> list[dict[str, Any]]:
    rows = await _rows(
        conn,
        "SELECT br.batch_id,br.product_id,p.provider,p.name AS product,br.node_id,n.host,"
        "br.status,br.started_at,br.finished_at,br.last_progress_at,br.requests,br.successes,"
        "br.unique_ips,br.challenges,br.blocks,br.timeouts,br.proxy_errors,br.bytes_up,"
        "br.bytes_down,br.stop_reason,br.error,br.run_key "
        "FROM batch_runs br JOIN products p USING(product_id) "
        "LEFT JOIN nodes n ON n.node_id=br.node_id "
        "WHERE br.batch_id=ANY(%s) ORDER BY br.batch_id,p.provider,p.name",
        (batch_ids,),
    )
    for row in rows:
        row["label"] = f"{row['provider']}/{row['product']}"
        row["ratio"] = _ratio(row["successes"], row["unique_ips"])
        row["success_rate"] = _rate(row["successes"], row["requests"])
        seen = row["last_progress_at"] or row["started_at"]
        row["silent_s"] = (
            int((now - seen).total_seconds())
            if seen is not None and row["status"] in OPEN_RUNS
            else None
        )
        end = row["finished_at"] or (now if row["status"] in OPEN_RUNS else None)
        row["duration_s"] = (
            int((end - row["started_at"]).total_seconds())
            if row["started_at"] is not None and end is not None
            else None
        )
    return rows


async def pulse_open(conn: psycopg.AsyncConnection) -> list[dict[str, Any]]:
    """Open pulse slots, one row per target and slot, summed over countries and products."""
    return await _rows(
        conn,
        "SELECT b.target_id,b.scheduled_for,min(b.start_at) AS start_at,"
        "count(DISTINCT b.batch_id)::int AS batches,"
        "count(*) FILTER (WHERE br.status IN ('queued','running','uploading'))::int AS runs_open,"
        "count(*) FILTER (WHERE br.status='done')::int AS runs_done,"
        "count(*) FILTER (WHERE br.status='failed')::int AS runs_failed,"
        "coalesce(sum(br.requests),0)::bigint AS requests,"
        "coalesce(sum(br.successes),0)::bigint AS successes "
        "FROM test_batches b LEFT JOIN batch_runs br USING(batch_id) "
        "WHERE b.kind='pulse' AND b.status IN ('scheduled','running') "
        "GROUP BY b.target_id,b.scheduled_for ORDER BY b.scheduled_for,b.target_id",
    )


async def pulse_heatmap(
    conn: psycopg.AsyncConnection, now: datetime | None = None
) -> dict[str, Any]:
    """Pulse success per runner region per 30-minute slot over the last 24 hours.

    A cell is successes / requests over every pulse run scheduled in that slot (live runs
    included); null means no pulse request was made there, which is not the same as 0%.
    """
    now = _now(now)
    step = int(HEATMAP_SLOT.total_seconds())
    last = datetime.fromtimestamp(now.timestamp() // step * step, UTC)
    first = last - HEATMAP_SLOT * (HEATMAP_SLOTS - 1)
    slots = [first + HEATMAP_SLOT * i for i in range(HEATMAP_SLOTS)]
    regions = await _rows(
        conn,
        "SELECT region_id,slug FROM runner_regions WHERE enabled "
        "OR region_id IN (SELECT region_id FROM test_batches WHERE kind='pulse' "
        " AND scheduled_for>=%s) ORDER BY region_id",
        (first,),
    )
    cells = await _rows(
        conn,
        "SELECT b.region_id,"
        "floor(extract(epoch FROM b.scheduled_for)/%(step)s)::bigint*%(step)s AS slot,"
        "coalesce(sum(br.requests),0)::bigint AS requests,"
        "coalesce(sum(br.successes),0)::bigint AS successes "
        "FROM test_batches b JOIN batch_runs br USING(batch_id) "
        "WHERE b.kind='pulse' AND b.scheduled_for>=%(first)s AND b.scheduled_for<%(end)s "
        "GROUP BY 1,2",
        {"step": step, "first": first, "end": last + HEATMAP_SLOT},
    )
    index = {(c["region_id"], int(c["slot"])): c for c in cells}
    rows = []
    for region in regions:
        found = [index.get((region["region_id"], int(slot.timestamp()))) for slot in slots]
        rows.append(
            {
                "region": region["slug"],
                "requests": [c["requests"] if c else 0 for c in found],
                "rates": [_rate(c["successes"], c["requests"]) if c else None for c in found],
            }
        )
    return {"slot_minutes": step // 60, "slots": slots, "regions": rows}


async def open_alerts(conn: psycopg.AsyncConnection, limit: int = 50) -> list[dict[str, Any]]:
    return await _rows(
        conn,
        "SELECT alert_id,kind,severity,message,data,occurrences,first_at,last_at "
        "FROM alerts WHERE acknowledged_at IS NULL ORDER BY CASE severity "
        "WHEN 'critical' THEN 0 WHEN 'major' THEN 1 WHEN 'minor' THEN 2 ELSE 3 END,"
        "last_at DESC LIMIT %s",
        (limit,),
    )


async def recent_jobs(conn: psycopg.AsyncConnection, limit: int = 20) -> list[dict[str, Any]]:
    return await _rows(
        conn,
        "SELECT id,job,started_at,finished_at,status,error,duration_ms FROM job_runs "
        "ORDER BY started_at DESC,id DESC LIMIT %s",
        (limit,),
    )


async def job_health(conn: psycopg.AsyncConnection, now: datetime | None = None) -> list[dict]:
    """The latest run of each job, with its failures over the last day."""
    now = _now(now)
    return await _rows(
        conn,
        "SELECT DISTINCT ON (j.job) j.job,j.started_at,j.finished_at,j.status,j.error,"
        "j.duration_ms,extract(epoch FROM %(now)s-j.started_at)::int AS age_s,"
        "(SELECT count(*) FROM job_runs f WHERE f.job=j.job AND f.status='failed' "
        " AND f.started_at>=%(day)s)::int AS failures_24h,"
        "(SELECT max(o.finished_at) FROM job_runs o WHERE o.job=j.job AND o.status='ok') "
        " AS last_ok_at "
        "FROM job_runs j ORDER BY j.job,j.started_at DESC,j.id DESC",
        {"now": now, "day": now - timedelta(days=1)},
    )


async def upcoming_census(
    conn: psycopg.AsyncConnection, now: datetime | None = None, limit: int = 12
) -> list[dict[str, Any]]:
    """The next census slots of enabled countries, soonest first (census.every_minutes, when
    set, puts every country on the same next slot)."""
    now = _now(now)
    every = census_every(await setting(conn, "census.every_minutes"))
    if every is not None:
        rows = await _rows(
            conn,
            "SELECT c.country_code,c.name,r.slug AS region,NULL::time AS census_time,"
            "%(next)s::timestamptz AS next_at FROM countries c JOIN runner_regions r "
            "ON r.region_id=c.region_id AND r.enabled WHERE c.enabled AND EXISTS "
            "(SELECT 1 FROM targets t WHERE t.census) ORDER BY c.country_code LIMIT %(limit)s",
            {"next": next_slot(every, now), "limit": limit},
        )
    else:
        rows = await _rows(
            conn,
            "SELECT c.country_code,c.name,r.slug AS region,c.census_time,"
            "CASE WHEN ((%(now)s AT TIME ZONE 'UTC')::date+c.census_time) AT TIME ZONE 'UTC'"
            " >= %(now)s THEN ((%(now)s AT TIME ZONE 'UTC')::date+c.census_time) AT TIME ZONE"
            " 'UTC' ELSE ((%(now)s AT TIME ZONE 'UTC')::date+1+c.census_time) AT TIME ZONE 'UTC'"
            " END AS next_at "
            "FROM countries c JOIN runner_regions r ON r.region_id=c.region_id AND r.enabled "
            "WHERE c.enabled AND EXISTS (SELECT 1 FROM targets t WHERE t.census) "
            "ORDER BY next_at,c.country_code LIMIT %(limit)s",
            {"now": now, "limit": limit},
        )
    for row in rows:
        row["census_time"] = _hhmm(row["census_time"])
        row["in_s"] = int((row["next_at"] - now).total_seconds())
    return rows


async def dashboard(conn: psycopg.AsyncConnection, now: datetime | None = None) -> dict:
    """Everything the front page shows, in one call."""
    now = _now(now)
    nodes = await node_counts(conn)
    return {
        "now": now,
        "census": await running_batches(conn, now),
        "pulse": await pulse_open(conn),
        "heatmap": await pulse_heatmap(conn, now),
        "alerts": await open_alerts(conn),
        "jobs": await recent_jobs(conn, 10),
        "failed_jobs": sum(job["status"] == "failed" for job in await job_health(conn, now)),
        "upcoming": await upcoming_census(conn, now),
        "nodes": nodes,
        "open_outages": await open_outages(conn),
    }


async def open_outages(conn: psycopg.AsyncConnection) -> int:
    """Product x country outages still open (a stalled census or a pulse 'down')."""
    row = await _row(conn, "SELECT count(*)::int AS n FROM outages WHERE ended_at IS NULL")
    return row["n"]


async def node_counts(conn: psycopg.AsyncConnection) -> dict[str, int]:
    row = await _row(
        conn,
        "SELECT count(*) FILTER (WHERE status='online')::int AS online,"
        "count(*) FILTER (WHERE status<>'online')::int AS offline FROM nodes",
    )
    grid = await node_grid(conn)
    return {
        "online": row["online"],
        "offline": row["offline"],
        "missing": sum(cell["status"] == "missing" for cell in grid["cells"]),
    }


async def batch_list(
    conn: psycopg.AsyncConnection,
    *,
    kind: str | None = None,
    status: str | None = None,
    country: str | None = None,
    limit: int = 100,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Batches newest first, with their runs summed."""
    now = _now(now)
    clauses, params = ["true"], []
    if kind:
        clauses.append("b.kind=%s")
        params.append(kind)
    if status:
        clauses.append("b.status=%s")
        params.append(status)
    if country:
        clauses.append("b.country_code=%s")
        params.append(country)
    rows = await _rows(
        conn,
        "SELECT b.batch_id,b.kind,b.country_code,r.slug AS region,b.target_id,b.scheduled_for,"
        "b.start_at,b.started_at,b.finished_at,b.status,"
        "count(br.product_id)::int AS runs_total,"
        "count(*) FILTER (WHERE br.status IN ('queued','running','uploading'))::int AS runs_open,"
        "count(*) FILTER (WHERE br.status='done')::int AS runs_done,"
        "count(*) FILTER (WHERE br.status='failed')::int AS runs_failed,"
        "coalesce(sum(br.requests),0)::bigint AS requests,"
        "coalesce(sum(br.successes),0)::bigint AS successes,"
        "coalesce(sum(br.unique_ips),0)::bigint AS unique_ips,"
        "coalesce(sum(br.bytes_up),0)::bigint AS bytes_up,"
        "coalesce(sum(br.bytes_down),0)::bigint AS bytes_down "
        "FROM test_batches b JOIN runner_regions r ON r.region_id=b.region_id "
        "LEFT JOIN batch_runs br USING(batch_id) WHERE "
        + " AND ".join(clauses)
        + " GROUP BY b.batch_id,r.slug ORDER BY b.scheduled_for DESC,b.batch_id DESC LIMIT %s",
        (*params, limit),
    )
    for row in rows:
        row["success_rate"] = _rate(row["successes"], row["requests"])
        row["duration_s"] = _duration(row, now)
    return rows


def _duration(batch: dict[str, Any], now: datetime) -> int | None:
    if batch["started_at"] is None:
        return None
    end = batch["finished_at"] or now
    return int((end - batch["started_at"]).total_seconds())


async def batch_detail(
    conn: psycopg.AsyncConnection, batch_id: int, now: datetime | None = None
) -> dict[str, Any] | None:
    """One batch: its runs per product and its tape events."""
    now = _now(now)
    batch = await _row(
        conn,
        "SELECT b.batch_id,b.kind,b.country_code,c.name AS country,r.slug AS region,b.target_id,"
        "t.url AS target_url,b.scheduled_for,b.start_at,b.started_at,b.finished_at,b.status "
        "FROM test_batches b JOIN runner_regions r ON r.region_id=b.region_id "
        "JOIN countries c ON c.country_code=b.country_code "
        "JOIN targets t ON t.target_id=b.target_id WHERE b.batch_id=%s",
        (batch_id,),
    )
    if batch is None:
        return None
    batch["runs"] = await _batch_runs(conn, [batch_id], now)
    _totals(batch)
    batch["success_rate"] = _rate(batch["successes"], batch["requests"])
    batch["duration_s"] = _duration(batch, now)
    # Bounded on `at` so the partitioned table is pruned to the months the batch can touch.
    batch["events"] = await _rows(
        conn,
        "SELECT e.event_id,e.at,e.kind,e.product_id,p.provider,p.name AS product,e.signal,"
        "e.severity,e.data FROM test_events e LEFT JOIN products p USING(product_id) "
        "WHERE e.batch_id=%s AND e.at>=%s ORDER BY e.at DESC,e.event_id DESC LIMIT 200",
        (batch_id, batch["scheduled_for"] - timedelta(days=1)),
    )
    return batch


async def schedule(conn: psycopg.AsyncConnection, now: datetime | None = None) -> dict:
    """Every country's census slot and every target's census and pulse settings."""
    now = _now(now)
    countries = await _rows(
        conn,
        "SELECT c.country_code,c.name,c.region_id,r.slug AS region,r.enabled AS region_enabled,"
        "c.census_time,c.enabled FROM countries c "
        "JOIN runner_regions r ON r.region_id=c.region_id ORDER BY r.region_id,c.census_time,"
        "c.country_code",
    )
    for row in countries:
        row["census_time"] = _hhmm(row["census_time"])
    targets = await _rows(
        conn,
        "SELECT target_id,label,url,engine,timeout_ms,census,census_concurrency,pulse_enabled,"
        "pulse_interval_minutes,pulse_requests,pulse_concurrency,sort_order FROM targets "
        "ORDER BY sort_order,target_id",
    )
    regions = await _rows(
        conn,
        "SELECT r.region_id,r.slug,r.enabled,count(c.country_code)::int AS countries,"
        "count(c.country_code) FILTER (WHERE c.enabled)::int AS enabled_countries "
        "FROM runner_regions r LEFT JOIN countries c ON c.region_id=r.region_id "
        "GROUP BY r.region_id ORDER BY r.region_id",
    )
    return {
        "now": now,
        "regions": regions,
        "countries": countries,
        "targets": targets,
        "scheduler": await scheduler_state(conn),
    }


SCHEDULER_SWITCHES = {"census": "scheduler.census_enabled", "pulse": "scheduler.pulse_enabled"}


async def scheduler_state(conn: psycopg.AsyncConnection) -> dict[str, Any]:
    """Whether each planner is on, and the census interval (None: the daily slot)."""
    return {
        **{kind: bool(await setting(conn, key)) for kind, key in SCHEDULER_SWITCHES.items()},
        "census_every_minutes": await setting(conn, "census.every_minutes"),
    }


async def set_scheduler(conn: psycopg.AsyncConnection, values: dict[str, bool]) -> dict:
    """Switch the census and/or pulse planner on or off (keys 'census', 'pulse')."""
    unknown = set(values) - set(SCHEDULER_SWITCHES)
    if unknown or not values:
        raise ValueError("unknown or missing scheduler switches")
    for kind, enabled in values.items():
        await put_setting(conn, SCHEDULER_SWITCHES[kind], bool(enabled))
    return await scheduler_state(conn)


async def node_grid(conn: psycopg.AsyncConnection, now: datetime | None = None) -> dict:
    """Every product with a node against every region, including the cells with none."""
    now = _now(now)
    regions = await _rows(
        conn,
        "SELECT region_id,slug,enabled FROM runner_regions WHERE enabled "
        "OR region_id IN (SELECT region_id FROM nodes) ORDER BY region_id",
    )
    products = await _rows(
        conn,
        "SELECT DISTINCT provider,product FROM nodes ORDER BY provider,product",
    )
    nodes = await _rows(
        conn,
        "SELECT n.node_id,n.region_id,n.provider,n.product,n.host,n.status,n.last_heartbeat,"
        "n.version,n.open_conns,n.req_per_s,n.rss_mb,"
        "(SELECT count(*) FROM batch_runs br WHERE br.node_id=n.node_id "
        " AND br.status IN ('running','uploading'))::int AS runs_open "
        "FROM nodes n",
    )
    offline_after = int(await setting(conn, "node.offline_seconds"))
    index = {(n["provider"], n["product"], n["region_id"]): n for n in nodes}
    cells = []
    for product in products:
        for region in regions:
            node = index.get((product["provider"], product["product"], region["region_id"]))
            cell = {
                "provider": product["provider"],
                "product": product["product"],
                "label": f"{product['provider']}/{product['product']}",
                "region": region["slug"],
                "node_id": None,
                "status": "missing",
                "host": None,
                "last_heartbeat": None,
                "heartbeat_age_s": None,
                "version": None,
                "open_conns": None,
                "req_per_s": None,
                "rss_mb": None,
                "runs_open": 0,
            }
            if node is not None:
                beat = node["last_heartbeat"]
                cell |= {
                    key: node[key]
                    for key in (
                        "node_id",
                        "status",
                        "host",
                        "last_heartbeat",
                        "version",
                        "open_conns",
                        "req_per_s",
                        "rss_mb",
                        "runs_open",
                    )
                }
                cell["heartbeat_age_s"] = int((now - beat).total_seconds()) if beat else None
            cells.append(cell)
    return {
        "now": now,
        "offline_after_s": offline_after,
        "regions": [r["slug"] for r in regions],
        "products": [f"{p['provider']}/{p['product']}" for p in products],
        "cells": cells,
    }


async def traffic(
    conn: psycopg.AsyncConnection,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    test: str | None = None,
    provider: str | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    """Wire bytes per test and provider product, from the batch_runs counters.

    Batches are selected by their scheduled slot in [start, end). A run counts its final
    counters once it is terminal and its latest progress while it is still going; queued runs
    have sent nothing and are left out. GB are decimal (10^9 bytes), like the traffic report.
    """
    clauses, params = ["br.status<>'queued'"], []
    if start is not None:
        clauses.append("b.scheduled_for>=%s")
        params.append(start)
    if end is not None:
        clauses.append("b.scheduled_for<%s")
        params.append(end)
    if test:
        clauses.append("b.kind=%s")
        params.append(test)
    if provider:
        name, _, product = provider.partition("/")
        clauses.append("p.provider=%s")
        params.append(name)
        if product:
            clauses.append("p.name=%s")
            params.append(product)
    if region:
        clauses.append("r.slug=%s")
        params.append(region)
    rows = await _rows(
        conn,
        "SELECT b.kind AS test,p.provider,p.name AS product,"
        "count(DISTINCT b.batch_id)::int AS batches,count(*)::int AS runs,"
        "coalesce(sum(br.requests),0)::bigint AS requests,"
        "coalesce(sum(br.bytes_up),0)::bigint AS bytes_up,"
        "coalesce(sum(br.bytes_down),0)::bigint AS bytes_down "
        "FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "JOIN products p USING(product_id) JOIN runner_regions r ON r.region_id=b.region_id "
        "WHERE " + " AND ".join(clauses) + " GROUP BY 1,2,3 ORDER BY 1,2,3",
        params,
    )
    for row in rows:
        row["label"] = f"{row['provider']}/{row['product']}"
        row |= _traffic_totals([row])
    return {
        "unit": "GB = 10^9 bytes",
        "filters": {
            "from": start,
            "to": end,
            "test": test,
            "provider": provider,
            "region": region,
        },
        "rows": rows,
        "by_test": {
            kind: _traffic_totals([r for r in rows if r["test"] == kind]) for kind in KINDS
        },
        "total": _traffic_totals(rows),
    }


def _traffic_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    requests = sum(r["requests"] for r in rows)
    up = sum(r["bytes_up"] for r in rows)
    down = sum(r["bytes_down"] for r in rows)
    return {
        "requests": requests,
        "bytes_up": up,
        "bytes_down": down,
        "bytes": up + down,
        "gb": (up + down) / GB,
        "avg_bytes_per_request": (up + down) / requests if requests else None,
    }


async def traffic_choices(conn: psycopg.AsyncConnection) -> dict[str, list[str]]:
    """What the traffic filters can be set to."""
    providers = await _rows(
        conn,
        "SELECT DISTINCT p.provider,p.name FROM batch_runs br JOIN products p USING(product_id) "
        "ORDER BY 1,2",
    )
    regions = await _rows(conn, "SELECT slug FROM runner_regions ORDER BY region_id")
    return {
        "providers": sorted(
            {p["provider"] for p in providers} | {f"{p['provider']}/{p['name']}" for p in providers}
        ),
        "regions": [r["slug"] for r in regions],
    }


# --- writers -----------------------------------------------------------------------------


class Conflict(Exception):
    """The action does not apply to the row in its current state."""


async def stop_batch(
    db: Database, batch_id: int, status: str, now: datetime | None = None
) -> dict[str, Any] | None:
    """Kill or skip an open batch: the batch and its open runs end now, nodes are told to stop.

    `status` is 'killed' or 'skipped'. Open runs become 'killed' with stop_reason 'operator'
    either way (a skipped batch still stops what is already running); runs that already
    finished keep their result. Each killed census run gets its census_finished event from
    batches.run_finished, like any other run that ends. On top of that a skipped census writes
    'census_skipped', and a killed or skipped pulse batch 'pulse_finished' (pulse runs write no
    per-run event), both with data.reason 'operator'.
    Returns None for an unknown batch; raises Conflict when it is no longer open.
    """
    if status not in ("killed", "skipped"):
        raise ValueError("status must be killed or skipped")
    now = _now(now)
    await db.ensure_partition("test_events", now)
    async with db.transaction() as conn:
        batch = await _row(
            conn,
            "SELECT b.batch_id,b.kind,b.status,b.country_code,b.target_id,b.region_id,"
            "r.slug AS region FROM test_batches b JOIN runner_regions r "
            "ON r.region_id=b.region_id WHERE b.batch_id=%s FOR UPDATE OF b",
            (batch_id,),
        )
        if batch is None:
            return None
        if batch["status"] not in OPEN_BATCHES:
            raise Conflict(f"batch {batch_id} is already {batch['status']}")
        killed = await _rows(
            conn,
            "UPDATE batch_runs SET status='killed',finished_at=%s,stop_reason='operator' "
            "WHERE batch_id=%s AND status IN ('queued','running','uploading') "
            "RETURNING product_id",
            (now, batch_id),
        )
        await conn.execute(
            "UPDATE test_batches SET status=%s,finished_at=%s WHERE batch_id=%s",
            (status, now, batch_id),
        )
        mark_closed(conn, batch_id)
        for run in killed:
            await run_finished(conn, batch_id, run["product_id"], now)
        # Load batches (latency under load) have no tape event of their own.
        if batch["kind"] != "load" and (batch["kind"] == "pulse" or status == "skipped"):
            totals = await _row(
                conn,
                "SELECT coalesce(sum(requests),0)::bigint AS requests,"
                "coalesce(sum(successes),0)::bigint AS successes,"
                "coalesce(sum(unique_ips),0)::bigint AS unique_ips "
                "FROM batch_runs WHERE batch_id=%s",
                (batch_id,),
            )
            await emit(
                conn,
                "census_skipped" if batch["kind"] == "census" else "pulse_finished",
                now,
                batch_id=batch_id,
                country_code=batch["country_code"],
                target_id=batch["target_id"],
                region_id=batch["region_id"],
                severity="minor",
                data={
                    "region": batch["region"],
                    "reason": "operator",
                    "status": status,
                    "runs_killed": len(killed),
                    **totals,
                },
            )
    return {"batch_id": batch_id, "status": status, "runs_killed": len(killed)}


async def acknowledge(conn: psycopg.AsyncConnection, alert_id: int) -> bool | None:
    """True when acknowledged now, False when it already was, None for an unknown alert."""
    if await _row(
        conn,
        "UPDATE alerts SET acknowledged_at=now() WHERE alert_id=%s AND acknowledged_at IS NULL "
        "RETURNING alert_id",
        (alert_id,),
    ):
        return True
    exists = await _row(conn, "SELECT 1 FROM alerts WHERE alert_id=%s", (alert_id,))
    return False if exists else None


async def set_country(
    conn: psycopg.AsyncConnection,
    country_code: str,
    *,
    census_time: time | None = None,
    enabled: bool | None = None,
) -> dict[str, Any] | None:
    """Change a country's census slot and/or enabled flag. None for an unknown country."""
    row = await _row(
        conn,
        "UPDATE countries SET census_time=coalesce(%s,census_time),enabled=coalesce(%s,enabled) "
        "WHERE country_code=%s RETURNING country_code,census_time,enabled",
        (census_time, enabled, country_code),
    )
    if row is not None:
        row["census_time"] = _hhmm(row["census_time"])
    return row


async def set_target(
    conn: psycopg.AsyncConnection, target_id: str, values: dict[str, Any]
) -> dict[str, Any] | None:
    """Change a target's pulse and census settings (keys from TARGET_FIELDS, pre-validated)."""
    unknown = set(values) - set(TARGET_FIELDS)
    if unknown or not values:
        raise ValueError("unknown or missing target fields")
    assignments = ",".join(f"{key}=%({key})s" for key in TARGET_FIELDS if key in values)
    return await _row(
        conn,
        f"UPDATE targets SET {assignments} WHERE target_id=%(target_id)s RETURNING target_id,"
        "census,census_concurrency,pulse_enabled,pulse_interval_minutes,pulse_requests,"
        "pulse_concurrency",
        values | {"target_id": target_id},
    )
