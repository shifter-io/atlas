"""The runner: summary and IP artifacts, cancellation, pacing, sinks, summary keys and progress.

Stop rules are in `test_runner_stops.py`, geo rows in `test_runner_geo.py`; the shared run
configuration is in `runner_cases.py`.
"""

import asyncio
import gzip
import hashlib
import json

import pytest
from helpers import FakeTransport
from runner_cases import config

from atlas import JsonSink, RunOptions, run
from atlas.models import Outcome


async def test_summary_artifacts_and_exact_ip_counts(tmp_path):
    sink = JsonSink(tmp_path)
    result = await run(
        config(requests=10000, concurrency=31), sink, RunOptions(transport_factory=FakeTransport)
    )
    assert result.status == "completed"
    assert result.completed == result.started == result.successes == 10000
    assert result.unique_ips == 1
    assert result.latency_ms == dict.fromkeys(("p50", "p90", "p95", "p99"), 11)
    assert result.latency_mean_ms == 11
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

    result = await run(config(requests=10), JsonSink(tmp_path), RunOptions(transport_factory=Mixed))
    assert result.errors == {"timeout": 5}
    assert result.success_rate == 0.5
    assert result.latency_ms["p99"] == 2
    assert result.latency_mean_ms == 2
    assert result.unique_ips == 1


async def test_task_cancellation_persists_partial_then_propagates(tmp_path):
    entered = asyncio.Event()

    class Slow(FakeTransport):
        async def execute(self, target, *, context=None):
            entered.set()
            await asyncio.sleep(10)

    sink = JsonSink(tmp_path)
    task = asyncio.create_task(run(config(), sink, RunOptions(transport_factory=Slow)))
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
    result = await run(
        config(), JsonSink(tmp_path), RunOptions(stop=stop, transport_factory=FakeTransport)
    )
    assert result.status == "cancelled"
    assert result.started == 0 and result.not_started == 100
    assert result.success_rate is None


async def test_programming_errors_fail_run_without_leaking_secrets(tmp_path):
    class Broken(FakeTransport):
        async def execute(self, target, *, context=None):
            raise RuntimeError("http://user:secret@localhost/")

    sink = JsonSink(tmp_path)
    with pytest.raises(RuntimeError) as exc:
        await run(config(requests=3), sink, RunOptions(transport_factory=Broken))
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

    await run(
        config(requests=5, concurrency=3, requests_per_second=20),
        JsonSink(tmp_path),
        RunOptions(transport_factory=Timed),
    )
    assert len(starts) == 5
    assert starts[-1] - starts[0] >= 0.18


async def test_bucket_boundary_is_not_run_start_time(tmp_path, monkeypatch):
    # Advance the monotonic clock from minute 4:59 to 5:01 between two results.
    import atlas.runner as runner

    clock = [0.0]
    monkeypatch.setattr(runner.time, "time", lambda: 299.0)
    monkeypatch.setattr(runner.time, "perf_counter", lambda: clock[0])

    class Advancing(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            clock[0] = (self.count - 1) * 2
            return Outcome(2, ip="8.8.8.8", status=200)

    sink = JsonSink(tmp_path)
    result = await run(
        config(requests=2, concurrency=1), sink, RunOptions(transport_factory=Advancing)
    )
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
        result = await run(config(requests=2), sink, RunOptions(transport_factory=FakeTransport))
        assert result.artifacts["ips"]["rows"] == 1


async def test_disk_failure_never_publishes_a_complete_summary(tmp_path):
    class FailingSink(JsonSink):
        def observations(self, rows):
            raise OSError("Disk full")

    sink = FailingSink(tmp_path)
    with pytest.raises(OSError):
        await run(config(requests=2), sink, RunOptions(transport_factory=FakeTransport))
    assert not (sink.directory / "summary.json").exists()


async def test_bucket_export_does_not_block_request_execution(tmp_path, monkeypatch):
    import threading

    import atlas.runner as runner

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

    result = await run(
        config(requests=3, concurrency=1),
        WaitingSink(tmp_path),
        RunOptions(transport_factory=Advancing),
    )
    assert result.successes == 3


OLD_SUMMARY_KEYS = {
    "run_id",
    "schema_version",
    "framework_version",
    "target",
    "config",
    "started_at",
    "finished_at",
    "duration_seconds",
    "status",
    "requested",
    "started",
    "completed",
    "cancelled",
    "not_started",
    "successes",
    "total_errors",
    "errors",
    "status_codes",
    "success_rate",
    "requests_per_second",
    "unique_ips",
    "pool_eligible",
    "latency_ms",
    "latency_histogram",
    "response_body_bytes",
    "buckets",
    "artifacts",
}
OLD_BUCKET_KEYS = {
    "bucket_start",
    "completed",
    "successes",
    "errors",
    "status_codes",
    "response_body_bytes",
    "latency_histogram",
}
NEW_COUNTERS = {"challenges", "blocks", "timeouts", "proxy_errors", "bytes_up", "bytes_down"}


async def test_summary_keeps_old_keys_and_adds_counters(tmp_path):
    class Mixed(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            return [
                Outcome(1, ip="8.8.8.8", status=200, bytes_up=10, bytes_down=20),
                Outcome(1, error="challenge", status=200, bytes_up=10, bytes_down=900),
                Outcome(1, error="http_status", status=403, verdict="block"),
                Outcome(1, error="timeout"),
                Outcome(1, error="proxy_auth", status=407),
                Outcome(1, error="tls"),
            ][(self.count - 1) % 6]

    sink = JsonSink(tmp_path)
    result = await run(
        config(requests=12, concurrency=1), sink, RunOptions(transport_factory=Mixed)
    )
    saved = json.loads((sink.directory / "summary.json").read_text())
    assert OLD_SUMMARY_KEYS | NEW_COUNTERS | {"stop_reason", "latency_mean_ms"} == set(saved)
    assert set(saved["latency_ms"]) == {"p50", "p90", "p95", "p99"}
    assert saved["latency_mean_ms"] == 1
    assert set(saved["buckets"][0]) == OLD_BUCKET_KEYS | NEW_COUNTERS
    assert {k: saved[k] for k in NEW_COUNTERS} == {
        "challenges": 2,
        "blocks": 2,
        "timeouts": 2,
        "proxy_errors": 2,
        "bytes_up": 40,
        "bytes_down": 1840,
    }
    assert result.successes == 2 and result.total_errors == 10
    assert result.completed == result.successes + result.total_errors
    assert saved["stop_reason"] == "requests" and set(saved["artifacts"]) == {"ips"}


async def test_progress_is_throttled_and_final(tmp_path):
    calls = []

    class Slow(FakeTransport):
        async def execute(self, target, *, context=None):
            await asyncio.sleep(0.004)
            return await super().execute(target, context=context)

    async def progress(counters):
        calls.append((asyncio.get_running_loop().time(), counters))

    result = await run(
        config(requests=60, concurrency=2),
        JsonSink(tmp_path),
        RunOptions(transport_factory=Slow, progress=progress, progress_interval=0.03),
    )
    assert len(calls) >= 3
    assert set(calls[0][1]) == {
        "requests",
        "successes",
        "unique_ips",
        *NEW_COUNTERS,
        "execution_backend",
        "execution_processes",
    }
    times = [at for at, _ in calls[:-1]]
    assert all(b - a >= 0.025 for a, b in zip(times, times[1:], strict=False))
    requests = [c["requests"] for _, c in calls]
    assert requests == sorted(requests)
    assert calls[-1][1] == {
        "requests": 60,
        "successes": 60,
        "unique_ips": 1,
        "challenges": 0,
        "blocks": 0,
        "timeouts": 0,
        "proxy_errors": 0,
        "bytes_up": 0,
        "bytes_down": 0,
        "execution_backend": "inline",
        "execution_processes": 1,
    }
    assert result.completed == 60


async def test_progress_failure_does_not_end_the_run(tmp_path):
    def progress(counters):
        raise ConnectionError("orchestrator unreachable")

    result = await run(
        config(requests=5),
        JsonSink(tmp_path),
        RunOptions(transport_factory=FakeTransport, progress=progress, progress_interval=0.001),
    )
    assert result.completed == 5 and result.status == "completed"
    with pytest.raises(ValueError):
        await run(
            config(requests=5),
            JsonSink(tmp_path),
            RunOptions(transport_factory=FakeTransport, progress=progress, progress_interval=0),
        )
