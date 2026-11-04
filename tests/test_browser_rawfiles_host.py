"""Browser bundles on a running host (browser/node.py with browser/rawfiles.py): seal, send and
delete after each run, restarts, recovery at start, the outbox pass; a fake orchestrator
speaking K-BP7 in-process (no manager, no Spaces)."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import shutil
import tarfile
from datetime import timedelta

from aiohttp import web
from aiohttp.test_utils import TestServer
from test_browser_node import (
    CHEAP,
    SHIFTER,
    TOKENS,
    Fleet,
    Orchestrator,
    browser_work,
    host_view,
    make_state,
    serve_until,
)
from test_browser_rawfiles import CAMOUFOX, MEMBERS, RUN_ID, SLOT, make_run, seal_dir

from proxy_index.browser import contracts as bc
from proxy_index.browser import node as host_module
from proxy_index.browser import rawfiles
from proxy_index.browser.node import BrowserHost


class Kbp7(Orchestrator):
    """test_browser_node's fake orchestrator whose /v1/results answers as K-BP7 does."""

    def __init__(self, work, replies=("ok",)) -> None:
        super().__init__(work)
        self.replies = list(replies)
        self.posts: list[tuple[str, str, str]] = []  # (token, bundle id, sha256)

    async def result(self, request):
        body = await request.read()
        sha = hashlib.sha256(body).hexdigest()
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
            summary = json.load(archive.extractfile("summary.json"))
        self.posts.append((self.token(request), request.headers[bc.HEADER_BUNDLE_ID], sha))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        self.results[int(request.query["batch_id"])] = (self.token(request), summary)
        self.changed.set()
        if reply != "ok":
            return web.json_response({"error": "busy"}, status=reply)
        archived = {"key": "k", "sha256": sha}
        return web.json_response({"ok": True, "ingested": True, "archived": archived})


async def until(done, limit: float = 10) -> None:
    """Poll done() (files on disk have no event) every 20 ms for at most `limit` seconds."""
    for _ in range(int(limit / 0.02)):
        if done():
            return
        await asyncio.sleep(0.02)
    raise TimeoutError


async def serve_while(host: BrowserHost, done, limit: float = 10) -> None:
    """Run the host until done() holds, then stop it."""
    stop = asyncio.Event()
    task = asyncio.create_task(host.run(stop))
    try:
        await until(done, limit)
    finally:
        stop.set()
        await task


async def host_with(state, fake, **options):
    server = TestServer(fake.app)
    await server.start_server()
    host = BrowserHost(
        host_view(state),
        state,
        str(server.make_url("")),
        launch=Fleet().launch,
        poll_interval=0.02,
        heartbeat_interval=0.05,
        **options,
    )
    return host, server


async def test_a_host_seals_sends_and_deletes_and_logs_no_credentials(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(host_module, "DEADLINE_MARGIN_S", -600)
    caplog.set_level(logging.DEBUG)
    state = make_state(tmp_path)
    work = {TOKENS[SHIFTER]: [browser_work(1, "amazon_pdp")], TOKENS[CHEAP]: []}
    work[TOKENS[CHEAP]].append(browser_work(3, "amazon_pdp"))
    fake = Kbp7(work)
    host, server = await host_with(state, fake)
    outbox = state / "outbox"
    try:
        await serve_while(host, lambda: len(fake.posts) == 2 and not list(outbox.glob("*")))
    finally:
        await server.close()
    assert fake.failed == {}
    assert sorted(token for token, *_ in fake.posts) == sorted(TOKENS.values())
    assert list((state / "runs").glob("*/*")) == []  # sealed and removed
    text = caplog.text + (state / "logs" / host_module.LOG_NAME).read_text()
    assert "cheapsecret" not in text and not re.search(r"\bsecret\b", text)
    assert all(token not in text for token in TOKENS.values())


async def test_an_unconfirmed_upload_never_calls_failed_and_stays(tmp_path, monkeypatch):
    monkeypatch.setattr(host_module, "DEADLINE_MARGIN_S", -600)
    state = make_state(tmp_path)
    fake = Kbp7({TOKENS[SHIFTER]: [browser_work(1, "amazon_pdp")]}, replies=[503])
    host, server = await host_with(state, fake)
    try:
        await serve_until(host, fake, lambda: len(fake.posts) == 1)
    finally:
        await server.close()
    assert fake.failed == {}
    [bundle] = rawfiles.Outbox(state / "outbox").pending()
    assert bundle.batch_id == 1 and bundle.network == SHIFTER
    with tarfile.open(bundle.archive, "r:gz") as tar:
        names = tar.getnames()
        records = [json.loads(line) for line in tar.extractfile(MEMBERS.tries)]
    assert MEMBERS.manifest in names and len(records) == 3
    for record in records:
        assert not bc.FORBIDDEN_FIELDS & set(record)


async def test_a_restart_sends_three_pending_bundles_oldest_first(tmp_path):
    state = make_state(tmp_path)
    ids = [f"0b6a7e4c-1d2e-4f3a-9b8c-7d6e5f4a3b2{n}" for n in range(3)]
    for n, slot in ((2, SLOT), (0, SLOT - timedelta(hours=2)), (1, SLOT - timedelta(hours=1))):
        seal_dir(make_run(tmp_path / f"r{n}", ids[n], slot=slot), state / "outbox")
    fake = Kbp7({})
    host, server = await host_with(state, fake)
    host.outbox.parallel = asyncio.Semaphore(1)  # one at a time, so arrival order is send order
    try:
        await serve_while(host, lambda: not rawfiles.Outbox(state / "outbox").bundles())
    finally:
        await server.close()
    assert [bundle_id for _, bundle_id, _ in fake.posts] == ids
    assert {token for token, *_ in fake.posts} == {TOKENS[SHIFTER]} and fake.failed == {}


async def test_a_host_start_seals_a_finished_run_and_deletes_a_half_run(tmp_path, caplog):
    """BP-DATA-HALF-1: a run killed mid-run (no summary.json) is deleted at start and never
    uploaded; a run killed after it finished is sealed and uploaded as before."""
    state = make_state(tmp_path)
    run_dir = make_run(state, RUN_ID)  # state/runs/shifter.residential/<run id>
    killed = make_run(state, "0b6a7e4c-1d2e-4f3a-9b8c-7d6e5f4a3b2d")
    (killed / "summary.json").unlink()
    caplog.set_level(logging.WARNING)
    fake = Kbp7({})
    host, server = await host_with(state, fake)
    try:
        await serve_while(host, lambda: len(fake.posts) == 1)
    finally:
        await server.close()
    assert not run_dir.exists() and not killed.exists()
    assert (host.discarded, host.unsealed) == (1, 0)
    assert [bundle_id for _, bundle_id, _ in fake.posts] == [RUN_ID]
    assert caplog.text.count(f"browser_run_discarded: run {killed.name} ") == 1


async def test_a_run_that_cannot_be_sealed_is_kept_and_not_failed(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(host_module, "DEADLINE_MARGIN_S", -600)

    def refuse(*args, **kwargs):
        raise OSError("disk")

    monkeypatch.setattr(rawfiles, "pack", refuse)
    state = make_state(tmp_path)
    fake = Kbp7({TOKENS[SHIFTER]: [browser_work(1, "amazon_pdp")]})
    host, server = await host_with(state, fake)
    caplog.set_level(logging.WARNING)
    stop = asyncio.Event()
    task = asyncio.create_task(host.run(stop))
    try:
        await until(lambda: "run not sealed" in caplog.text)
    finally:
        stop.set()
        await task
        await server.close()
    assert fake.failed == {} and fake.posts == []
    [run_dir] = (state / "runs" / "shifter.residential").iterdir()
    assert (run_dir / "summary.json").exists() and host.unsealed == 1 and host.discarded == 0
    assert host.sealing == set()


async def test_a_host_with_an_unreadable_sidecar_still_starts(tmp_path):
    """Review F1 at the host: the start goes on, other runs are sealed and sent."""
    state = make_state(tmp_path)
    stuck = make_run(state, RUN_ID)
    shutil.copytree(stuck, tmp_path / "keep")
    seal_dir(stuck, state / "outbox")
    shutil.copytree(tmp_path / "keep", stuck)
    bc.outbox_paths(state / "outbox", RUN_ID)[1].write_text("{")
    other = make_run(state, "0b6a7e4c-1d2e-4f3a-9b8c-7d6e5f4a3b2d")
    fake = Kbp7({})
    host, server = await host_with(state, fake)
    try:
        await serve_while(host, lambda: len(fake.posts) == 1)
    finally:
        await server.close()
    assert stuck.exists() and not other.exists() and host.unsealed == 1
    assert fake.posts[0][1] == other.name


def registered(host: BrowserHost) -> None:
    for agent in host.agents:
        agent.node_id, agent.session = 1, object()


async def test_a_pass_skips_a_bundle_whose_seal_has_not_ended(tmp_path):
    """Review F2: a bundle renamed into the outbox but not yet re-hashed is not sent."""
    state = make_state(tmp_path)
    seal_dir(make_run(tmp_path, RUN_ID), state / "outbox")
    host = BrowserHost(
        host_view(state), state, "http://orchestrator.invalid", launch=Fleet().launch
    )
    registered(host)
    sent = []

    async def send(bundle, agent):
        sent.append(bundle.run_id)
        return "confirmed"

    host.outbox.send = send
    host.sealing.add(RUN_ID)
    assert await host.flush() == [] and sent == []
    host.sealing.clear()
    assert await host.flush() == ["confirmed"] and sent == [RUN_ID]
    host.log.close()


async def test_one_failing_send_does_not_end_the_pass(tmp_path, caplog):
    """Review F3: every send is awaited; one that raises is logged by type and kept."""
    state = make_state(tmp_path)
    ids = [f"0b6a7e4c-1d2e-4f3a-9b8c-7d6e5f4a3b2{n}" for n in range(3)]
    for n, run_id in enumerate(ids):
        slot = SLOT + timedelta(hours=n)
        seal_dir(make_run(tmp_path / str(n), run_id, slot=slot), state / "outbox")
    host = BrowserHost(
        host_view(state), state, "http://orchestrator.invalid", launch=Fleet().launch
    )
    registered(host)
    finished = []

    async def send(bundle, agent):
        await asyncio.sleep(0.01 * ids.index(bundle.run_id))
        if bundle.run_id == ids[0]:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        finished.append(bundle.run_id)
        return "confirmed"

    host.outbox.send = send
    caplog.set_level(logging.WARNING)
    assert await host.flush() == ["error", "confirmed", "confirmed"]
    assert finished == ids[1:] and f"bundle {ids[0]} not sent (UnicodeDecodeError)" in caplog.text
    assert len(host.outbox.pending()) == 3
    host.log.close()


async def test_a_launched_browser_identity_goes_into_the_manifest(tmp_path):
    state = make_state(tmp_path)
    fleet = Fleet()

    async def launch():
        browser = await fleet.launch()
        browser.identity = dict(CAMOUFOX)
        return browser

    host = BrowserHost(host_view(state), state, "http://orchestrator.invalid", launch=launch)
    assert host.identity["release"] == host_module.release.RELEASE
    await host._launch_one()
    assert host.identity == CAMOUFOX
    host.log.close()
