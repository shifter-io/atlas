import asyncio
import gzip
import hashlib
import json

import pytest
from helpers import FakeTransport

from proxy_framework import JsonSink, ProxyConfig, RunConfig, run
from proxy_framework.models import Outcome


def config(**kwargs):
    return RunConfig("generic_http", ProxyConfig("http", "localhost", 8080), **kwargs)


async def test_summary_artifacts_and_exact_ip_counts(tmp_path):
    sink = JsonSink(tmp_path)
    result = await run(config(requests=10000, concurrency=31), sink,
                       transport_factory=FakeTransport)
    assert result.status == "completed"
    assert result.completed == result.started == result.successes == 10000
    assert result.unique_ips == 1
    assert result.latency_ms == dict.fromkeys(("p50", "p90", "p95", "p99"), 11)
    assert result.cancelled == result.not_started == result.total_errors == 0
    saved = json.loads((sink.directory / "summary.json").read_text())
    # JSON converts tuples in sparse histogram bins to arrays.
    assert saved == json.loads(json.dumps(result.to_dict()))
    path = sink.directory / saved["artifacts"]["ips"]["file"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == saved["artifacts"]["ips"]["sha256"]
    with gzip.open(path, "rt") as stream:
        rows = [json.loads(line) for line in stream]
    assert len(rows) == 1 and rows[0]["count"] == 10000
    assert rows[0]["first_seen"] <= rows[0]["last_seen"]


async def test_failed_requests_are_not_in_latency_or_ips(tmp_path):
    class Mixed(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            if self.count % 2:
                return Outcome(15000, error="timeout")
            return Outcome(1.2, ip="8.8.8.8", status=200)

    result = await run(config(requests=10), JsonSink(tmp_path), transport_factory=Mixed)
    assert result.errors == {"timeout": 5}
    assert result.success_rate == .5
    assert result.latency_ms["p99"] == 2
    assert result.unique_ips == 1


async def test_task_cancellation_persists_partial_then_propagates(tmp_path):
    entered = asyncio.Event()

    class Slow(FakeTransport):
        async def execute(self, target, *, context=None):
            entered.set()
            await asyncio.sleep(10)

    sink = JsonSink(tmp_path)
    task = asyncio.create_task(run(config(), sink, transport_factory=Slow))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = json.loads((sink.directory / "summary.json").read_text())
    assert result["status"] == "cancelled"
    assert result["started"] == result["completed"] + result["cancelled"]
    assert result["requested"] == result["started"] + result["not_started"]


async def test_stop_before_first_request(tmp_path):
    stop = asyncio.Event()
    stop.set()
    result = await run(config(), JsonSink(tmp_path), stop=stop, transport_factory=FakeTransport)
    assert result.status == "cancelled"
    assert result.started == 0 and result.not_started == 100
    assert result.success_rate is None


async def test_programming_errors_fail_run_without_leaking_secrets(tmp_path):
    class Broken(FakeTransport):
        async def execute(self, target, *, context=None):
            raise RuntimeError("http://user:secret@localhost/")

    sink = JsonSink(tmp_path)
    with pytest.raises(RuntimeError) as exc:
        await run(config(requests=3), sink, transport_factory=Broken)
    assert "secret" not in str(exc.value)
    saved = (sink.directory / "summary.json").read_text()
    assert "secret" not in saved
    assert json.loads(saved)["status"] == "failed"


async def test_rate_pacing(tmp_path):
    starts = []

    class Timed(FakeTransport):
        async def execute(self, target, *, context=None):
            starts.append(asyncio.get_running_loop().time())
            return await super().execute(target, context=context)

    await run(config(requests=5, concurrency=3, requests_per_second=20),
              JsonSink(tmp_path), transport_factory=Timed)
    assert len(starts) == 5
    assert starts[-1] - starts[0] >= .18


async def test_bucket_boundary_is_not_run_start_time(tmp_path, monkeypatch):
    # Advance the monotonic clock from minute 4:59 to 5:01 between two results.
    import proxy_framework.runner as runner

    clock = [0.0]
    monkeypatch.setattr(runner.time, "time", lambda: 299.0)
    monkeypatch.setattr(runner.time, "perf_counter", lambda: clock[0])

    class Advancing(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            clock[0] = (self.count - 1) * 2
            return Outcome(2, ip="8.8.8.8", status=200)

    sink = JsonSink(tmp_path)
    result = await run(config(requests=2, concurrency=1), sink, transport_factory=Advancing)
    assert result.unique_ips == 1
    assert len(result.buckets) == 2
    with gzip.open(sink.directory / "ips.jsonl.gz", "rt") as stream:
        rows = [json.loads(line) for line in stream]
    assert [r["count"] for r in rows] == [1, 1]
    assert rows[0]["bucket_start"] != rows[1]["bucket_start"]


def test_existing_output_is_not_overwritten(tmp_path):
    sink = JsonSink(tmp_path)
    sink.start("same")
    sink.abort()
    with pytest.raises(FileExistsError):
        JsonSink(tmp_path).start("same")


async def test_sink_can_be_reused_serially(tmp_path):
    sink = JsonSink(tmp_path)
    for _ in range(2):
        result = await run(config(requests=2), sink, transport_factory=FakeTransport)
        assert result.artifacts["ips"]["rows"] == 1


async def test_disk_failure_never_publishes_a_complete_summary(tmp_path):
    class FailingSink(JsonSink):
        def observations(self, rows):
            raise OSError("Disk full")

    sink = FailingSink(tmp_path)
    with pytest.raises(OSError):
        await run(config(requests=2), sink, transport_factory=FakeTransport)
    assert not (sink.directory / "summary.json").exists()


async def test_bucket_export_does_not_block_request_execution(tmp_path, monkeypatch):
    import threading

    import proxy_framework.runner as runner

    clock = [0.0]
    progressed = threading.Event()
    monkeypatch.setattr(runner.time, "time", lambda: 299.0)
    monkeypatch.setattr(runner.time, "perf_counter", lambda: clock[0])

    class WaitingSink(JsonSink):
        def observations(self, rows):
            assert progressed.wait(timeout=2), "IP export blocked the request loop"
            super().observations(rows)

    class Advancing(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            clock[0] = (self.count - 1) * 2
            if self.count == 3:
                progressed.set()
            await asyncio.sleep(0)
            return Outcome(2, ip="8.8.8.8", status=200)

    result = await run(config(requests=3, concurrency=1), WaitingSink(tmp_path),
                       transport_factory=Advancing)
    assert result.successes == 3
