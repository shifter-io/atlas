"""Browser bundle ingest (K-BP7): `POST /v1/results` for a run of batch kind `browser`.

A mini uploads one bundle per run (K-BP4: its run directory plus `browser/`). The manager checks
the sender's sha256 while receiving, archives the exact bytes once, verified, under
`<browser.archive_prefix>/<network>/<target>/<cc>/<slot>.tar.gz` (Spaces through ARCHIVE-1's
client; `browser.archive_dir`, a local directory with the same layout, on staging and in tests),
ingests the run directory without `browser/` and finishes the run. It keeps nothing: every
temporary file goes before the reply. The mini deletes its copy only on the confirmation the
reply carries (`browser.contracts.confirmed`); any other answer leaves it in place.

Uploads from several minis are safe and bounded: at most `browser.ingest_concurrency` bundles
are received and processed at once by this process (checked before the body is read: 503,
Retry-After 30), and one per (batch, product) by any process (a PostgreSQL advisory try-lock;
429, Retry-After 10); a body not received within `results.upload_timeout_seconds` is 408. A
run the reaper failed without a run_key (`stale`, `not_started`) is a late upload: it is
ingested, becomes done, and its batch is closed again with it. A replay of a finished run whose
object holds the same sha256 is answered again without touching the database.

Manifests of version 1 (Camoufox, no engine block) and 2 are read. The run's engine is its
summary's `browser_engine` (none in a version 1 bundle: Camoufox), which must be the manifest's;
it is part of the run's config, so runs of different engines are stored in different scopes.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from aiohttp import web

from ..archive import ArchiveError as StoreError
from ..archive.export import file_digests
from ..archive.spaces import ArchiveChanged, Spaces
from ..browser import contracts as bc
from ..browser.engines import DEFAULT_ENGINE
from . import nodework
from .api import (
    DB_KEY,
    NODE_KEY,
    _check_sha256,
    _error,
    _ingest,
    _receive,
    _reject,
    _sent_sha256,
)
from .archive import MAX_MEMBERS, unpack
from .db import Database, session, setting
from .events import raise_alert

# The errors of a run the reaper failed: an upload for it is late data, not a conflict.
REAPED = ("stale", "not_started")
SETTINGS = (
    "results.max_upload_bytes",
    "results.upload_timeout_seconds",
    "results.max_extracted_bytes",
    "browser.ingest_concurrency",
    "browser.archive_prefix",
    "browser.archive_dir",
    "archive.spaces_file",
)
MEMBERS = bc.bundle_members()
FOLDERS = ("browser", MEMBERS.screenshots)
LOCK = "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked"
_active = [0]  # bundles this process is receiving or processing now


@dataclass(frozen=True)
class _Upload:
    """One bundle being ingested: its request and node, the settings, the temporary directory
    it was received into and the digests of the bytes received."""

    request: web.Request
    node: dict
    config: dict
    work: Path
    digests: dict

    @property
    def bundle(self) -> Path:
        return self.work / "bundle.tar.gz"


class LocalArchive:
    """`browser.archive_dir`: the Spaces key layout on a local disk, the metadata of each
    object in `<object>.meta.json`. For staging and tests only, never in production."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _paths(self, key: str) -> tuple[Path, Path]:
        path = self.root / key
        return path, path.with_name(f"{path.name}.meta.json")

    def head(self, key: str) -> dict[str, Any] | None:
        path, meta = self._paths(key)
        if not path.is_file():
            return None
        stored = json.loads(meta.read_text()) if meta.is_file() else {}
        return stored | file_digests(path)

    def put_file(self, key: str, path: Path, digests: dict, meta: dict | None = None) -> None:
        target, sidecar = self._paths(key)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for source, destination in ((None, sidecar), (path, target)):
            partial = destination.with_name(f".{destination.name}.partial")
            if source is None:
                partial.write_text(json.dumps(meta or {}, sort_keys=True))
            else:
                shutil.copyfile(source, partial)
            partial.replace(destination)

    put_verified = Spaces.put_verified


def _busy(
    retry_after: int, message: str, status: type[web.HTTPException] = web.HTTPTooManyRequests
) -> web.HTTPException:
    return status(
        text=json.dumps({"error": message}),
        content_type="application/json",
        headers={"Retry-After": str(retry_after)},
    )


def _unavailable(message: str) -> web.HTTPException:
    return _error(web.HTTPServiceUnavailable, message)


async def check_host(conn: psycopg.AsyncConnection, node: dict, host: Any) -> dict:
    """D2: a browser node's token is bound to its host's logical name (nodes.host); 403 for
    any other name. Returns the node's host and last heartbeat."""
    row = await (
        await conn.execute(
            "SELECT host,last_heartbeat FROM nodes WHERE node_id=%s", (node["node_id"],)
        )
    ).fetchone()
    if row is None or row["host"] is None or host != row["host"]:
        raise _error(web.HTTPForbidden, "token belongs to another host")
    return row


async def _settings(db: Database, node: dict) -> dict[str, Any]:
    async with db.connection() as conn:
        values = {key: await setting(conn, key) for key in SETTINGS}
        row = await (
            await conn.execute(
                "SELECT host,last_heartbeat FROM nodes WHERE node_id=%s", (node["node_id"],)
            )
        ).fetchone()
    if row is None or row["last_heartbeat"] is None:
        raise _error(web.HTTPForbidden, "a browser node must register before it uploads")
    if not values["browser.archive_dir"] and not values["archive.spaces_file"]:
        raise _unavailable("archive_not_configured")
    return values | {"host": row["host"]}


async def ingest_bundle(request: web.Request, run: dict, node: dict) -> web.Response:
    """K-BP7 steps 1-8 for one browser bundle (`run` is nodework.begin_upload's row)."""
    db = request.app[DB_KEY]
    sent = _sent_sha256(request, required=True)
    try:
        config = await _settings(db, node)
    except psycopg.OperationalError:
        raise _unavailable("database unavailable; retry") from None
    if _active[0] >= int(config["browser.ingest_concurrency"]):
        raise _busy(30, "ingest_busy", web.HTTPServiceUnavailable)
    _active[0] += 1
    try:
        return await _receive_and_lock(request, run, node, config, sent)
    finally:
        _active[0] -= 1


async def _receive_and_lock(
    request: web.Request, run: dict, node: dict, config: dict, sent: str | None
) -> web.Response:
    size = int(config["results.max_upload_bytes"])
    seconds = float(config["results.upload_timeout_seconds"])
    with tempfile.TemporaryDirectory(prefix="proxy-index-browser-") as temporary:
        work = Path(temporary)
        digests = await _receive(request, work / "bundle.tar.gz", size, seconds)
        _check_sha256(sent, digests)
        try:
            return await _locked(_Upload(request, node, config, work, digests), run)
        except psycopg.OperationalError:
            raise _unavailable("database unavailable; retry") from None


async def _locked(upload: _Upload, run: dict) -> web.Response:
    """One upload per (batch, product) across processes: a session lock on a connection of its
    own, held for the ingest and released when that connection closes."""
    lock = f"browser-ingest:{run['batch_id']}:{run['product_id']}"
    async with session(upload.request.app[DB_KEY]) as conn:
        if not (await (await conn.execute(LOCK, (lock,))).fetchone())["locked"]:
            raise _busy(10, "this run's bundle is already being ingested; retry")
        return await _process(upload, conn, run["batch_id"])


async def _state(conn: psycopg.AsyncConnection, batch_id: int, node: dict) -> dict:
    return await (
        await conn.execute(
            "SELECT br.batch_id,br.product_id,br.status,br.run_key,br.error,b.target_id,"
            "b.country_code FROM batch_runs br JOIN test_batches b USING(batch_id) "
            "WHERE br.batch_id=%s AND br.node_id=%s",
            (batch_id, node["node_id"]),
        )
    ).fetchone()


async def _process(upload: _Upload, conn: psycopg.AsyncConnection, batch_id: int) -> web.Response:
    request, node, config, digests = upload.request, upload.node, upload.config, upload.digests
    state = await _state(conn, batch_id, node)  # read again under the lock
    status = state["status"]
    if status in ("killed", "skipped"):  # an operator stopped it: the mini discards
        return web.json_response({"ok": True, "ingested": False, "status": status})
    replay = status in ("done", "failed") and state["run_key"] is not None
    late = status == "failed" and state["run_key"] is None and state["error"] in REAPED
    if not (replay or late or status in ("running", "uploading")):
        raise _error(web.HTTPConflict, f"run is {status}")
    try:
        summary, manifest = await asyncio.to_thread(
            _open_bundle, upload.work, int(config["results.max_extracted_bytes"])
        )
        _check(summary, manifest, request.headers.get(bc.HEADER_BUNDLE_ID), state, node)
    except ValueError as exc:
        if not replay:
            await _reject(request, batch_id, f"bad bundle: {exc}"[:500], "results_rejected")
        raise _error(web.HTTPBadRequest, str(exc)[:300]) from None
    if manifest["host"] != config["host"]:
        raise _error(web.HTTPForbidden, "token belongs to another host")
    key = _key(config, node, summary, manifest)
    meta = {"run-id": summary["run_id"], "batch-id": str(batch_id)}
    await _archive(upload, key, meta, replay)
    archived = {bc.REPLY_ARCHIVED_KEY: key, bc.REPLY_ARCHIVED_SHA256: digests["sha256"]}
    if replay:
        run_key = state["run_key"]
    else:
        imported = await _ingest(request, batch_id, upload.work / "run", ip_rows=False)
        run_key = imported["run_key"]
        if late:
            await _reopen(request.app[DB_KEY], batch_id, node)
        status = await nodework.finish_run(request.app[DB_KEY], node, batch_id, summary, run_key)
    return web.json_response(
        {"ok": True, "ingested": True, "status": status, "run_key": run_key, "archived": archived}
    )


def bundle_member(name: str) -> bool:
    """A K-BP4 `browser/` member or one of its two directories."""
    return MEMBERS.is_member(name) or name in FOLDERS


def unpack_bundle(source: Path, directory: Path, limit: int) -> list[str]:
    """orchestrator.archive.unpack of a browser bundle: the run archive's limits, its `browser/`
    members (at most a manifest, the tries, the samples and one screenshot per try) besides."""
    return unpack(
        source,
        directory,
        max_bytes=limit,
        nested=bundle_member,
        max_members=MAX_MEMBERS + 3 + bc.MAX_TRIES,
    )


def _open_bundle(work: Path, limit: int) -> tuple[dict, dict]:
    """Unpack, move `browser/` out of the run directory, validate the manifest and the files it
    lists."""
    run_dir, browser = work / "run", work / "browser"
    run_dir.mkdir(mode=0o700)
    names = unpack_bundle(work / "bundle.tar.gz", run_dir, limit)
    if MEMBERS.manifest not in names:
        raise ValueError("the bundle has no browser/manifest.json")
    (run_dir / "browser").replace(browser)
    summary = json.loads((run_dir / "summary.json").read_text())
    manifest = bc.check_manifest(json.loads((work / MEMBERS.manifest).read_text()))
    listed = {entry["name"]: entry for entry in manifest["files"]}
    if set(listed) != {name for name in names if "/" in name} - {MEMBERS.manifest}:
        raise ValueError("the manifest's files differ from the bundle's browser/ members")
    for name, entry in listed.items():
        found = file_digests(work / name)
        if (found["bytes"], found["sha256"]) != (entry["bytes"], entry["sha256"]):
            raise ValueError(f"{name} differs from its manifest entry")
    return summary, manifest


def _check(summary: Any, manifest: dict, bundle_id: str | None, state: dict, node: dict):
    """The bundle belongs to this node's run: network, target, batch, run id, country and
    engine."""
    config = summary.get("config") if isinstance(summary, dict) else None
    target = summary.get("target") if isinstance(summary, dict) else None
    if not isinstance(config, dict) or not isinstance(target, dict):
        raise ValueError("summary.json must be an object with config and target")
    network = {"provider": node["provider"], "product": node["product"]}
    ours = {"provider": config.get("provider"), "product": config.get("product")}
    if ours != network or manifest["network"] != network:
        raise ValueError("results belong to another provider or product")
    if target.get("id") != state["target_id"] or manifest["target"] != state["target_id"]:
        raise ValueError("results are for another target")
    if bundle_id is None or not bundle_id == summary.get("run_id") == manifest["run_id"]:
        raise ValueError(f"{bc.HEADER_BUNDLE_ID}, summary.json and the manifest differ")
    if (manifest["batch_id"], manifest["country"]) != (state["batch_id"], state["country_code"]):
        raise ValueError("the manifest is for another batch")
    requested = config.get("requested_country")
    requested = requested.upper() if isinstance(requested, str) else requested
    if manifest["requested_country"] != requested or manifest["geo_targeting"] != bool(requested):
        raise ValueError("the manifest's geo targeting disagrees with summary.json")
    if bc.manifest_engine(manifest) != config.get("browser_engine", DEFAULT_ENGINE):
        raise ValueError("the manifest's engine disagrees with summary.json")


def _key(config: dict, node: dict, summary: dict, manifest: dict) -> str:
    """K-BP4: the cc is the requested country, `untargeted` when none was requested (D11)."""
    try:
        return bc.object_key(
            config["browser.archive_prefix"],
            bc.network_slug(node["provider"], node["product"]),
            manifest["target"],
            summary["config"].get("requested_country"),
            datetime.fromisoformat(manifest["slot"]),
        )
    except ValueError as exc:
        raise _unavailable(f"archive_not_configured: {exc}") from None


def _store(upload: _Upload, key: str, meta: dict, replay: bool):
    """The verified put; for a replay only the check that the object holds the same bytes.
    Returns False when a replay's object is missing."""
    config, digests = upload.config, upload.digests
    target = config["browser.archive_dir"]
    archive = LocalArchive(target) if target else Spaces.from_file(config["archive.spaces_file"])
    if not replay:
        archive.put_verified(
            key, upload.bundle, digests, meta, existing=("sha256",), what="the bundle"
        )
        return True
    found = archive.head(key)
    if found is not None and found.get("sha256") != digests["sha256"]:
        raise ArchiveChanged(f"archive_changed: {key} in the bucket differs from the bundle")
    return found is not None


async def _archive(upload: _Upload, key: str, meta: dict, replay: bool) -> None:
    """409 bundle_changed (and the alert) for an object with other bytes, never overwritten;
    503 when the archive cannot be reached."""
    try:
        stored = await asyncio.to_thread(_store, upload, key, meta, replay)
    except ArchiveChanged:
        await _changed(upload.request, key, upload.digests["sha256"])
        raise _error(web.HTTPConflict, bc.ERROR_BUNDLE_CHANGED) from None
    except (StoreError, OSError) as exc:
        raise _unavailable(f"archive unavailable ({type(exc).__name__}); retry") from None
    if not stored:
        raise _error(web.HTTPConflict, "run is finished but its bundle is not archived")


async def _changed(request: web.Request, key: str, sha256: str) -> None:
    node = request[NODE_KEY]
    async with request.app[DB_KEY].transaction() as conn:
        await raise_alert(
            conn,
            bc.ALERT_BUNDLE_CHANGED,
            f"node {node['node_id']} uploaded a browser bundle that differs from the archived one",
            severity="major",
            dedupe_key=f"{bc.ALERT_BUNDLE_CHANGED}:{key}",
            data={"key": key, "sha256": sha256, "node_id": node["node_id"]},
        )


async def _reopen(db: Database, batch_id: int, node: dict) -> None:
    """A late upload: the run the reaper failed goes back to uploading, and its batch, if that
    closed it, back to running, so finish_run links the run and the barrier closes the batch
    again with its status recomputed."""
    now = datetime.now(UTC)
    async with db.transaction() as conn:
        await conn.execute("SELECT 1 FROM test_batches WHERE batch_id=%s FOR UPDATE", (batch_id,))
        reopened = await (
            await conn.execute(
                "UPDATE batch_runs SET status='uploading',last_progress_at=%s "
                "WHERE batch_id=%s AND node_id=%s AND status='failed' AND run_key IS NULL "
                "AND error=ANY(%s) RETURNING product_id",
                (now, batch_id, node["node_id"], list(REAPED)),
            )
        ).fetchone()
        if reopened is not None:
            await conn.execute(
                "UPDATE test_batches SET status='running',finished_at=NULL "
                "WHERE batch_id=%s AND status IN ('done','failed')",
                (batch_id,),
            )
