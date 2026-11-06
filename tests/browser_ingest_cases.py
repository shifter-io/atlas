"""Shared helpers of the browser bundle ingest tests (orchestrator/browser_ingest.py, K-BP7):
three registered browser nodes, ARCHIVE-1's fake S3 bucket with listing, K-BP4 bundles and their
upload. test_browser_ingest.py holds the accepted and refused bundles, test_browser_ingest_runs.py
concurrency and run states, test_browser_ingest_archive.py the archive side.

The test modules load it with `pytest_plugins` after importing its names, so pytest cannot
rewrite it (PYTEST_DONT_REWRITE).
"""

import asyncio
import hashlib
import io
import json
import tarfile
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer

import pytest
from aiohttp.test_utils import TestClient, TestServer
from orchestrator_cases import bearer, prepare, query
from test_archive_job import KEY_ID, SECRET, Bucket
from test_storage import measurement

from proxy_index.browser import contracts as bc
from proxy_index.models import VERDICTS
from proxy_index.orchestrator import browser_ingest
from proxy_index.orchestrator.app import build_app
from proxy_index.orchestrator.db import Database, put_setting
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

SLOT = datetime(2026, 10, 1, 12, tzinfo=UTC)
MEMBERS = bc.bundle_members()
HOSTS = {"alpha": "browser-1", "beta": "browser-2", "gamma": "browser-3"}
KEY = "browser-files/alpha.res/amazon_pdp/de/2026-10-01T1200Z.tar.gz"


class ListingBucket(Bucket):
    """ARCHIVE-1's fake bucket, plus ListObjectsV2 pages of two keys (start-after)."""

    def _handle(self):
        query = self.path.partition("?")[2]
        if "list-type=2" not in query:
            return super()._handle()
        if not self._signed(None):
            return self._answer(403, b"<Error><Code>SignatureDoesNotMatch</Code></Error>")
        params = dict(q.partition("=")[::2] for q in query.split("&"))
        after, prefix = params.get("start-after", ""), params["prefix"]
        keys = sorted(k[1:] for k in self.objects if k[1:].startswith(prefix) and k[1:] > after)
        xml = "".join(f"<Contents><Key>{key}</Key></Contents>" for key in keys[:2])
        more = str(len(keys) > 2).lower()
        body = f"<ListBucketResult><IsTruncated>{more}</IsTruncated>{xml}</ListBucketResult>"
        return self._answer(200, body.encode())

    do_GET = do_PUT = do_HEAD = _handle


@contextmanager
def listing_bucket(tmp_path):
    handler = type("Handler", (ListingBucket,), {"objects": {}, "state": {}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = tmp_path / "spaces.toml"
    settings.write_text(
        f'[spaces]\nendpoint = "http://127.0.0.1:{server.server_port}"\nbucket = "archive"\n'
        f'region = "fra1"\naccess_key_id = "{KEY_ID}"\nsecret_access_key = "{SECRET}"\n'
    )
    settings.chmod(0o600)
    try:
        yield handler, settings
    finally:
        server.shutdown()


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()


@pytest.fixture
async def client(database, db):
    test_client = TestClient(TestServer(build_app(database, with_scheduler=False, db=db)))
    await test_client.start_server()
    yield test_client
    await test_client.close()


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """The manager's temporary directory, empty before and checked after every upload."""
    path = tmp_path / "manager-tmp"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    return path


@pytest.fixture
async def world(database, db, client, tmp_path, scratch):
    """Three registered browser nodes (one network per mini), the fake bucket as the archive,
    and a raw spool directory the browser path must leave empty."""
    prepare(database, ("DE", "US"))
    tokens = {}
    for provider, host in HOSTS.items():
        node = issue_token(database, provider=provider, product="res", region="browser", host=host)
        tokens[provider] = node["token"]
        body = {"provider": provider, "product": "res", "region": "browser", "host": host}
        reply = await client.post("/v1/register", json=body, headers=bearer(node["token"]))
        assert reply.status == 200, await reply.text()
    spool = tmp_path / "spool"
    with listing_bucket(tmp_path) as (bucket, settings):
        await set_settings(db, {"archive.spaces_file": str(settings), "raw.spool_dir": str(spool)})
        yield {
            "tokens": tokens,
            "bucket": bucket,
            "spool": spool,
            "tmp": tmp_path,
            "file": settings,
        }


async def set_settings(db, values):
    async with db.connection() as conn:
        for key, value in values.items():
            await put_setting(conn, key, value)


def browser_run(database, provider, *, target="amazon_pdp", slot=SLOT, status="running"):
    """The provider's run in the browser batch of (target, slot) in DE, created when missing (a
    batch has one run per network); returns the batch id."""
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at,status) VALUES ('browser','DE',%s,%s,%s,%s,'running') ON CONFLICT DO NOTHING",
            (bc.BROWSER_REGION_ID, target, slot, slot),
        )
        (batch_id,) = conn.execute(
            "SELECT batch_id FROM test_batches WHERE kind='browser' AND target_id=%s "
            "AND scheduled_for=%s",
            (target, slot),
        ).fetchone()
        conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,node_id,status,started_at) "
            "SELECT %s,p.product_id,n.node_id,%s,%s FROM nodes n JOIN products p "
            "ON p.provider=n.provider AND p.name=n.product WHERE n.provider=%s AND n.region_id=%s",
            (batch_id, status, slot, provider, bc.BROWSER_REGION_ID),
        )
    return batch_id


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def tries_line():
    record = bc.try_record(
        i=0,
        input="https://www.amazon.de/dp/B000000000",
        source=None,
        verdict="ok",
        error=None,
        status=200,
        final_url="https://www.amazon.de/dp/B000000000",
        timings_ms=dict.fromkeys(bc.TIMINGS, 10),
        bytes_down_est=1000,
        reason=None,
        screenshot=None,
    )
    return (json.dumps(record) + "\n").encode()


async def make_bundle(tmp_path, batch_id, provider="alpha", **options):
    """A K-BP4 bundle for the batch: a real run directory (target, country and network set as
    the browser runner sets them) plus browser/manifest.json and browser/tries.jsonl."""
    target, requested = options.get("target", "amazon_pdp"), options.get("requested", "de")
    work = tmp_path / f"bundle-{batch_id}-{provider}-{time.monotonic_ns()}"
    work.mkdir()
    directory = await measurement(
        work, provider=provider, product="res", requested_country=requested
    )
    summary = json.loads((directory / "summary.json").read_text())
    summary["target"]["id"] = target
    (directory / "summary.json").write_text(json.dumps(summary))
    tries = tries_line()
    manifest = bc.manifest(
        run_id=summary["run_id"],
        batch_id=batch_id,
        network={"provider": provider, "product": "res"},
        target=target,
        country="DE",
        requested_country=requested.upper() if requested else None,
        geo_targeting=requested is not None,
        slot=SLOT,
        started_at=SLOT,
        finished_at=SLOT + timedelta(minutes=1),
        tries=1,
        outcomes={
            "verdicts": dict.fromkeys(VERDICTS, 0) | {"ok": 1},
            "errors": dict.fromkeys(bc.CODE_VERDICTS, 0),
        },
        settings_sha256="a" * 64,
        camoufox={
            "release": "v135.0.1-beta.24",
            "engine_version": "135.0.1",
            "user_agent": bc.user_agent("135.0.1", "macos"),
        },
        host=options.get("host") or HOSTS[provider],
        files=[{"name": MEMBERS.tries, "bytes": len(tries), "sha256": sha256(tries)}],
    ) | options.get("manifest", {})
    members = [(MEMBERS.tries, tries), (MEMBERS.manifest, json.dumps(manifest).encode())]
    members = members[: 1 if options.get("no_manifest") else 2]
    return tar_bytes(directory, members + list(options.get("extra", ()))), summary["run_id"]


def tar_bytes(directory, members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(directory.iterdir()):
            archive.add(path, arcname=path.name)
        folder = tarfile.TarInfo("browser")
        folder.type = tarfile.DIRTYPE
        archive.addfile(folder)
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


async def upload(client, token, batch_id, data, run_id, sha=None, extra_headers=None):
    headers = bearer(token) | {
        "Content-Type": bc.CONTENT_TYPE,
        bc.HEADER_SHA256: sha or sha256(data),
        bc.HEADER_BUNDLE_ID: run_id,
    }
    headers |= extra_headers or {}
    reply = await client.post(f"/v1/results?batch_id={batch_id}", data=data, headers=headers)
    return reply.status, await reply.json(), reply.headers


def run_rows(database, batch_id):
    return query(
        database,
        "SELECT br.status,br.run_key,(SELECT count(*) FROM run_buckets rb WHERE "
        "rb.run_key=br.run_key) FROM batch_runs br WHERE br.batch_id=%s",
        (batch_id,),
    )


def assert_nothing_kept(world, scratch):
    assert list(scratch.iterdir()) == []
    assert not world["spool"].exists() or not any(world["spool"].rglob("*"))


class Gate:
    """Hold the first `hold` archive writes until released, so uploads overlap for certain."""

    def __init__(self, monkeypatch, hold):
        self.hold, self.waiting, self.release = hold, 0, threading.Event()
        self.lock = threading.Lock()
        store = browser_ingest._store

        def held(*args):
            with self.lock:
                self.waiting += 1
                wait = self.waiting <= self.hold
            if wait:
                self.release.wait(10)
            return store(*args)

        monkeypatch.setattr(browser_ingest, "_store", held)

    async def until_held(self):
        for _ in range(500):
            if self.waiting >= self.hold:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the uploads never reached the archive")
