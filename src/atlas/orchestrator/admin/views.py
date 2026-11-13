"""The admin UI's data layer: the shared query helpers and the batch, traffic and schedule readers.

The operator UI is the Astro app under src/pages/admin; it reaches these functions over the
JSON API in `routes.py` and never talks to the database itself. Readers return plain dicts and
lists (numbers as numbers, timestamps as datetimes) and the UI decides how to print them.
Everything here reads the live bookkeeping tables (test_batches, batch_runs, nodes, alerts,
job_runs, test_events, outages); none of it scans run_buckets or the calculated layer, so the
dashboard can poll without competing with ingest.

The front page's readers are in `views_dashboard.py` and the writers (kill, skip, acknowledge,
country and target edits) in `writers.py`; only the scheduler switches are written here, next
to the reader that shows them.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

import psycopg

from ..db import put_setting, setting

GB = 10**9
OPEN_RUNS = ("queued", "running", "uploading")
BATCH_STATUSES = ("scheduled", "running", "done", "failed", "skipped", "killed")
KINDS = ("census", "pulse", "load")


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


def _traffic_filters(
    start: datetime | None,
    end: datetime | None,
    test: str | None,
    provider: str | None,
    region: str | None,
) -> tuple[list[str], list[Any]]:
    """The WHERE clauses and their parameters for the traffic filters."""
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
    return clauses, params


async def _traffic_rows(
    conn: psycopg.AsyncConnection, clauses: list[str], params: list[Any]
) -> list[dict]:
    """Requests and wire bytes per test and provider product for the selected runs."""
    return await _rows(
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
    clauses, params = _traffic_filters(start, end, test, provider, region)
    rows = await _traffic_rows(conn, clauses, params)
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
