"""The browser bundle outbox on the host (browser/rawfiles.py, the mini's side of K-BP7): its
confirmation rule, markers, retries and parallelism, against a fake orchestrator in-process
(no manager, no Spaces)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from browser_rawfiles_cases import RUN_ID, make_run, seal_dir

from atlas.browser import contracts as bc
from atlas.browser import rawfiles
from atlas.browser.rawfiles import Outbox
from atlas.node import NodeAgent, NodeSettings

# --- outbox: confirmation, markers, retries -----------------------------------------------


class Ingest:
    """A fake orchestrator's /v1/results speaking K-BP7: `replies` answer in turn (the last
    repeats); "ok" is the confirming 200 for the bytes received."""

    def __init__(self, replies) -> None:
        self.replies = list(replies)
        self.posts: list[dict] = []
        self.app = web.Application(client_max_size=64 << 20)
        self.app.router.add_post("/v1/results", self.result)

    async def result(self, request):
        body = await request.read()
        sha = hashlib.sha256(body).hexdigest()
        self.posts.append(
            {
                "batch_id": request.query["batch_id"],
                "headers": dict(request.headers),
                "sha256": sha,
            }
        )
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if reply == "ok":
            archived = {"key": "browser-files/x.tar.gz", "sha256": sha}
            reply = (200, {"ok": True, "ingested": True, "status": "done", "archived": archived})
        status, document, *headers = reply
        return web.json_response(document, status=status, headers=(headers or [None])[0])


@pytest.fixture
async def agent_for(tmp_path):
    servers, sessions = [], []

    async def make(ingest):
        server = TestServer(ingest.app)
        await server.start_server()
        servers.append(server)
        agent = NodeAgent(
            NodeSettings(
                str(server.make_url("")),
                "token-shifter",
                provider="shifter",
                product="residential",
                proxy_factory=lambda country: None,
                work_dir=tmp_path / "work",
                region="browser",
            )
        )
        agent.session = aiohttp.ClientSession()
        sessions.append(agent.session)
        return agent

    yield make
    for session in sessions:
        await session.close()
    for server in servers:
        await server.close()


async def send_one(tmp_path, agent_for, replies, clock=None):
    ingest = Ingest(replies)
    agent = await agent_for(ingest)
    bundle = seal_dir(make_run(tmp_path), tmp_path / "outbox")
    outbox = Outbox(tmp_path / "outbox", **({"clock": clock} if clock else {}))
    return ingest, agent, bundle, outbox


async def test_a_confirmed_bundle_is_deleted_and_the_headers_are_k_bp7(tmp_path, agent_for, caplog):
    caplog.set_level(logging.INFO)
    ingest, agent, bundle, outbox = await send_one(tmp_path, agent_for, ["ok"])
    local = hashlib.sha256(bundle.archive.read_bytes()).hexdigest()
    assert await outbox.send(bundle, agent) == "confirmed"
    assert not bundle.archive.exists() and not bundle.sidecar.exists()
    # BP-VERIFY-1: the log names the SHA-256 the manager archived.
    assert f"bundle {RUN_ID} of batch 41 confirmed (sha256 {local}) and deleted" in caplog.text
    [post] = ingest.posts
    assert post["batch_id"] == "41" and post["sha256"] == local
    assert post["headers"][bc.HEADER_SHA256] == local
    assert post["headers"][bc.HEADER_BUNDLE_ID] == RUN_ID  # summary.run_id
    assert post["headers"]["Content-Type"] == bc.CONTENT_TYPE
    assert post["headers"]["Authorization"] == "Bearer token-shifter"
    assert outbox.hour_stats() == {
        "outbox_pending": 0,
        "outbox_bytes": 0,
        "confirmed_last_hour": 1,
        "rejected": 0,
        "conflicts": 0,
    }
    assert outbox.hour_stats()["confirmed_last_hour"] == 0


async def test_a_discarded_run_ingested_false_is_deleted(tmp_path, agent_for):
    _, agent, bundle, outbox = await send_one(tmp_path, agent_for, [(200, {"ingested": False})])
    assert await outbox.send(bundle, agent) == "confirmed"
    assert outbox.bundles() == []


@pytest.mark.parametrize(
    ("reply", "outcome", "marker"),
    [
        ((200, {"ingested": True, "archived": {"sha256": "0" * 64}}), "conflict", ".conflict"),
        ((200, {"ok": True, "status": "done"}), "conflict", ".conflict"),
        ((409, {"error": "bundle_changed"}), "conflict", ".conflict"),
        ((403, {"error": "another host"}), "rejected", ".rejected"),
        ((400, {"error": "the manifest is for another batch"}), "rejected", ".rejected"),
        ((429, {"error": "busy"}, {"Retry-After": "30"}), "retry", None),
        ((503, {"error": "archive unavailable"}), "retry", None),
        ((500, {"error": "boom"}), "retry", None),
    ],
)
async def test_every_other_reply_keeps_the_bundle(tmp_path, agent_for, reply, outcome, marker):
    ingest, agent, bundle, outbox = await send_one(tmp_path, agent_for, [reply])
    assert await outbox.send(bundle, agent) == outcome
    assert bundle.archive.exists() and bundle.sidecar.exists()
    if marker:
        note = json.loads(bundle.sidecar.with_suffix(marker).read_text())
        assert note["status"] == reply[0]
        assert outbox.pending() == [] and len(outbox.bundles()) == 1
    else:
        assert outbox.pending() == [bundle] and not outbox.due(bundle)
    stats = outbox.hour_stats()
    assert stats["outbox_bytes"] == bundle.bytes
    assert (stats["conflicts"], stats["rejected"]) == (
        int(marker == ".conflict"),
        int(marker == ".rejected"),
    )
    assert len(ingest.posts) == 1


async def test_a_reply_that_is_not_json_keeps_the_bundle(tmp_path, agent_for):
    class Text(Ingest):
        async def result(self, request):
            await request.read()
            status = self.replies.pop(0)
            return web.Response(status=status, text="<html>proxy error</html>")

    ingest = Text([502, 200])
    agent = await agent_for(ingest)
    bundle = seal_dir(make_run(tmp_path), tmp_path / "outbox")
    outbox = Outbox(tmp_path / "outbox")
    assert await outbox.send(bundle, agent) == "retry"
    outbox._retry.clear()
    assert await outbox.send(bundle, agent) == "conflict"
    assert bundle.archive.exists()


async def test_a_checksum_mismatch_is_rehashed_and_resent_once(tmp_path, agent_for):
    mismatch = (400, {"error": bc.ERROR_CHECKSUM_MISMATCH})
    ingest, agent, bundle, outbox = await send_one(tmp_path, agent_for, [mismatch])
    assert await outbox.send(bundle, agent) == "rejected"
    assert len(ingest.posts) == 2 and bundle.sidecar.with_suffix(".rejected").exists()
    ingest, agent, bundle, outbox = await send_one(tmp_path / "2", agent_for, [mismatch, "ok"])
    assert await outbox.send(bundle, agent) == "confirmed"
    assert len(ingest.posts) == 2 and not bundle.archive.exists()


async def test_503_three_times_then_200_sends_four_times_and_deletes_once(tmp_path, agent_for):
    now = [1000.0]
    busy = (503, {"error": "archive unavailable"})
    ingest, agent, bundle, outbox = await send_one(
        tmp_path, agent_for, [busy, busy, busy, "ok"], clock=lambda: now[0]
    )
    delays = []
    for _ in range(3):
        assert await outbox.send(bundle, agent) == "retry"
        assert not outbox.due(bundle)
        delays.append(outbox._retry[bundle.run_id][1] - now[0])
        now[0] += rawfiles.BACKOFF_MAX_S
        assert outbox.due(bundle)
    assert delays == [30.0, 60.0, 120.0]  # exponential backoff, at most 5 minutes
    deleted = []
    real = outbox.delete
    outbox.delete = lambda b: (deleted.append(b.run_id), real(b))
    assert await outbox.send(bundle, agent) == "confirmed"
    assert len(ingest.posts) == 4 and deleted == [RUN_ID] and outbox.bundles() == []


async def test_retry_after_is_honoured_and_a_network_error_keeps_the_bundle(tmp_path, agent_for):
    now = [0.0]
    busy = (429, {"error": "busy"}, {"Retry-After": "250"})
    ingest, agent, bundle, outbox = await send_one(
        tmp_path, agent_for, [busy], clock=lambda: now[0]
    )
    assert await outbox.send(bundle, agent) == "retry"
    assert outbox._retry[bundle.run_id][1] == 250.0
    agent.base = "http://127.0.0.1:9"  # nothing listens: connection refused
    assert await outbox.send(bundle, agent) == "retry"
    assert bundle.archive.exists() and outbox._retry[bundle.run_id][0] == 2


async def test_at_most_two_uploads_run_at_once(tmp_path, agent_for):
    gate, inside, peak = asyncio.Event(), [0], [0]

    class Slow(Ingest):
        async def result(self, request):
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
            await gate.wait()
            inside[0] -= 1
            return await super().result(request)

    ingest = Slow(["ok"])
    agent = await agent_for(ingest)
    outbox = Outbox(tmp_path / "outbox")
    bundles = []
    for n in range(4):
        run_id = f"0b6a7e4c-1d2e-4f3a-9b8c-7d6e5f4a3b2{n}"
        bundles.append(seal_dir(make_run(tmp_path / str(n), run_id), tmp_path / "outbox"))
    sends = asyncio.gather(*(outbox.send(bundle, agent) for bundle in bundles))
    await asyncio.sleep(0.2)
    gate.set()
    assert await sends == ["confirmed"] * 4 and peak[0] == rawfiles.INGEST_PARALLEL


def test_pending_skips_unreadable_sidecars_and_sidecars_without_their_tar_gz(tmp_path):
    outbox = tmp_path / "outbox"
    bundle = seal_dir(make_run(tmp_path), outbox)
    (outbox / "broken.json").write_text("{")
    other = bc.outbox_paths(outbox, "0b6a7e4c-1d2e-4f3a-9b8c-7d6e5f4a3b2f")[1]
    other.write_text(bundle.sidecar.read_text())
    assert Outbox(outbox).pending() == [bundle]
    assert Outbox(tmp_path / "none").pending() == []
