"""node.py paths the node end-to-end tests do not reach (COV-7): token and memory edge cases,
the register retry, failed polls and heartbeats, a run the node's shutdown cancels, MMDB
enrichment, rejected and failed reports, exhausted uploads and the signal-driven serve().

The orchestrator is a recorder in place of NodeAgent._call, so each reply (or failure) is
chosen per path; the runs themselves go through the real runner with a fake transport."""

import asyncio
import os
import signal
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiohttp
import pytest
from helpers import FakeTransport

from atlas import node as node_module
from atlas.models import ProxyConfig
from atlas.node import (
    Active,
    NodeAgent,
    NodeSettings,
    Rejected,
    read_token,
    resident_mb,
    serve,
)


class Orchestrator:
    """Stands in for NodeAgent._call: records every call and answers from `replies` (path ->
    list of replies, the last one repeated; an exception in the list is raised)."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    async def __call__(self, method, path, **options):
        self.calls.append((method, path, options.get("json")))
        queue = self.replies.get(path, [{}])
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def paths(self):
        return [path for _, path, _ in self.calls]


def make_agent(tmp_path, replies=None, **options):
    agent = NodeAgent(
        NodeSettings(
            "http://orchestrator",
            "tok",
            provider="p",
            product="q",
            region="fra1",
            proxy_factory=lambda country: ProxyConfig("http", "127.0.0.1", 9),
            work_dir=tmp_path,
            **{"transport_factory": FakeTransport} | options,
        )
    )
    agent._call = Orchestrator(replies or {})
    return agent


def assignment(**overrides):
    return {
        "batch_id": 7,
        "kind": "pulse",
        "country_code": "DE",
        "target_id": "generic_https",
        "timeout_ms": 9000,
        "concurrency": 1,
        "stop_rule": "count",
        "requests": 3,
        "start_at": datetime.now(UTC).isoformat(),
    } | overrides


def test_a_token_path_that_is_not_a_regular_file_is_refused():
    with pytest.raises(ValueError, match="regular file"):
        read_token(os.devnull)


def test_resident_memory_falls_back_to_the_peak_then_to_none(monkeypatch):
    def unreadable(*args, **kwargs):
        raise OSError("no /proc")

    monkeypatch.setattr(node_module, "open", unreadable, raising=False)
    usage = SimpleNamespace(ru_maxrss=300 << 20)
    fake = SimpleNamespace(RUSAGE_SELF=0, getrusage=lambda who: usage)
    monkeypatch.setitem(__import__("sys").modules, "resource", fake)
    monkeypatch.setattr(node_module.sys, "platform", "linux")
    assert resident_mb() == 300 << 10  # Linux reports KiB
    monkeypatch.setattr(node_module.sys, "platform", "darwin")
    assert resident_mb() == 300  # macOS reports bytes
    monkeypatch.setitem(__import__("sys").modules, "resource", None)
    assert resident_mb() is None


async def test_register_retries_and_shutdown_releases_a_waiting_run(tmp_path, caplog):
    later = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    replies = {
        "/v1/register": [OSError("down"), {"node_id": 3, "region": "fra1"}],
        "/v1/work": [{"assignments": [assignment(start_at=later)]}],
    }
    agent = make_agent(tmp_path, replies, poll_interval=0.01, heartbeat_interval=0.01)
    polled, poll = asyncio.Event(), agent.poll

    async def polling():
        await poll()
        polled.set()

    agent.poll = polling
    stop = asyncio.Event()
    serving = asyncio.create_task(agent.run(stop))
    await asyncio.wait_for(polled.wait(), 5)
    assert list(agent.active) == [7]
    stop.set()
    await asyncio.wait_for(serving, 5)
    assert "register failed (OSError); retrying" in caplog.text
    assert agent.node_id == 3
    # The run never started: it is released for the orchestrator to hand out again.
    assert agent.active == {} and agent.tasks == set()
    assert "/v1/progress" not in agent._call.paths()
    assert agent._call.paths().count("/v1/register") == 2


async def test_failed_polls_and_heartbeats_are_logged_and_survived(tmp_path, caplog):
    agent = make_agent(
        tmp_path,
        {"/v1/work": [Rejected("GET /v1/work answered 403")], "/v1/heartbeat": [OSError()]},
    )
    await agent.poll()
    assert agent.active == {} and "work poll failed (Rejected)" in caplog.text
    agent.active[1] = Active(assignment(), concurrency=5, requests=50)
    agent._rate_mark = (agent._rate_mark[0] - 10, 0)
    await agent.heartbeat()
    assert "heartbeat failed (OSError)" in caplog.text
    body = agent._call.calls[-1][2]
    assert body["open_conns"] == 5 and 0 < body["req_per_s"] <= 5


async def test_a_kill_stops_only_the_active_batch_it_names(tmp_path):
    work = {"kill": [7, 99], "assignments": [assignment()]}
    agent = make_agent(tmp_path, {"/v1/work": [work]})
    active = Active(assignment())
    agent.active[7] = active
    await agent.poll()
    # Batch 7 is stopped as the orchestrator's kill; 99 is unknown here and the
    # repeated assignment of 7 does not start a second run.
    assert active.killed and active.stop.is_set()
    assert agent.active == {7: active} and agent.tasks == set()


def test_an_unknown_target_without_a_usable_url_is_refused(tmp_path):
    agent = make_agent(tmp_path)
    with pytest.raises(ValueError):
        agent.targets(assignment(target_id="not_built_in", target_url="ftp://example.test/"))
    with pytest.raises(ValueError):
        agent.targets(assignment(target_id="not_built_in"))


async def test_a_progress_post_the_orchestrator_rejects_stops_the_run(tmp_path):
    agent = make_agent(tmp_path, {"/v1/progress": [Rejected("POST /v1/progress answered 404")]})
    active = Active(assignment())
    await agent._progress(active, {"requests": 12})
    assert active.requests == 12 and active.killed and active.stop.is_set()


async def test_a_run_cancelled_by_the_nodes_shutdown_is_reported_and_discarded(tmp_path):
    active = Active(assignment(requests=100))

    class Stopping(FakeTransport):
        async def execute(self, target, *, context=None):
            active.stop.set()  # the node shuts down while the request is in flight
            await asyncio.sleep(10)

    agent = make_agent(tmp_path, transport_factory=Stopping)
    agent.active[7] = active
    await asyncio.wait_for(agent.execute(active), 10)
    assert ("POST", "/v1/failed", {"batch_id": 7, "error": "node_shutdown"}) in agent._call.calls
    assert "/v1/results" not in agent._call.paths()
    assert list(tmp_path.iterdir()) == [] and agent.active == {}


async def test_a_finished_run_is_enriched_then_uploaded(tmp_path, monkeypatch):
    enriched, uploaded = [], []
    monkeypatch.setattr(
        "atlas.enrichment.enrich_run", lambda directory, mmdb: enriched.append(mmdb)
    )
    agent = make_agent(tmp_path, mmdb="dbip.mmdb")

    async def upload(batch_id, directory):
        uploaded.append((batch_id, (directory / "summary.json").exists()))

    agent.upload = upload
    await agent.execute(Active(assignment()))
    assert enriched == ["dbip.mmdb"] and uploaded == [(7, True)]
    assert agent._call.paths() == ["/v1/progress", "/v1/progress"]


async def test_a_rejected_upload_is_final_and_not_reported_failed(tmp_path, caplog):
    agent = make_agent(tmp_path)

    async def upload(batch_id, directory):
        raise Rejected("POST /v1/results answered 409: duplicate")

    agent.upload = upload
    await agent.execute(Active(assignment()))
    assert "batch 7: POST /v1/results answered 409: duplicate" in caplog.text
    assert "/v1/failed" not in agent._call.paths()


async def test_a_failure_report_that_cannot_be_sent_is_logged(tmp_path, caplog):
    agent = make_agent(tmp_path, {"/v1/failed": [aiohttp.ClientError()]})
    # A target this build does not know, with no URL to run it from, fails the batch.
    await agent.execute(Active(assignment(target_id="not_built_in")))
    assert "batch 7 failed (ValueError)" in caplog.text
    assert "could not report batch 7 failed (ClientError)" in caplog.text


async def test_an_upload_that_never_gets_through_gives_up(tmp_path, monkeypatch):
    monkeypatch.setattr(node_module, "UPLOAD_ATTEMPTS", 2)
    agent = make_agent(tmp_path, {"/v1/results": [OSError("unreachable")]}, upload_backoff=0)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text("{}")
    with pytest.raises(RuntimeError, match="upload attempts exhausted"):
        await agent.upload(7, run_dir)
    assert agent._call.paths() == ["/v1/results", "/v1/results"]
    assert run_dir.exists()  # kept for the next try


async def test_serve_stops_the_agent_on_sigterm(tmp_path):
    agent = make_agent(tmp_path)
    seen = []

    async def run(stop):
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), 5)
        seen.append(stop.is_set())

    agent.run = run
    loop = asyncio.get_running_loop()
    try:
        await serve(agent)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
    assert seen == [True]
