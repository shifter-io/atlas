"""The admin front page's readers: open census and pulse batches, the pulse heatmap, alerts,
jobs, upcoming census slots and the node grid. `dashboard` returns them all in one call.

The shared query helpers and the batch, traffic and schedule readers are in `views.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from ..batches import census_every, next_slot
from ..db import setting
from .views import _batch_runs, _hhmm, _now, _rate, _row, _rows, _totals

HEATMAP_SLOT = timedelta(minutes=30)
HEATMAP_SLOTS = 48


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


_NODE_FIELDS = (
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


async def _grid_rows(conn: psycopg.AsyncConnection) -> tuple[list[dict], list[dict], list[dict]]:
    """The regions, provider products and nodes the node grid is built from."""
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
    return regions, products, nodes


def _grid_key(row: dict, region_id: int) -> tuple[str, str, int]:
    return row["provider"], row["product"], region_id


def _grid_cell(product: dict, region: dict, node: dict | None, now: datetime) -> dict[str, Any]:
    """One product/region cell of the node grid; 'missing' when no node serves it."""
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
        cell |= {key: node[key] for key in _NODE_FIELDS}
        cell["heartbeat_age_s"] = int((now - beat).total_seconds()) if beat else None
    return cell


async def node_grid(conn: psycopg.AsyncConnection, now: datetime | None = None) -> dict:
    """Every product with a node against every region, including the cells with none."""
    now = _now(now)
    regions, products, nodes = await _grid_rows(conn)
    offline_after = int(await setting(conn, "node.offline_seconds"))
    index = {_grid_key(n, n["region_id"]): n for n in nodes}
    cells = [
        _grid_cell(product, region, index.get(_grid_key(product, region["region_id"])), now)
        for product in products
        for region in regions
    ]
    return {
        "now": now,
        "offline_after_s": offline_after,
        "regions": [r["slug"] for r in regions],
        "products": [f"{p['provider']}/{p['product']}" for p in products],
        "cells": cells,
    }
