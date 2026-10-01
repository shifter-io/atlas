"""Local read-only JSON API backed exclusively by immutable prepared snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json

from aiohttp import web

from . import store

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


def app(database):
    application = web.Application()
    application[DATABASE] = database
    application.router.add_get("/v1/snapshots/{snapshot}", handle)
    application.router.add_get("/v1/snapshots/{snapshot}/{resource}", handle)
    application.router.add_get("/v1/{resource}", handle)
    return application


def serve(database, host="127.0.0.1", port=8081):
    web.run_app(app(database), host=host, port=port, print=None)
