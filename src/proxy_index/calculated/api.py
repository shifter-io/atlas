"""Local read-only JSON API backed exclusively by immutable prepared snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json

from aiohttp import web

from .. import detect, traffic
from . import store
from .aggregate import iso

RESOURCES = {
    "products",
    "targets",
    "scores",
    "series",
    "breakdowns",
    "overlap",
    "methodology",
    "fleet",
    "runs",
}
DATABASE = web.AppKey("database", object)
EVENT_PARAMETERS = {"product", "kind", "category", "since", "limit"}
TRAFFIC_PARAMETERS = {"test", "provider", "country", "region", "from", "to"}
CATEGORIES = {"census", "pulse", "incident", "audit"}
MAX_EVENTS = 500
LIVE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def project(payload, resource, query):
    window = query.get("window", "30d")
    if window not in payload["periods"]:
        raise ValueError("window must be 24h, 7d or 30d")
    unknown = set(query) - {"window", "product", "cohort"}
    if unknown:
        raise ValueError("Unknown query parameter")
    products = [
        p
        for p in payload["products"]
        if (not query.get("product") or str(p["product_id"]) == query["product"])
        and (not query.get("cohort") or p["cohort"] == query["cohort"])
    ]
    envelope = {k: payload[k] for k in ("snapshot_id", "as_of", "methodology", "readiness")}
    envelope["methodology"] = {"id": payload["methodology"]["id"]}
    envelope["measurement_period"] = payload["periods"][window]
    if resource == "products":
        data = [
            {k: v for k, v in p.items() if k != "windows"} | {"result": p["windows"][window]}
            for p in products
        ]
    elif resource in ("scores", "series", "targets", "breakdowns"):
        key = "pool" if resource == "breakdowns" else resource
        data = [
            {
                "product_id": p["product_id"],
                "cohort": p["cohort"],
                "readiness": p["windows"][window]["readiness"],
                resource: p["windows"][window][key],
            }
            # v2: success, latency and outcomes per country, region, city, ASN, country x ASN,
            # each capped to its top rows with the full row count in dimension_entries.
            | (
                {
                    "dimensions": p["windows"][window]["breakdowns"],
                    "dimension_entries": p["windows"][window].get("breakdown_entries"),
                }
                if resource == "breakdowns" and "breakdowns" in p["windows"][window]
                else {}
            )
            for p in products
        ]
    elif resource in ("fleet", "overlap"):
        data = [
            r
            for r in payload[resource]
            if r["window"] == window and (not query.get("cohort") or r["cohort"] == query["cohort"])
        ]
        if query.get("product") and resource == "overlap":
            data = [
                r for r in data if query["product"] in (str(r["product_a"]), str(r["product_b"]))
            ]
    else:
        data = payload[resource]
    return envelope | {"data": data}


async def handle(request):
    snapshot = request.match_info.get("snapshot", "current")
    if snapshot != "current" and (not snapshot.isdigit() or len(snapshot) > 18):
        raise web.HTTPBadRequest(text="Invalid snapshot id")
    token = await asyncio.to_thread(store.head, request.app[DATABASE], snapshot)
    if token is None:
        raise web.HTTPNotFound(text="Snapshot unavailable")
    resource = request.match_info.get("resource")
    if resource and resource not in RESOURCES:
        raise web.HTTPNotFound()
    etag = (
        '"'
        + hashlib.sha256(
            (token[1] + str(resource) + str(sorted(request.query.items()))).encode()
        ).hexdigest()
        + '"'
    )
    headers = {
        "ETag": etag,
        "Cache-Control": "no-cache"
        if snapshot == "current"
        else "public,max-age=31536000,immutable",
        "X-Snapshot-Id": str(token[0]),
        "X-Content-Type-Options": "nosniff",
    }
    if request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers=headers)
    # Pin the body to the token's immutable version if publication moves between reads.
    payload = await asyncio.to_thread(store.read, request.app[DATABASE], str(token[0]))
    try:
        result = project(payload, resource, request.query) if resource else payload
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    body = json.dumps(result, separators=(",", ":"), allow_nan=False).encode()
    return web.Response(body=body, content_type="application/json", headers=headers)


def _live(database, query, *args, **kwargs):
    """A read-only query over live history (events and traffic are not snapshot projections)."""
    with store.connect(database) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        return query(conn, *args, **kwargs)


def _json(value):
    body = json.dumps(value, separators=(",", ":"), allow_nan=False, default=iso).encode()
    return web.Response(body=body, content_type="application/json", headers=LIVE)


async def events(request):
    """GET /v1/events?product=&kind=&category=&since=&limit= — newest first, {"events": [...]}.

    product is a provider slug or "provider/product"; kind a comma-separated list; since an ISO
    time (exclusive; empty means none); limit 1-500 (default 100).
    """
    query = request.query
    try:
        if set(query) - EVENT_PARAMETERS:
            raise ValueError("Unknown query parameter")
        limit = int(query.get("limit") or 100)
        if not 1 <= limit <= MAX_EVENTS:
            raise ValueError(f"limit must be between 1 and {MAX_EVENTS}")
        category = query.get("category") or None
        if category is not None and category not in CATEGORIES:
            raise ValueError("category must be census, pulse, incident or audit")
        filters = {
            "product": query.get("product") or None,
            "kinds": [k for k in query.get("kind", "").split(",") if k] or None,
            "category": category,
            "since": traffic.parse_time(query.get("since")),
            "limit": limit,
        }
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    rows = await asyncio.to_thread(_live, request.app[DATABASE], detect.list_events, **filters)
    return _json({"events": rows})


async def traffic_report(request):
    """GET /v1/traffic?test=&provider=&country=&region=&from=&to= — see traffic.summary."""
    query = request.query
    try:
        if set(query) - TRAFFIC_PARAMETERS:
            raise ValueError("Unknown query parameter")
        test = query.get("test") or None
        if test is not None and test not in traffic.TESTS:
            raise ValueError("test must be census, pulse or load")
        filters = {
            "test": test,
            "provider": query.get("provider") or None,
            "country": query.get("country") or None,
            "region": query.get("region") or None,
            "start": traffic.parse_time(query.get("from")),
            "end": traffic.parse_time(query.get("to")),
        }
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    report = await asyncio.to_thread(_live, request.app[DATABASE], traffic.summary, **filters)
    return _json(report)


def app(database):
    application = web.Application()
    application[DATABASE] = database
    application.router.add_get("/v1/events", events)
    application.router.add_get("/v1/traffic", traffic_report)
    application.router.add_get("/v1/snapshots/{snapshot}", handle)
    application.router.add_get("/v1/snapshots/{snapshot}/{resource}", handle)
    application.router.add_get("/v1/{resource}", handle)
    return application


def serve(database, host="127.0.0.1", port=8081):
    web.run_app(app(database), host=host, port=port, print=None)
