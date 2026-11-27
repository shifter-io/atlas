"""The node API (/v1/*), per the census/pulse contract.

Nodes hold provider credentials; the orchestrator never sends a proxy URL. Each node presents
its own bearer token (stored as sha256 in nodes.token_hash), which binds it to one region,
provider and product: a node can only see, report and upload its own runs.

The browser port (BROWSER-MTLS-1) serves only these routes, to a browser host that presents a
client certificate of the manager's browser CA (the TLS handshake checks it) whose subject CN
is the logical host name its token is bound to; anything else there is 404 or 403. When the
process has a browser port, the node port refuses browser host tokens (403), so a browser token
never works without its client certificate. The admin API has a listener of its own
(ADMIN_PORT_KEY, loopback by default): the node and browser ports answer 404 for /admin, and
the admin port answers 404 for anything else.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import ssl
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import psycopg
from aiohttp import web

from .. import storage
from ..browser.contracts import BROWSER_KIND, BROWSER_REGION, ERROR_CHECKSUM_MISMATCH, HEADER_SHA256
from ..regions import resolve_region
from . import nodework, pulse_enrich, results_watch
from .archive import ArchiveError, check_rows, unpack
from .db import Database, setting
from .events import raise_alert
from .nodes import token_hash

log = logging.getLogger(__name__)

DB_KEY: web.AppKey[Database] = web.AppKey("db", Database)
CREDENTIALS_KEY: web.AppKey[str] = web.AppKey("credentials_file", str)
# The upload path's database file (SEC-INGEST-ROLE-1): a login that may only add run data; the
# run-time one (CREDENTIALS_KEY) when unset. A missing day's partitions are made with the latter.
INGEST_CREDENTIALS_KEY: web.AppKey[str] = web.AppKey("ingest_credentials_file", str)
NODE_KEY: web.RequestKey[dict[str, Any]] = web.RequestKey("node", dict)
# The browser listener's TLS context: a request whose connection uses it came in on that port.
BROWSER_TLS_KEY: web.AppKey[ssl.SSLContext] = web.AppKey("browser_tls", ssl.SSLContext)
# The admin listener's port: set, /admin is served there alone and nothing else is.
ADMIN_PORT_KEY: web.AppKey[int] = web.AppKey("admin_port", int)
CHUNK = 1 << 20
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
# INGEST-HANG-1: an upload's import waits at most this long for a lock (its networks', held by
# another import), then answers 503 with RETRY_AFTER; at most `results.ingest_concurrency`
# uploads are received and imported at once by this process, the next answered the same way
# before its body is read. Waiting uploads never hold the executor's threads, the database's
# connections, or the memory their bodies take (/tmp is memory on the manager), without bound.
UPLOAD_LOCK_WAIT = "30s"
RETRY_AFTER = 30
_uploads = [0]  # census and pulse uploads this process is receiving or importing now


def _error(status: type[web.HTTPException], message: str) -> web.HTTPException:
    return status(text=json.dumps({"error": message}), content_type="application/json")


def _busy(
    retry_after: int, message: str, status: type[web.HTTPException] = web.HTTPTooManyRequests
) -> web.HTTPException:
    return status(
        text=json.dumps({"error": message}),
        content_type="application/json",
        headers={"Retry-After": str(retry_after)},
    )


def _browser_listener(request: web.Request) -> bool:
    """True when the request came in on the browser port (its TLS context is the app's)."""
    context = request.app.get(BROWSER_TLS_KEY)
    transport = request.transport
    tls = transport.get_extra_info("ssl_object") if transport is not None else None
    return context is not None and tls is not None and tls.context is context


def _admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


def _check_listener(request: web.Request) -> None:
    """404 for an admin path off the admin port, and for any other path on it."""
    port = request.app.get(ADMIN_PORT_KEY)
    if port is None:
        return
    transport = request.transport
    name = transport.get_extra_info("sockname") if transport is not None else None
    on_admin = isinstance(name, tuple) and len(name) >= 2 and name[1] == port
    if on_admin != _admin_path(request.path):
        raise _error(web.HTTPNotFound, "not found")


def _peer_name(request: web.Request) -> str | None:
    """The subject CN of the client certificate the handshake verified, or None."""
    transport = request.transport
    peer = transport.get_extra_info("peercert") if transport is not None else None
    for entry in (peer or {}).get("subject", ()):
        for key, value in entry:
            if key == "commonName":
                return value
    return None


def _check_browser_peer(request: web.Request, node: dict[str, Any]) -> None:
    """On the browser port the token is a browser host's and the certificate names that host."""
    if node["region"] != BROWSER_REGION:
        raise _error(web.HTTPForbidden, "the browser port takes only browser host tokens")
    if node["host"] is None or _peer_name(request) != node["host"]:
        raise _error(web.HTTPForbidden, "the client certificate is not the token's host")


@web.middleware
async def node_auth(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Bearer token on every /v1/ route; the node row rides on the request. The browser port
    serves nothing else and also checks the client certificate against the token."""
    _check_listener(request)
    browser = _browser_listener(request)
    if not request.path.startswith("/v1/"):
        if browser:
            raise _error(web.HTTPNotFound, "the browser port serves only the node API")
        return await handler(request)
    scheme, _, presented = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not presented.strip():
        raise _error(web.HTTPUnauthorized, "bad or missing bearer token")
    async with request.app[DB_KEY].connection() as conn:
        node = await (
            await conn.execute(
                "SELECT n.node_id,n.region_id,r.slug AS region,n.provider,n.product,n.host,"
                "p.product_id FROM nodes n JOIN runner_regions r ON r.region_id=n.region_id "
                "LEFT JOIN products p ON p.provider=n.provider AND p.name=n.product "
                "WHERE n.token_hash=%s",
                (token_hash(presented.strip()),),
            )
        ).fetchone()
    if node is None:
        raise _error(web.HTTPUnauthorized, "bad or missing bearer token")
    if browser:
        _check_browser_peer(request, node)
    elif node["region"] == BROWSER_REGION and BROWSER_TLS_KEY in request.app:
        raise _error(web.HTTPForbidden, "a browser host token is accepted only on the browser port")
    request[NODE_KEY] = node
    return await handler(request)


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise _error(web.HTTPBadRequest, "body must be JSON") from None
    if not isinstance(body, dict):
        raise _error(web.HTTPBadRequest, "body must be a JSON object")
    return body


def _batch_id(value: Any) -> int:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if type(value) is not int or value <= 0:
        raise _error(web.HTTPBadRequest, "batch_id must be a positive integer")
    return value


def _metric(body: dict[str, Any], key: str, kind: type) -> Any:
    value = body.get(key)
    if value is None:
        return None
    if kind is float and type(value) is int:
        value = float(value)
    if type(value) is not kind or value < 0:
        raise _error(web.HTTPBadRequest, f"{key} must be a non-negative number")
    return value


async def health(request: web.Request) -> web.Response:
    """Unauthenticated, for a load balancer."""
    try:
        async with request.app[DB_KEY].connection() as conn:
            await conn.execute("SELECT 1")
    except (OSError, psycopg.Error) as exc:
        return web.json_response({"ok": False, "error": type(exc).__name__}, status=503)
    return web.json_response({"ok": True})


async def register(request: web.Request) -> web.Response:
    node = request[NODE_KEY]
    body = await _body(request)
    if body.get("provider") != node["provider"] or body.get("product") != node["product"]:
        raise _error(web.HTTPForbidden, "token belongs to another provider or product")
    named = body.get("region") or body.get("host")
    if not isinstance(named, str) or not named:
        raise _error(web.HTTPBadRequest, "region or host is required")
    try:
        region = resolve_region(named)
    except ValueError:
        raise _error(web.HTTPBadRequest, "region or host matches no runner region") from None
    if region != node["region"]:
        raise _error(web.HTTPForbidden, f"token belongs to region {node['region']}")
    if region == BROWSER_REGION:  # D2: a browser token is bound to its host's logical name
        from .browser_ingest import check_host

        async with request.app[DB_KEY].connection() as conn:
            await check_host(conn, node, body.get("host"))
    values: dict[str, Any] = {}
    if isinstance(body.get("host"), str):
        values["host"] = body["host"][:255]
    if isinstance(body.get("version"), str):
        values["version"] = body["version"][:64]
    async with request.app[DB_KEY].transaction() as conn:
        await nodework.online(conn, node["node_id"], values)
    return web.json_response({"node_id": node["node_id"], "region": node["region"]})


async def heartbeat(request: web.Request) -> web.Response:
    node = request[NODE_KEY]
    body = await _body(request)
    values = {
        "open_conns": _metric(body, "open_conns", int),
        "req_per_s": _metric(body, "req_per_s", float),
        "rss_mb": _metric(body, "rss_mb", int),
        # PULSE-STATE-2: the host's pressure (node_health); None from an older node.
        "io_pressure": _metric(body, "io_pressure", float),
        "mem_available_mb": _metric(body, "mem_available_mb", int),
        "swap_pages": _metric(body, "swap_pages", int),
    }
    if isinstance(body.get("version"), str):
        values["version"] = body["version"][:64]
    async with request.app[DB_KEY].transaction() as conn:
        await nodework.online(conn, node["node_id"], values)
    return web.json_response({"ok": True})


async def work(request: web.Request) -> web.Response:
    return web.json_response(await nodework.assignments(request.app[DB_KEY], request[NODE_KEY]))


async def progress(request: web.Request) -> web.Response:
    body = await _body(request)
    counters = body.get("counters")
    if not isinstance(counters, dict):
        raise _error(web.HTTPBadRequest, "counters must be an object")
    try:
        result = await nodework.progress(
            request.app[DB_KEY], request[NODE_KEY], _batch_id(body.get("batch_id")), counters
        )
    except ValueError as exc:
        raise _error(web.HTTPBadRequest, str(exc)) from None
    if result is None:
        raise _error(web.HTTPNotFound, "no such run for this node")
    return web.json_response(result)


async def failed(request: web.Request) -> web.Response:
    body = await _body(request)
    error = body.get("error")
    if not isinstance(error, str) or not error:
        raise _error(web.HTTPBadRequest, "error must be a non-empty string")
    status = await nodework.fail_run(
        request.app[DB_KEY], request[NODE_KEY], _batch_id(body.get("batch_id")), error
    )
    if status is None:
        raise _error(web.HTTPNotFound, "no such run for this node")
    return web.json_response({"ok": True, "status": status})


def _write(stream: Any, chunk: bytes, sha: Any, md5: Any) -> None:
    stream.write(chunk)
    sha.update(chunk)
    md5.update(chunk)


async def _receive(request: web.Request, path: Path, limit: int, seconds: float) -> dict[str, Any]:
    """Stream the body to path, hashing it: its archive.export.file_digests (bytes, sha256,
    md5) without reading it again. A body not received within `seconds` is 408 and the file
    goes."""
    sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
    stream = await asyncio.to_thread(path.open, "xb")
    try:
        async with asyncio.timeout(seconds):
            size = await _stream(request, stream, limit, sha, md5)
    except TimeoutError:
        await asyncio.to_thread(path.unlink, missing_ok=True)
        raise _error(web.HTTPRequestTimeout, "upload_timeout") from None
    finally:
        await asyncio.to_thread(stream.close)
    return {"bytes": size, "sha256": sha.hexdigest(), "md5": md5.hexdigest()}


async def _stream(request: web.Request, stream: Any, limit: int, sha: Any, md5: Any) -> int:
    size = 0
    async for chunk in request.content.iter_chunked(CHUNK):
        size += len(chunk)
        if size > limit:
            raise web.HTTPRequestEntityTooLarge(
                limit,
                size,
                text=json.dumps({"error": "upload exceeds the size limit"}),
                content_type="application/json",
            )
        await asyncio.to_thread(_write, stream, chunk, sha, md5)
    return size


def _sent_sha256(request: web.Request, *, required: bool) -> str | None:
    """The sender's X-Content-SHA256 (K-BP7), lower-cased; required for browser bundles."""
    value = request.headers.get(HEADER_SHA256)
    if value is None and not required:
        return None
    if value is None or not re.fullmatch(r"[0-9a-fA-F]{64}", value.strip()):
        raise _error(web.HTTPBadRequest, f"{HEADER_SHA256} must be 64 hex digits")
    return value.strip().lower()


def _check_sha256(sent: str | None, digests: dict[str, Any]) -> None:
    if sent is not None and sent != digests["sha256"]:
        raise _error(web.HTTPBadRequest, ERROR_CHECKSUM_MISMATCH)


def _open_upload(archive: Path, directory: Path, limit: int) -> Any:
    directory.mkdir(mode=0o700)
    unpack(archive, directory, max_bytes=limit)
    return json.loads((directory / "summary.json").read_text())


async def _reject(request: web.Request, batch_id: int, reason: str, kind: str) -> None:
    """A run whose upload cannot be ingested fails now rather than waiting for the reaper."""
    db, node = request.app[DB_KEY], request[NODE_KEY]
    await nodework.fail_run(db, node, batch_id, reason)
    async with db.transaction() as conn:
        await raise_alert(
            conn,
            kind,
            f"node {node['node_id']} uploaded results that were rejected",
            severity="major",
            dedupe_key=f"{kind}:{node['node_id']}",
            data={"batch_id": batch_id, "node_id": node["node_id"], "reason": reason[:300]},
        )


async def results(request: web.Request) -> web.Response:
    """A finished run directory (tar.gz): extract, ingest, link run_key, then the barrier. Each
    upload and its answer are counted for the stall alert (results_watch, INGEST-HANG-2)."""
    watch, status = results_watch.WATCH, 500  # an error or a node gone: an upload not answered
    watch.began()
    try:
        response = await _results(request)
        status = response.status
        return response
    except web.HTTPException as exc:
        status = exc.status
        raise
    finally:
        watch.ended(status)


async def _results(request: web.Request) -> web.Response:
    db, node = request.app[DB_KEY], request[NODE_KEY]
    batch_id = _batch_id(request.query.get("batch_id"))
    run = await nodework.begin_upload(db, node, batch_id)
    if run is None:
        raise _error(web.HTTPNotFound, "no such run for this node")
    if run["kind"] == BROWSER_KIND:  # K-BP7: archived per run, so no generic raw spool
        from .browser_ingest import ingest_bundle

        return await ingest_bundle(request, run, node)
    sent = _sent_sha256(request, required=False)
    answered = _answered(run)
    if answered is not None:
        return answered
    async with db.connection() as conn:
        concurrency = int(await setting(conn, "results.ingest_concurrency"))
    if _uploads[0] >= concurrency:
        raise _busy(RETRY_AFTER, "ingest_busy", web.HTTPServiceUnavailable)
    _uploads[0] += 1
    try:
        return await _upload(request, run, batch_id, sent)
    finally:
        _uploads[0] -= 1


async def _upload(
    request: web.Request, run: dict[str, Any], batch_id: int, sent: str | None
) -> web.Response:
    """Receive, check, import and spool one census or pulse upload, then finish its run."""
    db, node = request.app[DB_KEY], request[NODE_KEY]
    async with db.connection() as conn:
        limits = (
            int(await setting(conn, "results.max_upload_bytes")),
            float(await setting(conn, "results.upload_timeout_seconds")),
        )
        bounds = (
            int(await setting(conn, "results.max_extracted_bytes")),
            int(await setting(conn, "results.max_artifact_rows")),
        )
        spool_dir = await setting(conn, "raw.spool_dir")
    with tempfile.TemporaryDirectory(prefix="atlas-upload-") as temporary:
        archive, directory = Path(temporary, "run.tar.gz"), Path(temporary, "run")
        _check_sha256(sent, await _receive(request, archive, *limits))
        summary = await _summary(request, run, archive, directory, bounds)
        pulse = run["kind"] == "pulse"
        if pulse:  # its exit IPs with the manager's DB-IP, in the counters (PULSE-COUNTERS-1)
            enricher = request.app.get(pulse_enrich.ENRICHER_KEY)
            await pulse_enrich.enrich(db, enricher, batch_id, directory, summary)
        imported = await _ingest(request, batch_id, directory, ip_rows=not pulse, counters=pulse)
        if spool_dir:  # the raw file this update used (ARCHIVE-1: 30 days here, then Spaces)
            await _spool(db, spool_dir, run, summary, archive)
    status = await nodework.finish_run(db, node, batch_id, summary, imported["run_key"])
    return web.json_response(
        {
            "ok": True,
            "ingested": True,
            "status": status,
            "run_key": imported["run_key"],
            "imported": imported["imported"],
        }
    )


def _answered(run: dict[str, Any]) -> web.Response | None:
    """The reply to an upload for a run that takes no data now; None when it does."""
    if run["status"] in ("killed", "skipped"):
        # An operator stopped it: the node discards the data.
        return web.json_response({"ok": True, "ingested": False, "status": run["status"]})
    if run["status"] in ("done", "failed") and run["run_key"] is not None:
        # A retry after a lost response.
        return web.json_response(
            {"ok": True, "ingested": True, "status": run["status"], "run_key": run["run_key"]}
        )
    if run["status"] not in ("running", "uploading"):
        raise _error(web.HTTPConflict, f"run is {run['status']}")
    return None


async def _summary(
    request: web.Request,
    run: dict[str, Any],
    archive: Path,
    directory: Path,
    bounds: tuple[int, int],
) -> dict[str, Any]:
    """Extract the upload and check its summary.json is this node's run, within `bounds`
    (extracted bytes, declared artifact rows): 400 and a failed run when it is not."""
    extracted, rows = bounds
    try:
        summary = await asyncio.to_thread(_open_upload, archive, directory, extracted)
        _check_summary(summary, request[NODE_KEY], run)
        check_rows(summary, rows)
    except (ArchiveError, json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
        reason = str(exc) if isinstance(exc, ArchiveError) else "unreadable summary.json"
        await _reject(request, run["batch_id"], f"bad archive: {reason}", "results_rejected")
        raise _error(web.HTTPBadRequest, reason) from None
    return summary


def _check_summary(summary: Any, node: dict[str, Any], run: dict[str, Any]) -> None:
    if not isinstance(summary, dict):
        raise ArchiveError("summary.json must be an object")
    config, target = summary.get("config") or {}, summary.get("target") or {}
    if (config.get("provider"), config.get("product")) != (node["provider"], node["product"]):
        raise ArchiveError("results belong to another provider or product")
    if target.get("id") != run["target_id"]:
        raise ArchiveError("results are for another target")


async def _ingest(
    request: web.Request, batch_id: int, directory: Path, *, ip_rows: bool, counters: bool = False
):
    """storage.ingest of an upload's run directory with the ingest login: 503 when the database is
    unreachable or a lock stays held past UPLOAD_LOCK_WAIT (the node keeps its file and
    retries), 422 and a failed run when the data is refused."""
    runtime = request.app[CREDENTIALS_KEY]
    login = request.app.get(INGEST_CREDENTIALS_KEY, runtime)
    try:
        return await asyncio.to_thread(
            storage.ingest,
            directory,
            login,
            batch_id=batch_id,
            ip_rows=ip_rows,
            counters=counters,
            partitions_file=None if login == runtime else runtime,
            lock_wait=UPLOAD_LOCK_WAIT,
        )
    except psycopg.errors.LockNotAvailable:
        log.warning("ingest of batch %s waited %s for a lock; retry", batch_id, UPLOAD_LOCK_WAIT)
        raise _busy(RETRY_AFTER, "ingest_busy", web.HTTPServiceUnavailable) from None
    except psycopg.OperationalError:
        log.exception("ingest of batch %s could not reach the database", batch_id)
        raise _error(web.HTTPServiceUnavailable, "database unavailable; retry") from None
    except (ValueError, KeyError, TypeError, psycopg.Error) as exc:
        log.warning("ingest of batch %s rejected: %s", batch_id, exc)
        await _reject(request, batch_id, f"ingest: {exc}"[:500], "ingest_failed")
        raise web.HTTPUnprocessableEntity(
            text=json.dumps({"error": "ingest_rejected", "reason": _reason(exc)}),
            content_type="application/json",
        ) from None


def _reason(exc: Exception) -> str:
    """The class of a refused ingest, for the node: the detail (database text included) goes
    to the log and the run's error only."""
    if isinstance(exc, psycopg.Error):
        return "database_refused"
    return {KeyError: "missing_field", TypeError: "wrong_type"}.get(type(exc), "invalid_data")


async def _spool(db: Database, root: str, run: dict[str, Any], summary: dict, archive):
    """Keep the upload's raw file. The import has committed, so a failure only raises an alert:
    the run is still finished and linked."""
    from datetime import UTC, datetime

    from ..archive.rawfiles import try_spool

    kind, batch_id = run["kind"], run["batch_id"]
    name = f"{kind}-b{batch_id}-{summary.get('run_id')}.tar.gz"
    network = summary["config"]["provider"]  # the node's own (_check_summary)
    moment = datetime.now(UTC)
    issue = await asyncio.to_thread(try_spool, root, moment, network, name, archive, move=True)
    if issue:
        log.warning("raw file of batch %s not spooled: %s", batch_id, issue)
        async with db.transaction() as conn:
            await raise_alert(
                conn,
                "raw_spool_failed",
                f"the raw file of batch {batch_id} was not kept ({issue})",
                severity="major",
                dedupe_key="raw_spool_failed:results",
                data={"batch_id": batch_id, "error": issue},
            )


def add_routes(app: web.Application) -> None:
    app.router.add_get("/health", health)
    app.router.add_post("/v1/register", register)
    app.router.add_get("/v1/work", work)
    app.router.add_post("/v1/progress", progress)
    app.router.add_post("/v1/results", results)
    app.router.add_post("/v1/failed", failed)
    app.router.add_post("/v1/heartbeat", heartbeat)
