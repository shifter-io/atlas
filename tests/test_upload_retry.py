"""INGEST-HANG-2: a node keeps retrying a finished run's upload while the run's hour is open.

26 Nov 2026: while the upload path was blocked, nodes gave up after 8 tries (2 s doubling to
256 s, about 9 to 10 minutes) and one hung try could hold an upload for 30 minutes (sock_read
1800 s): thousands of pulse runs were lost as stale. Now each try gives up after READ_SECONDS
without a reply, the tries go on until the end of the run's hour (at least MIN_WINDOW) with a
capped backoff, and the alert results_stalled says when /v1/results answers no upload.

The clocks are scaled down: a test's "outage" is many more failed tries than the old 8, in
well under a second.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from orchestrator_cases import planned_run, query, set_setting
from test_storage import measurement

from atlas import node_upload
from atlas.models import ProxyConfig
from atlas.node import Active, NodeAgent, NodeSettings
from atlas.orchestrator import results_watch
from atlas.orchestrator.events import raise_alert
from atlas.orchestrator.results_watch import ResultsWatch, check

pytest_plugins = ["test_storage", "orchestrator_cases"]

OLD_ATTEMPTS = 8  # the tries a node made before this change


@pytest.fixture
def fast(monkeypatch):
    """Retries in milliseconds: the node's backoff and the orchestrator's Retry-After."""
    monkeypatch.setattr(node_upload, "BACKOFF_MAX", 0.005)
    monkeypatch.setattr(node_upload, "RETRY_AFTER_MAX", 0.005)


def agent_for(base, tmp_path, token="tok", product="q"):
    agent = NodeAgent(
        NodeSettings(
            base,
            token,
            provider="fixture",
            product=product,
            region="fra1",
            proxy_factory=lambda country: ProxyConfig("http", "127.0.0.1", 9),
            work_dir=tmp_path,
            upload_backoff=0.001,
        )
    )
    return agent


def pulse(batch_id=7, start=None):
    start = start or datetime.now(UTC)
    return Active({"batch_id": batch_id, "kind": "pulse", "start_at": start.isoformat()})


def run_dir(tmp_path):
    directory = tmp_path / "run"
    directory.mkdir()
    (directory / "summary.json").write_text("{}")
    return directory


class Flaky:
    """/v1/results that hangs `hangs` times, then answers 503 (Retry-After) `busy` times, then
    200; or 503 for ever (`busy=None`)."""

    def __init__(self, hangs=0, busy=None):
        self.hangs, self.busy, self.tries = hangs, busy, 0
        self.release = asyncio.Event()  # ends the hung handlers before the server closes
        self.tried = asyncio.Event()  # set by the first try

    async def __call__(self, request):
        await request.read()
        self.tries += 1
        self.tried.set()
        if self.tries <= self.hangs:
            with contextlib.suppress(TimeoutError):  # 30 s: past the try's read timeout
                await asyncio.wait_for(self.release.wait(), 30)
        if self.busy is None or self.tries <= self.hangs + self.busy:
            raise web.HTTPServiceUnavailable(headers={"Retry-After": "30"})
        return web.json_response({"ok": True, "status": "done"})


async def serving(handler):
    app = web.Application()

    async def route(request):
        return await handler(request)

    app.router.add_post("/v1/results", route)
    server = TestServer(app)
    await server.start_server()
    return server


async def upload_through(handler, tmp_path, active):
    server = await serving(handler)
    agent = agent_for(str(server.make_url("")), tmp_path)
    directory = run_dir(tmp_path)
    if active is not None:
        agent.active[active.assignment["batch_id"]] = active
    try:
        async with aiohttp.ClientSession() as agent.session:
            reply = await agent.upload(7, directory)
    finally:
        handler.release.set()
        await server.close()
    return reply, directory


async def test_a_node_uploads_after_a_hung_then_busy_orchestrator_recovers(
    tmp_path, fast, monkeypatch
):
    """Hung tries are cut at READ_SECONDS, and the node goes on well past the old 8 tries."""
    monkeypatch.setattr(node_upload, "READ_SECONDS", 0.2)
    flaky = Flaky(hangs=3, busy=2 * OLD_ATTEMPTS)
    began = time.monotonic()
    reply, directory = await upload_through(flaky, tmp_path, pulse())
    assert reply == {"ok": True, "status": "done"}
    assert flaky.tries == 3 + 2 * OLD_ATTEMPTS + 1
    assert not directory.exists()  # deleted once accepted
    assert time.monotonic() - began < 10  # three hangs of 30 s each were cut short


async def test_the_retries_end_with_the_run_s_window(tmp_path, fast, monkeypatch):
    """A run whose hour has ended keeps MIN_WINDOW from its first try; then the node gives up,
    and keeps the directory."""
    monkeypatch.setattr(node_upload, "MIN_WINDOW", timedelta(seconds=0.3))
    flaky = Flaky()
    began = time.monotonic()
    with pytest.raises(node_upload.UploadGaveUp, match="upload of batch 7 not accepted"):
        await upload_through(flaky, tmp_path, pulse(start=datetime.now(UTC) - timedelta(hours=2)))
    assert 0.3 <= time.monotonic() - began < 5
    assert flaky.tries > OLD_ATTEMPTS
    assert (tmp_path / "run").exists()


async def test_the_node_s_shutdown_ends_the_wait_with_one_last_try(tmp_path, monkeypatch):
    """A stop during a long wait (here Retry-After 30 s) makes one more try at once, then the
    upload gives up."""
    active = pulse()
    flaky = Flaky()

    async def stop_soon():
        await flaky.tried.wait()
        await asyncio.sleep(0.1)
        active.stop.set()

    stopper = asyncio.create_task(stop_soon())
    began = time.monotonic()
    with pytest.raises(node_upload.UploadGaveUp):
        await upload_through(flaky, tmp_path, active)
    await stopper
    assert flaky.tries == 2 and time.monotonic() - began < 5


def test_the_window_lasts_until_the_run_s_hour_ends():
    now = datetime(2026, 11, 26, 19, 20, tzinfo=UTC)
    assert node_upload.window_end(datetime(2026, 11, 26, 19, 5, tzinfo=UTC), now) == datetime(
        2026, 11, 26, 20, 0, tzinfo=UTC
    )
    # Finished just before its hour ends, or with no start known: at least MIN_WINDOW.
    late = datetime(2026, 11, 26, 19, 58, tzinfo=UTC)
    assert node_upload.window_end(datetime(2026, 11, 26, 19, 55, tzinfo=UTC), late) == (
        late + timedelta(minutes=10)
    )
    assert node_upload.window_end(None, now) == now + timedelta(minutes=10)


def test_the_backoff_doubles_up_to_its_cap_with_jitter_or_the_longer_retry_after():
    assert 2 <= node_upload.backoff(2, 0) <= 2.5
    assert 8 <= node_upload.backoff(2, 2) <= 10
    for attempt in (5, 6, 1000):
        assert 60 <= node_upload.backoff(2, attempt) <= 75
    assert node_upload.backoff(2, 0, wait=300) == 300


def test_a_census_upload_keeps_the_session_s_reply_timeout():
    assert node_upload.timeout("census") is None
    limit = node_upload.timeout("pulse")
    assert limit is not None and limit.sock_read == node_upload.READ_SECONDS == 60
    window = node_upload.Window.of(Active({"kind": "census", "start_at": "2026-11-26T19:00Z"}), 2)
    assert window.options == {}
    assert node_upload.Window.of(None, 2).options == {}


async def test_a_blocked_orchestrator_loses_no_run_and_alerts_while_blocked(
    database, db, client, tmp_path, fast, monkeypatch
):
    """The acceptance case on the real upload path: every try is answered 503 ingest_busy for
    far more than the old 8 tries; the alert opens; once the path recovers the node's next try
    is stored, and the alert is acknowledged."""
    watch = ResultsWatch()
    monkeypatch.setattr(results_watch, "WATCH", watch)
    batch_id, headers = await planned_run(database, db, client)
    directory = await measurement(tmp_path / "run", requests=3)
    agent = agent_for(str(client.make_url("")), tmp_path, product="residential")
    agent._headers = headers
    agent.active[batch_id] = Active(
        {"batch_id": batch_id, "kind": "pulse", "start_at": datetime.now(UTC).isoformat()}
    )
    tries, many = [], asyncio.Event()
    call = agent._call

    async def counted(method, path, **options):
        tries.append(path)
        if len(tries) > 2 * OLD_ATTEMPTS:
            many.set()
        return await call(method, path, **options)

    agent._call = counted
    await set_setting(db, "results.ingest_concurrency", 0)
    async with aiohttp.ClientSession() as agent.session:
        upload = asyncio.create_task(agent.upload(batch_id, directory))
        await asyncio.wait_for(many.wait(), 10)
        assert not upload.done()
        assert query(database, "SELECT status FROM batch_runs") == [("uploading",)]
        assert watch.waiting_at is not None
        watch.waiting_at = time.monotonic() - 299  # as if the uploads had waited 299 s
        assert await check(db, watch) == {}
        watch.waiting_at -= 2
        stalled = await check(db, watch)
        assert stalled["stalled_s"] >= 301 and stalled["in_flight"] in (0, 1)
        await set_setting(db, "results.ingest_concurrency", 2000)
        reply = await asyncio.wait_for(upload, 10)
    assert reply["ingested"] is True and reply["run_key"]
    assert query(database, "SELECT count(*) FROM test_runs") == [(1,)]
    assert query(database, "SELECT status FROM batch_runs") == [("done",)]
    open_alerts = "SELECT count(*) FROM alerts WHERE kind=%s AND acknowledged_at IS NULL"
    assert query(database, open_alerts, (results_watch.KIND,)) == [(1,)]
    assert await check(db, watch) == {"recovered": True}
    assert query(database, open_alerts, (results_watch.KIND,)) == [(0,)]
    assert watch.open == 0


def test_the_watch_counts_only_uploads_a_node_retries():
    watch = ResultsWatch(alerted_at=None)
    watch.began(now=0)
    watch.ended(409, now=10)  # refused for good: an answer, no stall
    assert watch.stalled(400, 300) is None
    watch.began(now=1000)  # after a quiet spell: the wait begins now, not at the last 2xx
    assert watch.stalled(1010, 300) is None
    watch.ended(503, now=1010)  # answered with a status the node retries: still waiting
    assert watch.stalled(1020, 300) is None
    assert watch.stalled(1299, 300) is None and watch.stalled(1301, 300) == 301
    watch.began(now=1302)
    watch.ended(200, now=1303)
    assert watch.stalled(5000, 300) is None  # a 2xx, then no upload: quiet, no stall
    watch.began(now=5001)
    watch.began(now=5002)
    watch.ended(200, now=5003)  # one answered; the other waits from that 2xx
    assert watch.stalled(5302, 300) is None and watch.stalled(5304, 300) == 301
    watch.ended(429, now=5304)
    watch.began(now=5305)
    watch.ended(404, now=5306)  # a refusal for good does not end a retried wait
    assert watch.stalled(5306, 300) == 303
    assert watch.open == 0


async def test_the_alert_left_open_by_an_earlier_process_is_acknowledged_by_the_first_2xx(
    database, db
):
    async with db.transaction() as conn:
        await raise_alert(conn, results_watch.KIND, "left open", severity="major")
    watch = ResultsWatch()
    assert await check(db, watch) == {}  # no 2xx yet: it may still be stalled
    watch.began()
    watch.ended(200)
    assert await check(db, watch) == {"recovered": True}
    assert await check(db, watch) == {}
    acknowledged = "SELECT acknowledged_at IS NOT NULL FROM alerts WHERE kind=%s"
    assert query(database, acknowledged, (results_watch.KIND,)) == [(True,)]
