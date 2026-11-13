"""Admin JSON API under /admin/api, for the Astro operator UI (src/pages/admin).

Data and actions only: no templates and no formatting. Every reader is a thin wrapper over
`views` or `views_dashboard` and every action over `writers` (or `views.set_scheduler`),
which own every query, so the UI cannot see a shape the tests do not.

Authentication is HTTP Basic against ADMIN_USER / ADMIN_PASSWORD (the Astro server sends the
same pair). With no password the admin answers only over loopback, and only in --dev;
`app.serve` refuses to start without --dev and without a password, and should anything build
the app that way anyway, every /admin request is refused.

Actions require `Content-Type: application/json`: a browser cannot send that cross-origin
without a preflight, and no CORS headers are ever granted, so an operator's cached Basic
credential cannot be aimed at these endpoints by a third-party page.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from base64 import b64decode
from binascii import Error as BinasciiError
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from aiohttp import web

from ... import __version__
from ..api import DB_KEY
from . import views, views_dashboard, writers

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
COUNTRY = re.compile(r"^[A-Za-z]{2}$")
CLOCK = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
LIST_LIMIT = 500


@dataclass(frozen=True, slots=True)
class AdminAuth:
    """The expected `user:password`, or None for a password-less admin."""

    credential: str | None
    dev: bool


AUTH_KEY: web.AppKey[AdminAuth] = web.AppKey("admin_auth", AdminAuth)
SCHEDULER_KEY: web.AppKey[Any] = web.AppKey("admin_scheduler", object)


def credentials_from_env(env: dict[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    password = env.get("ADMIN_PASSWORD") or ""
    if not password:
        return None
    return f"{env.get('ADMIN_USER') or 'admin'}:{password}"


def _error(status: type[web.HTTPException], message: str, **headers: str) -> web.HTTPException:
    return status(
        text=json.dumps({"error": message}), content_type="application/json", headers=headers
    )


@web.middleware
async def admin_auth(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Basic auth on /admin, compared in constant time; loopback only without a password."""
    if not (request.path == "/admin" or request.path.startswith("/admin/")):
        return await handler(request)
    auth = request.app[AUTH_KEY]
    if auth.credential is None:
        if auth.dev and request.remote in LOOPBACK:
            return await handler(request)
        raise _error(
            web.HTTPServiceUnavailable,
            "ADMIN_PASSWORD is not set; the admin answers only over loopback in --dev",
        )
    scheme, _, encoded = request.headers.get("Authorization", "").partition(" ")
    presented = ""
    if scheme.lower() == "basic":
        try:
            presented = b64decode(encoded.strip(), validate=True).decode("utf-8", "replace")
        except (BinasciiError, ValueError):
            presented = ""
    if not hmac.compare_digest(presented.encode(), auth.credential.encode()):
        raise _error(
            web.HTTPUnauthorized,
            "authentication required",
            **{"WWW-Authenticate": 'Basic realm="Atlas admin", charset="UTF-8"'},
        )
    return await handler(request)


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date | time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, set | frozenset | tuple):
        return list(value)
    return str(value)


def _json(payload: Any) -> web.Response:
    return web.json_response(payload, dumps=lambda v: json.dumps(v, default=_default))


async def _body(request: web.Request) -> dict[str, Any]:
    if request.content_type != "application/json":
        raise _error(web.HTTPUnsupportedMediaType, "send application/json")
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise _error(web.HTTPBadRequest, "malformed JSON body") from None
    if not isinstance(payload, dict):
        raise _error(web.HTTPBadRequest, "body must be a JSON object")
    return payload


def _id(request: web.Request, name: str) -> int:
    return int(request.match_info[name])


def _choice(request: web.Request, name: str, allowed: tuple[str, ...]) -> str | None:
    value = request.query.get(name) or None
    if value is not None and value not in allowed:
        raise _error(web.HTTPBadRequest, f"{name} must be one of {', '.join(allowed)}")
    return value


def _country(value: Any, name: str = "country") -> str:
    if not isinstance(value, str) or not COUNTRY.match(value):
        raise _error(web.HTTPBadRequest, f"{name} must be a two-letter country code")
    return value.upper()


def _moment(request: web.Request, name: str) -> datetime | None:
    value = request.query.get(name) or None
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise _error(web.HTTPBadRequest, f"{name} must be an ISO date or timestamp") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise _error(web.HTTPBadRequest, f"{name} must be true or false")
    return value


def _int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if type(value) is not int or not low <= value <= high:
        raise _error(web.HTTPBadRequest, f"{name} must be an integer from {low} to {high}")
    return value


# --- readers -----------------------------------------------------------------------------


async def health(request: web.Request) -> web.Response:
    async with request.app[DB_KEY].connection() as conn:
        await conn.execute("SELECT 1")
    scheduler = request.app.get(SCHEDULER_KEY)
    return _json(
        {
            "ok": True,
            "version": __version__,
            "scheduler": scheduler is not None,
            "jobs": sorted(scheduler.tasks) if scheduler is not None else [],
            "now": datetime.now(UTC),
        }
    )


async def dashboard(request: web.Request) -> web.Response:
    async with request.app[DB_KEY].connection() as conn:
        return _json(await views_dashboard.dashboard(conn))


async def batches(request: web.Request) -> web.Response:
    kind = _choice(request, "kind", views.KINDS)
    status = _choice(request, "status", views.BATCH_STATUSES)
    country = request.query.get("country") or None
    if country is not None:
        country = _country(country)
    limit = _int(request.query.get("limit", "100"), "limit", 1, LIST_LIMIT)
    async with request.app[DB_KEY].connection() as conn:
        rows = await views.batch_list(conn, kind=kind, status=status, country=country, limit=limit)
    return _json(
        {
            "filters": {"kind": kind, "status": status, "country": country, "limit": limit},
            "batches": rows,
        }
    )


async def batch(request: web.Request) -> web.Response:
    batch_id = _id(request, "batch_id")
    async with request.app[DB_KEY].connection() as conn:
        detail = await views.batch_detail(conn, batch_id)
    if detail is None:
        raise _error(web.HTTPNotFound, f"no batch {batch_id}")
    return _json(detail)


async def schedule(request: web.Request) -> web.Response:
    async with request.app[DB_KEY].connection() as conn:
        return _json(await views.schedule(conn))


async def nodes(request: web.Request) -> web.Response:
    async with request.app[DB_KEY].connection() as conn:
        return _json(await views_dashboard.node_grid(conn))


async def traffic(request: web.Request) -> web.Response:
    start, end = _moment(request, "from"), _moment(request, "to")
    if start is not None and end is not None and end <= start:
        raise _error(web.HTTPBadRequest, "to must be after from")
    test = _choice(request, "test", views.KINDS)
    provider = request.query.get("provider") or None
    region = request.query.get("region") or None
    async with request.app[DB_KEY].connection() as conn:
        report = await views.traffic(
            conn, start=start, end=end, test=test, provider=provider, region=region
        )
        report["choices"] = await views.traffic_choices(conn)
    return _json(report)


async def jobs(request: web.Request) -> web.Response:
    scheduler = request.app.get(SCHEDULER_KEY)
    async with request.app[DB_KEY].connection() as conn:
        latest = await views_dashboard.job_health(conn)
        recent = await views_dashboard.recent_jobs(conn, 50)
    return _json(
        {
            "jobs": latest,
            "recent": recent,
            "schedule": scheduler.upcoming() if scheduler is not None else [],
        }
    )


# --- actions -----------------------------------------------------------------------------


async def _stop(request: web.Request, status: str) -> web.Response:
    await _body(request)
    batch_id = _id(request, "batch_id")
    try:
        result = await writers.stop_batch(request.app[DB_KEY], batch_id, status)
    except writers.Conflict as exc:
        raise _error(web.HTTPConflict, str(exc)) from None
    if result is None:
        raise _error(web.HTTPNotFound, f"no batch {batch_id}")
    return _json(result)


async def kill_batch(request: web.Request) -> web.Response:
    """Stop a batch now; its nodes see stop on their next progress post or /v1/work."""
    return await _stop(request, "killed")


async def skip_batch(request: web.Request) -> web.Response:
    """Skip a batch (e.g. a census that would run into the next slot); open runs are killed."""
    return await _stop(request, "skipped")


async def acknowledge_alert(request: web.Request) -> web.Response:
    await _body(request)
    alert_id = _id(request, "alert_id")
    async with request.app[DB_KEY].connection() as conn:
        fresh = await writers.acknowledge(conn, alert_id)
    if fresh is None:
        raise _error(web.HTTPNotFound, f"no alert {alert_id}")
    return _json({"alert_id": alert_id, "acknowledged": True, "already": not fresh})


async def run_job(request: web.Request) -> web.Response:
    """Fire a scheduled job by hand, when waiting for the next fire time will not do."""
    body = await _body(request)
    job = body.get("job")
    if not isinstance(job, str) or not job:
        raise _error(web.HTTPBadRequest, "missing 'job'")
    scheduler = request.app.get(SCHEDULER_KEY)
    if scheduler is None:
        raise _error(web.HTTPServiceUnavailable, "no scheduler in this process")
    if job not in scheduler.tasks:
        raise _error(web.HTTPNotFound, f"no job named {job!r}")
    result = await scheduler.run_now(job)
    return _json({"ran": job, "result": result, "last": scheduler.tasks[job].last})


async def set_country(request: web.Request) -> web.Response:
    body = await _body(request)
    unknown = set(body) - {"cc", "census_time", "enabled"}
    if unknown:
        raise _error(web.HTTPBadRequest, f"unknown fields: {', '.join(sorted(unknown))}")
    cc = _country(body.get("cc"), "cc")
    census_time = None
    if "census_time" in body:
        value = body["census_time"]
        match = CLOCK.match(value) if isinstance(value, str) else None
        if match is None:
            raise _error(web.HTTPBadRequest, "census_time must be HH:MM (UTC, 00:00-23:59)")
        census_time = time(int(match[1]), int(match[2]))
    enabled = _bool(body["enabled"], "enabled") if "enabled" in body else None
    if census_time is None and enabled is None:
        raise _error(web.HTTPBadRequest, "send census_time and/or enabled")
    async with request.app[DB_KEY].connection() as conn:
        row = await writers.set_country(conn, cc, census_time=census_time, enabled=enabled)
    if row is None:
        raise _error(web.HTTPNotFound, f"no country {cc}")
    return _json(row)


async def set_target(request: web.Request) -> web.Response:
    body = await _body(request)
    target_id = body.get("target_id")
    if not isinstance(target_id, str) or not target_id:
        raise _error(web.HTTPBadRequest, "missing 'target_id'")
    fields = {key: value for key, value in body.items() if key != "target_id"}
    unknown = set(fields) - set(writers.TARGET_FIELDS)
    if unknown:
        raise _error(web.HTTPBadRequest, f"unknown fields: {', '.join(sorted(unknown))}")
    if not fields:
        raise _error(web.HTTPBadRequest, "send at least one field to change")
    values: dict[str, Any] = {}
    for key, value in fields.items():
        bounds = writers.TARGET_FIELDS[key]
        values[key] = _bool(value, key) if bounds is None else _int(value, key, *bounds)
    async with request.app[DB_KEY].connection() as conn:
        row = await writers.set_target(conn, target_id, values)
    if row is None:
        raise _error(web.HTTPNotFound, f"no target {target_id!r}")
    return _json(row)


async def set_scheduler(request: web.Request) -> web.Response:
    """Switch the census and/or pulse planner: {"census": bool?, "pulse": bool?}."""
    body = await _body(request)
    unknown = set(body) - set(views.SCHEDULER_SWITCHES)
    if unknown:
        raise _error(web.HTTPBadRequest, f"unknown fields: {', '.join(sorted(unknown))}")
    if not body:
        raise _error(web.HTTPBadRequest, "send census and/or pulse")
    values = {key: _bool(value, key) for key, value in body.items()}
    async with request.app[DB_KEY].connection() as conn:
        return _json(await views.set_scheduler(conn, values))


def setup(
    app: web.Application,
    *,
    dev: bool = False,
    credential: str | None = None,
    scheduler: Any = None,
) -> None:
    """Mount /admin/api on the orchestrator app with its auth middleware."""
    app[AUTH_KEY] = AdminAuth(credential=credential, dev=dev)
    if scheduler is not None:
        app[SCHEDULER_KEY] = scheduler
    app.middlewares.append(admin_auth)
    add = app.router
    add.add_get("/admin/api/health", health)
    add.add_get("/admin/api/dashboard", dashboard)
    add.add_get("/admin/api/batches", batches)
    add.add_get("/admin/api/batches/{batch_id:\\d+}", batch)
    add.add_get("/admin/api/schedule", schedule)
    add.add_get("/admin/api/nodes", nodes)
    add.add_get("/admin/api/traffic", traffic)
    add.add_get("/admin/api/jobs", jobs)
    add.add_post("/admin/api/batches/{batch_id:\\d+}/kill", kill_batch)
    add.add_post("/admin/api/batches/{batch_id:\\d+}/skip", skip_batch)
    add.add_post("/admin/api/alerts/{alert_id:\\d+}/ack", acknowledge_alert)
    add.add_post("/admin/api/jobs/run", run_job)
    add.add_post("/admin/api/schedule/country", set_country)
    add.add_post("/admin/api/schedule/target", set_target)
    add.add_post("/admin/api/schedule/scheduler", set_scheduler)
