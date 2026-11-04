"""Browser bundle ingest (orchestrator/browser_ingest.py, K-BP7): POST /v1/results for kind
browser, through PostgreSQL, ARCHIVE-1's fake S3 bucket and the aiohttp test client."""

import asyncio
import hashlib
import io
import json
import tarfile
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from http.server import ThreadingHTTPServer

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_archive import empty_database
from test_archive_job import KEY_ID, SECRET, Bucket
from test_orchestrator import bearer, planned_run, prepare, query
from test_storage import measurement

from proxy_index.archive import ArchiveError
from proxy_index.archive.export import file_digests
from proxy_index.archive.rawfiles import rebuild_browser
from proxy_index.archive.spaces import ArchiveChanged, Spaces
from proxy_index.browser import contracts as bc
from proxy_index.cli import main
from proxy_index.models import VERDICTS
from proxy_index.orchestrator import browser_ingest
from proxy_index.orchestrator.app import build_app
from proxy_index.orchestrator.archive import ArchiveError as UploadError
from proxy_index.orchestrator.archive import pack, unpack
from proxy_index.orchestrator.browser_ingest import LocalArchive
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
    """A browser batch in DE with the provider's node running it; returns the batch id."""
    with connect(database) as conn:
        (batch_id,) = conn.execute(
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at,status) VALUES ('browser','DE',%s,%s,%s,%s,'running') RETURNING batch_id",
            (bc.BROWSER_REGION_ID, target, slot, slot),
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


# --- the happy path ---------------------------------------------------------------------------


async def test_a_bundle_is_archived_verified_ingested_and_nothing_is_kept(
    database, client, world, scratch
):
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert status == 200, body
    assert body["ingested"] is True and body["status"] == "done"
    assert body["archived"] == {"key": KEY, "sha256": sha256(data)}
    assert bc.confirmed((status, body), sha256(data))
    stored, meta, acl = world["bucket"].objects["/" + KEY]
    assert stored == data and acl == "private"
    meta = {k.lower(): v for k, v in meta}
    assert meta["x-amz-meta-sha256"] == sha256(data)
    assert meta["x-amz-meta-run-id"] == run_id and meta["x-amz-meta-batch-id"] == str(batch_id)
    ((run_status, run_key, buckets),) = run_rows(database, batch_id)
    assert (run_status, run_key) == ("done", body["run_key"]) and buckets >= 1
    assert query(database, "SELECT run_id::text FROM test_runs") == [(run_id,)]
    assert query(database, "SELECT status FROM test_batches WHERE batch_id=%s", (batch_id,)) == [
        ("done",)
    ]
    assert_nothing_kept(world, scratch)


async def test_an_untargeted_run_is_archived_under_untargeted(database, client, world, scratch):
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id, requested=None)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert status == 200, body
    key = "browser-files/alpha.res/amazon_pdp/untargeted/2026-10-01T1200Z.tar.gz"
    assert body["archived"]["key"] == key and "/" + key in world["bucket"].objects


async def test_the_local_archive_directory_replaces_spaces(database, db, client, world, scratch):
    archive_dir = world["tmp"] / "staging-archive"
    await set_settings(db, {"browser.archive_dir": str(archive_dir), "archive.spaces_file": None})
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert status == 200, body
    assert (archive_dir / KEY).read_bytes() == data and world["bucket"].objects == {}
    assert json.loads((archive_dir / f"{KEY}.meta.json").read_text()) == {
        "batch-id": str(batch_id),
        "run-id": run_id,
    }
    assert_nothing_kept(world, scratch)


async def test_without_an_archive_a_bundle_is_refused_503(database, db, client, world, scratch):
    await set_settings(db, {"archive.spaces_file": None})
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert (status, body) == (503, {"error": "archive_not_configured"})
    assert run_rows(database, batch_id)[0][1] is None


# --- checksums --------------------------------------------------------------------------------


async def test_a_wrong_checksum_is_refused_and_nothing_is_kept(database, client, world, scratch):
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id)
    token = world["tokens"]["alpha"]
    status, body, _ = await upload(client, token, batch_id, data, run_id, sha="0" * 64)
    assert (status, body) == (400, {"error": bc.ERROR_CHECKSUM_MISMATCH})
    assert world["bucket"].objects == {} and query(database, "SELECT * FROM test_runs") == []
    assert_nothing_kept(world, scratch)
    # The header is required for a browser bundle, and must be a SHA-256.
    reply = await client.post(
        f"/v1/results?batch_id={batch_id}",
        data=data,
        headers=bearer(token) | {bc.HEADER_BUNDLE_ID: run_id},
    )
    assert reply.status == 400 and "64 hex" in (await reply.json())["error"]
    status, body, _ = await upload(client, token, batch_id, data, run_id, sha="xyz")
    assert status == 400 and "64 hex" in body["error"]
    # The mini retries after re-hashing: the same bundle goes through.
    assert (await upload(client, token, batch_id, data, run_id))[0] == 200


async def test_other_kinds_verify_a_checksum_when_sent(database, db, client, tmp_path):
    batch_id, headers = await planned_run(database, db, client)
    directory = await measurement(tmp_path, requests=3)
    data = pack(directory, tmp_path / "run.tar.gz").read_bytes()
    url = f"/v1/results?batch_id={batch_id}"
    wrong = await client.post(url, data=data, headers=headers | {bc.HEADER_SHA256: "f" * 64})
    assert wrong.status == 400 and (await wrong.json())["error"] == bc.ERROR_CHECKSUM_MISMATCH
    assert query(database, "SELECT * FROM test_runs") == []
    right = await client.post(url, data=data, headers=headers | {bc.HEADER_SHA256: sha256(data)})
    assert right.status == 200 and (await right.json())["ingested"] is True


# --- concurrency ------------------------------------------------------------------------------


async def test_three_minis_upload_twelve_bundles_at_once(database, db, client, world, scratch):
    await set_settings(db, {"browser.ingest_concurrency": 12})
    targets = bc.PAGE_TARGETS[:4]
    uploads = []
    for provider in HOSTS:
        for target in targets:
            batch_id = browser_run(database, provider, target=target)
            data, run_id = await make_bundle(world["tmp"], batch_id, provider, target=target)
            uploads.append((world["tokens"][provider], batch_id, data, run_id))
    replies = await asyncio.gather(*(upload(client, *args) for args in uploads))
    assert [status for status, _, _ in replies] == [200] * 12, replies
    assert len(world["bucket"].objects) == 12
    assert query(database, "SELECT count(*) FROM test_runs") == [(12,)]
    assert query(database, "SELECT count(*) FROM batch_runs WHERE status='done'") == [(12,)]
    assert_nothing_kept(world, scratch)


async def test_a_full_ingest_semaphore_answers_429_and_retries_succeed(
    database, db, client, world, scratch, monkeypatch
):
    await set_settings(db, {"browser.ingest_concurrency": 2})
    uploads = []
    for provider in ("alpha", "beta", "gamma"):
        batch_id = browser_run(database, provider)
        data, run_id = await make_bundle(world["tmp"], batch_id, provider)
        uploads.append((world["tokens"][provider], batch_id, data, run_id))
    gate = Gate(monkeypatch, hold=2)
    first = [asyncio.create_task(upload(client, *args)) for args in uploads[:2]]
    await gate.until_held()
    status, body, headers = await upload(client, *uploads[2])
    assert status == 429 and headers["Retry-After"] == "30", body
    gate.release.set()
    assert [reply[0] for reply in await asyncio.gather(*first)] == [200, 200]
    assert (await upload(client, *uploads[2]))[0] == 200
    assert len(world["bucket"].objects) == 3
    assert_nothing_kept(world, scratch)


async def test_a_duplicate_in_flight_gets_429_and_a_replay_adds_nothing(
    database, client, world, scratch, monkeypatch
):
    batch_id = browser_run(database, "alpha")
    args = (world["tokens"]["alpha"], batch_id, *await make_bundle(world["tmp"], batch_id))
    gate = Gate(monkeypatch, hold=1)
    first = asyncio.create_task(upload(client, *args))
    await gate.until_held()
    status, body, headers = await upload(client, *args)
    assert status == 429 and headers["Retry-After"] == "10", body
    gate.release.set()
    status, body, _ = await first
    assert status == 200 and body["status"] == "done"
    replay_status, replay, _ = await upload(client, *args)
    assert replay_status == 200 and replay == body
    assert query(database, "SELECT count(*) FROM test_runs") == [(1,)]
    assert len(world["bucket"].objects) == 1
    assert_nothing_kept(world, scratch)


# --- run states, late data, failures ----------------------------------------------------------


async def test_a_spaces_failure_answers_503_and_a_retry_succeeds(
    database, client, world, scratch, monkeypatch
):
    batch_id = browser_run(database, "alpha")
    args = (world["tokens"]["alpha"], batch_id, *await make_bundle(world["tmp"], batch_id))
    put_file = Spaces.put_file
    calls = []

    def failing_once(self, *a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise ArchiveError("Spaces PUT failed: ConnectionResetError")
        return put_file(self, *a, **k)

    monkeypatch.setattr(Spaces, "put_file", failing_once)
    status, body, _ = await upload(client, *args)
    assert status == 503 and "archive unavailable" in body["error"]
    assert query(database, "SELECT * FROM test_runs") == [] and world["bucket"].objects == {}
    assert run_rows(database, batch_id)[0][:2] == ("uploading", None)
    assert_nothing_kept(world, scratch)
    status, body, _ = await upload(client, *args)
    assert status == 200 and body["status"] == "done"


async def test_a_database_failure_after_the_archive_is_retried_idempotently(
    database, client, world, scratch, monkeypatch
):
    import psycopg

    from proxy_index.orchestrator import batches

    batch_id = browser_run(database, "alpha")
    args = (world["tokens"]["alpha"], batch_id, *await make_bundle(world["tmp"], batch_id))
    finish = batches.finish_run

    async def unreachable(*a, **k):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(batches, "finish_run", unreachable)
    status, body, _ = await upload(client, *args)
    assert (status, body) == (503, {"error": "database unavailable; retry"})
    assert len(world["bucket"].objects) == 1
    monkeypatch.setattr(batches, "finish_run", finish)
    status, body, _ = await upload(client, *args)
    assert status == 200 and body["status"] == "done"
    assert query(database, "SELECT count(*) FROM test_runs") == [(1,)]
    assert len(world["bucket"].objects) == 1


async def test_a_late_upload_for_a_reaped_run_is_ingested_and_closes_the_batch_done(
    database, client, world, scratch
):
    batch_id = browser_run(database, "alpha")
    with connect(database) as conn:  # what the reaper leaves: run failed 'stale', batch failed
        conn.execute(
            "UPDATE batch_runs SET status='failed',error='stale',stop_reason='error',"
            "finished_at=now() WHERE batch_id=%s",
            (batch_id,),
        )
        conn.execute(
            "UPDATE test_batches SET status='failed',finished_at=now() WHERE batch_id=%s",
            (batch_id,),
        )
    data, run_id = await make_bundle(world["tmp"], batch_id)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert status == 200 and body["status"] == "done", body
    assert query(
        database, "SELECT status,error,run_key FROM batch_runs WHERE batch_id=%s", (batch_id,)
    ) == [("done", None, body["run_key"])]
    assert query(database, "SELECT status FROM test_batches WHERE batch_id=%s", (batch_id,)) == [
        ("done",)
    ]


@pytest.mark.parametrize("state", ["killed", "skipped"])
async def test_a_stopped_run_is_discarded_with_ingested_false(
    database, client, world, scratch, state
):
    batch_id = browser_run(database, "alpha", status=state)
    data, run_id = await make_bundle(world["tmp"], batch_id)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert (status, body) == (200, {"ok": True, "ingested": False, "status": state})
    assert bc.confirmed((status, body), sha256(data))
    assert world["bucket"].objects == {} and query(database, "SELECT * FROM test_runs") == []
    assert_nothing_kept(world, scratch)


async def test_a_queued_or_refused_run_is_a_conflict(database, client, world, scratch):
    batch_id = browser_run(database, "alpha", status="queued")
    data, run_id = await make_bundle(world["tmp"], batch_id)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert (status, body) == (409, {"error": "run is queued"})


async def test_an_archived_object_with_other_bytes_is_never_overwritten(
    database, client, world, scratch
):
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id)
    meta = [("x-amz-meta-sha256", "b" * 64)]
    world["bucket"].objects["/" + KEY] = (b"older bytes", meta, "private")
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert (status, body) == (409, {"error": bc.ERROR_BUNDLE_CHANGED})
    assert world["bucket"].objects["/" + KEY][0] == b"older bytes"
    ((severity, data_),) = query(
        database, "SELECT severity,data FROM alerts WHERE kind=%s", (bc.ALERT_BUNDLE_CHANGED,)
    )
    assert severity == "major" and data_["key"] == KEY and data_["sha256"] == sha256(data)
    assert query(database, "SELECT * FROM test_runs") == []
    assert_nothing_kept(world, scratch)


async def test_a_replay_checks_the_archived_object(database, client, world, scratch):
    batch_id = browser_run(database, "alpha")
    args = (world["tokens"]["alpha"], batch_id, *await make_bundle(world["tmp"], batch_id))
    assert (await upload(client, *args))[0] == 200
    world["bucket"].objects["/" + KEY] = (b"x", [("x-amz-meta-sha256", "c" * 64)], "private")
    assert (await upload(client, *args))[:2] == (409, {"error": bc.ERROR_BUNDLE_CHANGED})
    del world["bucket"].objects["/" + KEY]
    status, body, _ = await upload(client, *args)
    assert status == 409 and "not archived" in body["error"]


# --- refusals ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"manifest": {"run_id": "00000000-0000-4000-8000-000000000000"}}, "differ"),
        ({"manifest": {"batch_id": 999999}}, "another batch"),
        ({"manifest": {"target": "google_search"}}, "another target"),
        ({"manifest": {"requested_country": None, "geo_targeting": False}}, "geo targeting"),
        ({"manifest": {"format": "other"}}, "manifest"),
        ({"extra": [("browser/screenshots/0000.jpg", b"jpeg")]}, "files differ"),
    ],
)
async def test_a_bundle_that_does_not_match_its_run_is_refused_400(
    database, client, world, scratch, options, message
):
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id, **options)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert status == 400 and message in body["error"], body
    assert world["bucket"].objects == {} and query(database, "SELECT * FROM test_runs") == []
    assert query(database, "SELECT kind FROM alerts") == [("results_rejected",)]
    assert run_rows(database, batch_id)[0][0] == "failed"
    assert_nothing_kept(world, scratch)


async def test_a_listed_file_with_other_bytes_and_a_missing_bundle_id_are_refused(
    database, client, world, scratch
):
    batch_id = browser_run(database, "alpha")
    entry = {"name": MEMBERS.tries, "bytes": 1, "sha256": "d" * 64}
    data, run_id = await make_bundle(world["tmp"], batch_id, manifest={"files": [entry]})
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert status == 400 and "differs from its manifest entry" in body["error"]
    other = browser_run(database, "alpha", target="google_search")
    data, _ = await make_bundle(world["tmp"], other, target="google_search")
    status, body, _ = await upload(client, world["tokens"]["alpha"], other, data, "")
    assert status == 400 and bc.HEADER_BUNDLE_ID in body["error"]


# --- authentication (D2) ----------------------------------------------------------------------


async def test_a_browser_token_from_another_host_is_refused(database, client, world, scratch):
    token = world["tokens"]["alpha"]
    body = {"provider": "alpha", "product": "res", "region": "browser", "host": "browser-2"}
    reply = await client.post("/v1/register", json=body, headers=bearer(token))
    assert reply.status == 403 and "another host" in (await reply.json())["error"]
    assert query(database, "SELECT host FROM nodes WHERE provider='alpha'") == [("browser-1",)]
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id, host="browser-2")
    status, body, _ = await upload(client, token, batch_id, data, run_id)
    assert (status, body) == (403, {"error": "token belongs to another host"})
    assert world["bucket"].objects == {}


async def test_a_browser_node_must_register_before_it_uploads(database, client, world, scratch):
    node = issue_token(
        database, provider="delta", product="res", region="browser", host="browser-4"
    )
    batch_id = browser_run(database, "delta")
    data, run_id = await make_bundle(world["tmp"], batch_id, "delta", host="browser-4")
    status, body, _ = await upload(client, node["token"], batch_id, data, run_id)
    assert status == 403 and "register" in body["error"]


# --- the rebuild from Spaces (CALC-INC Q5) ---------------------------------------------------


async def test_archive_rebuild_replays_the_browser_bundles_from_spaces(
    database, databases, client, world, scratch, capsys
):
    run_ids = []
    for target in bc.PAGE_TARGETS[:3]:
        batch_id = browser_run(database, "alpha", target=target)
        data, run_id = await make_bundle(world["tmp"], batch_id, target=target)
        assert (await upload(client, world["tokens"]["alpha"], batch_id, data, run_id))[0] == 200
        run_ids.append(run_id)
    world["bucket"].objects["/browser-files/README.txt"] = (b"x", [], "private")  # not a bundle
    world["bucket"].objects["/raw-files/2026/10/01/2026-10-01T12.tar"] = (b"x", [], "private")
    target = empty_database(databases)
    spaces = Spaces.from_file(world["file"])
    day = date(2026, 10, 1)
    report = rebuild_browser(spaces, target, "browser-files", day, day)
    assert report["runs"] == report["imported"] == 3 and len(report["unlinked"]) == 3
    with connect(target) as conn:
        found = conn.execute("SELECT run_id::text FROM test_runs ORDER BY 1").fetchall()
    assert [row[0] for row in found] == sorted(run_ids)
    # The run linked to its batch where the target has it; a replay imports nothing.
    assert rebuild_browser(spaces, database, "browser-files", day, day) == {
        "runs": 3,
        "imported": 0,
        "unlinked": [],
    }
    args = ["archive", "rebuild", "--spaces-file", str(world["file"]), "--from", "2026-10-01"]
    assert main([*args, "--to", "2026-10-01", "--database-file", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["browser"]["imported"] == 0
    assert rebuild_browser(spaces, target, "browser-files", day + timedelta(1), day)["runs"] == 0
    assert (
        main(
            [
                "archive",
                "rebuild",
                "--from",
                "2026-10-01",
                "--to",
                "2026-10-01",
                "--database-file",
                str(target),
            ]
        )
        == 2
    )
    world["bucket"].objects["/" + KEY] = (b"not a bundle", [], "private")
    with pytest.raises(ArchiveError, match="not a readable browser bundle"):
        rebuild_browser(spaces, target, "browser-files", day, day)


# --- the archive helpers ----------------------------------------------------------------------


def test_put_verified_writes_once_and_refuses_other_bytes(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    archive = LocalArchive(tmp_path / "archive")
    stored = archive.put_verified("p/k.tar.gz", first, file_digests(first), {"run-id": "r"})
    assert stored["key"] == "p/k.tar.gz" and stored["sha256"] == file_digests(first)["sha256"]
    assert archive.put_verified("p/k.tar.gz", first, file_digests(first), {"run-id": "r"})
    with pytest.raises(ArchiveChanged, match="archive_changed: p/k.tar.gz .* the local file"):
        archive.put_verified("p/k.tar.gz", second, file_digests(second))
    assert (tmp_path / "archive" / "p/k.tar.gz").read_bytes() == b"first"
    # Only the named fields of an existing object are compared.
    archive.put_verified("p/k.tar.gz", first, file_digests(first), {"run-id": "s"}, existing=())
    assert archive.head("missing") is None


def test_unpack_admits_only_the_nested_names_asked_for(tmp_path):
    count = iter(range(100))

    def fresh():
        path = tmp_path / f"out{next(count)}"
        path.mkdir()
        return path

    def archive_of(*names):
        path = tmp_path / f"a{next(count)}.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name in ("summary.json", *names):
                info = tarfile.TarInfo(name)
                info.size = 2
                archive.addfile(info, io.BytesIO(b"{}"))
        return path

    nested = browser_ingest.bundle_member
    out = fresh()
    names = unpack(archive_of(MEMBERS.tries), out, max_bytes=100, nested=nested)
    assert names == ["summary.json", MEMBERS.tries] and (out / MEMBERS.tries).is_file()
    for name in ("browser/../evil", "browser/other.json", "browser/screenshots/1.jpg"):
        with pytest.raises(UploadError, match="plain, unique"):
            unpack(archive_of(name), fresh(), max_bytes=100, nested=nested)
    with pytest.raises(UploadError, match="plain, unique"):  # no nesting without the option
        unpack(archive_of(MEMBERS.tries), fresh(), max_bytes=100)
    with pytest.raises(UploadError, match="Too many"):
        unpack(archive_of(MEMBERS.tries), fresh(), max_bytes=100, nested=nested, max_members=1)
