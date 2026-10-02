import asyncio
import gzip
import hashlib
import json
from datetime import datetime

import pytest
from helpers import FakeTransport

from milf_framework import JsonSink, ProxyConfig, RunConfig, run
from milf_framework.models import Outcome
from milf_framework.stats import LOCATION_FIELDS, NETWORK_FIELDS
from milf_framework.targets import EchoIpTarget, TargetRegistry


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
    import milf_framework.runner as runner

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

    import milf_framework.runner as runner

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


OLD_SUMMARY_KEYS = {
    "run_id", "schema_version", "framework_version", "target", "config", "started_at",
    "finished_at", "duration_seconds", "status", "requested", "started", "completed",
    "cancelled", "not_started", "successes", "total_errors", "errors", "status_codes",
    "success_rate", "requests_per_second", "unique_ips", "pool_eligible", "latency_ms",
    "latency_histogram", "response_body_bytes", "buckets", "artifacts",
}
OLD_BUCKET_KEYS = {
    "bucket_start", "completed", "successes", "errors", "status_codes",
    "response_body_bytes", "latency_histogram",
}
NEW_COUNTERS = {"challenges", "blocks", "timeouts", "proxy_errors", "bytes_up", "bytes_down"}


def random_pool(size):
    return lambda n, rng: f"10.0.{size}.{rng.randrange(size)}"


def early_duplicates(n, rng):
    """200 repeats of one exit, then 1,400 new exits, then 10 old ones again."""
    if n <= 200:
        return "10.9.9.9"
    if n <= 1600:
        return f"10.1.{n // 256}.{n % 256}"
    return f"10.1.1.{rng.randrange(10)}"


class PoolTransport(FakeTransport):
    """Exits chosen per success index, completing in a random order."""

    def __init__(self, config, *, address=None, seed=7, fail_every=0):
        super().__init__(config)
        import random

        self.random = random.Random(seed)
        self.address = address or random_pool(20)
        self.fail_every = fail_every
        self.in_flight = 0
        # (successes, unique, in flight, completed) right after each completion,
        # in the order the runner sees them: nothing awaits between return and count.
        self.trace = []
        self.seen = set()
        self.successes = 0

    async def execute(self, target, *, context=None):
        self.in_flight += 1
        await asyncio.sleep(self.random.random() / 2000)
        self.in_flight -= 1
        self.count += 1
        if self.fail_every and self.count % self.fail_every == 0:
            outcome = Outcome(3, error="timeout")
        else:
            self.successes += 1
            ip = self.address(self.successes, self.random)
            self.seen.add(ip)
            outcome = Outcome(2.5, ip=ip, status=200, bytes_up=100, bytes_down=200)
        self.trace.append((self.successes, len(self.seen), self.in_flight, self.count))
        return outcome


@pytest.mark.parametrize("scenario,address,fail_every", [
    ("low_diversity", random_pool(20), 0),
    ("low_diversity", random_pool(20), 7),
    ("low_diversity", random_pool(20), 2),
    ("high_diversity", random_pool(1500), 0),
    ("early_duplicates", early_duplicates, 3),
])
async def test_unique_ratio_after_the_warm_up_sample(tmp_path, scenario, address, fail_every):
    transports = []

    def factory(cfg):
        transports.append(PoolTransport(cfg, address=address, fail_every=fail_every))
        return transports[0]

    result = await run(config(requests=None, stop_rule="unique_ratio", concurrency=50),
                       JsonSink(tmp_path), transport_factory=factory)
    trace = transports[0].trace
    first = next(index for index, (ok, unique, _, _) in enumerate(trace)
                 if ok >= 1000 and ok > 2 * unique)
    ok, unique, in_flight, done = trace[first]
    # Before the warm-up sample nothing stops the run, whatever the ratio.
    assert all(s < 1000 or s <= 2 * u for s, u, _, _ in trace[:first])
    if scenario == "low_diversity":
        # Under 50% unique when the sample completes: stop at once.
        assert ok == 1000
    else:
        # Diverse enough at 1,000: continue until successes > 2 x unique.
        assert ok > 1000 and ok == 2 * unique + 1
    if scenario == "early_duplicates":
        assert any(s > 2 * u for s, u, _, _ in trace[:first] if s < 1000)
    if fail_every:
        # Failures count as requests but never toward the rule.
        assert done > ok
    # Nothing starts after the rule fires; what was in flight finishes and counts.
    assert result.started == done + in_flight
    assert result.completed == result.started == len(trace)
    assert result.completed - done <= 49
    assert result.stop_reason == "unique_ratio" and result.status == "completed"
    assert result.requested is None and result.not_started == result.cancelled == 0
    assert result.unique_ips == len(transports[0].seen)
    assert result.timeouts == result.errors.get("timeout", 0)
    assert result.bytes_up == 100 * result.successes
    assert result.config["stop_rule"] == "unique_ratio" and result.config["requests"] is None
    assert result.config["min_sample"] == 1000


async def test_min_sample_is_configurable(tmp_path):
    transports = []

    def factory(cfg):
        transports.append(PoolTransport(cfg, address=random_pool(3)))
        return transports[0]

    result = await run(config(requests=None, stop_rule="unique_ratio", concurrency=1,
                              min_sample=10), JsonSink(tmp_path), transport_factory=factory)
    assert result.completed == 10 and result.stop_reason == "unique_ratio"


async def test_census_without_any_success_stops_after_5000_requests(tmp_path):
    class Failing(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            return Outcome(1, error="proxy_connect")

    result = await run(config(requests=None, stop_rule="unique_ratio", concurrency=10),
                       JsonSink(tmp_path), transport_factory=Failing)
    assert result.stop_reason == "no_success" and result.status == "completed"
    assert 5000 <= result.completed < 5010 and result.completed == result.started
    assert result.successes == 0 and result.proxy_errors == result.completed


async def test_census_without_any_success_stops_after_ten_minutes(tmp_path, monkeypatch):
    import milf_framework.runner as runner

    clock = [0.0]
    monkeypatch.setattr(runner.time, "perf_counter", lambda: clock[0])

    class Hanging(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            clock[0] += 15  # each request times out after 15 s
            return Outcome(15000, error="timeout")

    result = await run(config(requests=None, stop_rule="unique_ratio", concurrency=1),
                       JsonSink(tmp_path), transport_factory=Hanging)
    assert result.stop_reason == "no_success" and result.completed == 40


async def test_one_success_disarms_the_no_success_guard(tmp_path, monkeypatch):
    import milf_framework.runner as runner

    monkeypatch.setattr(runner, "NO_SUCCESS_REQUESTS", 5)

    class LateStart(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            if self.count < 4 or self.count > 40:
                return Outcome(1, error="timeout")
            return Outcome(1, ip=f"10.0.{self.count}.1", status=200)

    result = await run(config(requests=60, stop_rule="unique_ratio", concurrency=1),
                       JsonSink(tmp_path), transport_factory=LateStart)
    # 37 distinct successes, then failures only: neither the ratio nor the
    # guard fires, so the request cap ends the run.
    assert result.stop_reason == "requests" and result.completed == 60
    assert result.successes == 37


def clocked(monkeypatch, successes, *, step=15):
    """A transport whose first `successes` requests succeed (distinct exits) and the rest time
    out; each request advances the runner's monotonic clock by `step` seconds."""
    import milf_framework.runner as runner

    clock = [0.0]
    monkeypatch.setattr(runner.time, "perf_counter", lambda: clock[0])

    class Dropping(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            clock[0] += step
            if self.count <= successes:
                return Outcome(1, ip=f"10.0.{self.count}.1", status=200)
            return Outcome(15000, error="timeout")

    return Dropping


async def test_census_that_goes_down_stops_as_stalled(tmp_path, monkeypatch):
    sink = JsonSink(tmp_path)
    result = await run(config(requests=None, stop_rule="unique_ratio", concurrency=1), sink,
                       transport_factory=clocked(monkeypatch, 10))
    # Ten successes (the last at 150 s), then failures until 600 s passed without one.
    assert result.stop_reason == "stalled" and result.status == "completed"
    assert result.completed == 10 + 40 and result.successes == 10
    started = datetime.fromisoformat(result.started_at)
    outage = datetime.fromisoformat(result.outage_started_at)
    assert (outage - started).total_seconds() == pytest.approx(150, abs=1e-3)
    saved = json.loads((sink.directory / "summary.json").read_text())
    assert saved["outage_started_at"] == result.outage_started_at
    assert saved["config"]["stall_seconds"] == 600
    # The failures during the stall stay in the buckets, so that range's success rate shows
    # the outage: every bucket after the last success has requests and no success.
    buckets = saved["buckets"]
    assert sum(b["completed"] - b["successes"] for b in buckets) == 40
    after = [b for b in buckets if datetime.fromisoformat(b["bucket_start"]) > outage]
    assert after and all(b["successes"] == 0 and b["completed"] > 0 for b in after)
    assert sum(b["successes"] for b in buckets) == 10


async def test_stall_seconds_is_configurable(tmp_path, monkeypatch):
    result = await run(config(requests=None, stop_rule="unique_ratio", concurrency=1,
                              stall_seconds=60), JsonSink(tmp_path),
                       transport_factory=clocked(monkeypatch, 10))
    assert result.stop_reason == "stalled" and result.completed == 10 + 4


async def test_no_stall_before_the_first_success(tmp_path, monkeypatch):
    # Never a success: the no_success guard (10 minutes) ends it, not the stall rule.
    result = await run(config(requests=None, stop_rule="unique_ratio", concurrency=1,
                              stall_seconds=30), JsonSink(tmp_path),
                       transport_factory=clocked(monkeypatch, 0))
    assert result.stop_reason == "no_success" and result.completed == 40
    assert result.outage_started_at is None


async def test_count_runs_never_stall(tmp_path, monkeypatch):
    sink = JsonSink(tmp_path)
    result = await run(config(requests=60, concurrency=1), sink,
                       transport_factory=clocked(monkeypatch, 1))
    assert result.stop_reason == "requests" and result.completed == 60
    saved = json.loads((sink.directory / "summary.json").read_text())
    assert "outage_started_at" not in saved and "stall_seconds" not in saved["config"]


def test_stall_seconds_validation():
    for value in (0, -1, float("nan"), float("inf"), "600", None):
        with pytest.raises(ValueError, match="stall_seconds"):
            config(requests=None, stop_rule="unique_ratio", stall_seconds=value)
    assert config(requests=None, stop_rule="unique_ratio", stall_seconds=0.5).stall_seconds == 0.5


async def test_unique_ratio_still_honours_a_request_cap(tmp_path):
    result = await run(config(requests=30, stop_rule="unique_ratio", concurrency=5),
                       JsonSink(tmp_path),
                       transport_factory=lambda cfg: PoolTransport(
                           cfg, address=random_pool(10_000)))
    assert result.completed == 30 and result.stop_reason == "requests"
    assert result.requested == 30 and result.not_started == 0


async def test_count_rule_stops_at_n(tmp_path):
    transports = []

    def factory(cfg):
        transports.append(PoolTransport(cfg, address=random_pool(2)))
        return transports[0]

    result = await run(config(requests=100, concurrency=25), JsonSink(tmp_path),
                       transport_factory=factory)
    assert result.completed == result.started == transports[0].count == 100
    assert result.stop_reason == "requests" and result.status == "completed"
    # The default rule is not written, so count runs keep their old configuration.
    assert "stop_rule" not in result.config and "min_sample" not in result.config


def test_stop_rule_validation():
    with pytest.raises(ValueError):
        config(requests=None)
    with pytest.raises(ValueError):
        config(stop_rule="forever")
    with pytest.raises(ValueError):
        config(requests=0, stop_rule="unique_ratio")
    with pytest.raises(ValueError):
        config(requests=None, stop_rule="unique_ratio", min_sample=0)
    assert config(requests=None, stop_rule="unique_ratio").requests is None


async def test_unique_ratio_requires_a_pool_target(tmp_path):
    targets = TargetRegistry()
    targets.register(EchoIpTarget("no_ips", "http://example.test/", pool_eligible=False))
    cfg = RunConfig("no_ips", ProxyConfig("http", "localhost", 8080), requests=None,
                    stop_rule="unique_ratio")
    with pytest.raises(ValueError, match="pool-eligible"):
        await run(cfg, JsonSink(tmp_path), targets=targets, transport_factory=FakeTransport)


async def test_operator_stop_reason(tmp_path):
    stop = asyncio.Event()
    stop.set()
    result = await run(config(requests=None, stop_rule="unique_ratio"), JsonSink(tmp_path),
                       stop=stop, transport_factory=FakeTransport)
    assert result.stop_reason == "operator" and result.status == "cancelled"
    assert result.requested is None and result.not_started == 0


async def test_failed_run_stop_reason(tmp_path):
    class Broken(FakeTransport):
        async def execute(self, target, *, context=None):
            raise RuntimeError("boom")

    sink = JsonSink(tmp_path)
    with pytest.raises(RuntimeError):
        await run(config(requests=3), sink, transport_factory=Broken)
    assert json.loads((sink.directory / "summary.json").read_text())["stop_reason"] == "error"


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
    result = await run(config(requests=12, concurrency=1), sink, transport_factory=Mixed)
    saved = json.loads((sink.directory / "summary.json").read_text())
    assert OLD_SUMMARY_KEYS | NEW_COUNTERS | {"stop_reason"} == set(saved)
    assert set(saved["buckets"][0]) == OLD_BUCKET_KEYS | NEW_COUNTERS
    assert {k: saved[k] for k in NEW_COUNTERS} == {
        "challenges": 2, "blocks": 2, "timeouts": 2, "proxy_errors": 2,
        "bytes_up": 40, "bytes_down": 1840,
    }
    assert result.successes == 2 and result.total_errors == 10
    assert result.completed == result.successes + result.total_errors
    assert saved["stop_reason"] == "requests" and set(saved["artifacts"]) == {"ips"}


async def test_progress_is_throttled_and_final(tmp_path):
    calls = []

    class Slow(FakeTransport):
        async def execute(self, target, *, context=None):
            await asyncio.sleep(.004)
            return await super().execute(target, context=context)

    async def progress(counters):
        calls.append((asyncio.get_running_loop().time(), counters))

    result = await run(config(requests=60, concurrency=2), JsonSink(tmp_path),
                       transport_factory=Slow, progress=progress, progress_interval=.03)
    assert len(calls) >= 3
    assert set(calls[0][1]) == {"requests", "successes", "unique_ips", *NEW_COUNTERS}
    times = [at for at, _ in calls[:-1]]
    assert all(b - a >= .025 for a, b in zip(times, times[1:], strict=False))
    requests = [c["requests"] for _, c in calls]
    assert requests == sorted(requests)
    assert calls[-1][1] == {"requests": 60, "successes": 60, "unique_ips": 1,
                            "challenges": 0, "blocks": 0, "timeouts": 0, "proxy_errors": 0,
                            "bytes_up": 0, "bytes_down": 0}
    assert result.completed == 60


async def test_progress_failure_does_not_end_the_run(tmp_path):
    def progress(counters):
        raise ConnectionError("orchestrator unreachable")

    result = await run(config(requests=5), JsonSink(tmp_path), transport_factory=FakeTransport,
                       progress=progress, progress_interval=.001)
    assert result.completed == 5 and result.status == "completed"
    with pytest.raises(ValueError):
        await run(config(requests=5), JsonSink(tmp_path), transport_factory=FakeTransport,
                  progress=progress, progress_interval=0)


class Enricher:
    release = {"file": "fixture.mmdb", "sha256": "ab" * 32, "database_type": "DBIP-Full",
               "build_epoch": 1000, "built_at": "1970-01-01T00:16:40+00:00"}

    def __init__(self):
        self.lookups = 0

    def lookup(self, address):
        self.lookups += 1
        last = int(address.rsplit(".", 1)[1])
        if last == 99:
            return {"matched": False, **dict.fromkeys(
                (*LOCATION_FIELDS, *NETWORK_FIELDS))}
        german = last % 2 == 0
        return {
            "matched": True, "country_code": "DE" if german else "FR",
            "country": "Germany" if german else "France", "region_code": None,
            "region": None, "city": "Berlin" if german else "Paris",
            "latitude": 52.5 if german else 48.9, "longitude": 13.4 if german else 2.35,
            "isp": "Telekom" if german else "Orange", "organization": None,
            "asn": 3320 if german else 3215, "asn_organization": "DTAG" if german else "Orange",
            "connection_type": "Cable/DSL", "usage_type": "residential",
        }


async def test_geo_rows_per_bucket_location_and_network(tmp_path, monkeypatch):
    import milf_framework.runner as runner

    clock = [0.0]
    monkeypatch.setattr(runner.time, "time", lambda: 290.0)
    monkeypatch.setattr(runner.time, "perf_counter", lambda: clock[0])

    class Script(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            # Requests 1-6 land in the 0:00 bucket, 7-12 in 5:00.
            clock[0] = 0 if self.count <= 6 else 20
            ip = ["10.0.0.2", "10.0.0.4", "10.0.0.3", "10.0.0.99", None, None][
                (self.count - 1) % 6]
            if ip is None:
                return Outcome(9, error="timeout" if self.count % 2 else "challenge",
                               bytes_up=5)
            return Outcome(self.count, ip=ip, status=200, bytes_up=10, bytes_down=30)

    enricher = Enricher()
    sink = JsonSink(tmp_path)
    result = await run(config(requests=12, concurrency=1), sink, transport_factory=Script,
                       enricher=enricher)
    artifact = result.artifacts["geo"]
    path = sink.directory / artifact["file"]
    assert artifact["file"] == "geo.jsonl.gz" and artifact["release"] == Enricher.release
    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    with gzip.open(path, "rt") as stream:
        rows = [json.loads(line) for line in stream]
    # (DE, FR, unknown) x 2 buckets: an unmatched IP shares the no-IP "unknown" row.
    assert artifact["rows"] == len(rows) == 6
    assert enricher.lookups == 8
    assert sum(r["completed"] for r in rows) == result.completed == 12
    assert sum(r["successes"] for r in rows) == result.successes == 8
    assert sum(r["bytes_up"] for r in rows) == result.bytes_up
    assert sum(r["challenges"] for r in rows) == result.challenges
    assert sum(r["timeouts"] for r in rows) == result.timeouts
    assert {r["bucket_start"] for r in rows} == {b["bucket_start"] for b in result.buckets}
    german = [r for r in rows if r["location"] and r["location"]["country_code"] == "DE"]
    assert len(german) == 2 and all(r["completed"] == 2 for r in german)
    assert german[0]["location"] == {
        "country_code": "DE", "country": "Germany", "region_code": None, "region": None,
        "city": "Berlin", "latitude": 52.5, "longitude": 13.4,
    }
    assert german[0]["network"] == {
        "isp": "Telekom", "organization": None, "asn": 3320, "asn_organization": "DTAG",
        "connection_type": "Cable/DSL", "usage_type": "residential",
    }
    assert german[0]["latency_histogram"] == {"format": "ceil_ms_v1", "bins": [[1, 1], [2, 1]]}
    unknown = [r for r in rows if r["location"] is None]
    assert all(r["network"] is None for r in unknown) and len(unknown) == 2
    assert all((r["completed"], r["successes"]) == (3, 1) for r in unknown)
    assert sum(r["challenges"] + r["timeouts"] for r in unknown) == 4
    assert [len(r["latency_histogram"]["bins"]) for r in unknown] == [1, 1]
    assert set(rows[0]) == {
        "bucket_start", "location", "network", "completed", "successes", "challenges",
        "blocks", "timeouts", "bytes_up", "bytes_down", "latency_histogram",
    }


async def test_geo_artifact_exists_without_traffic(tmp_path):
    stop = asyncio.Event()
    stop.set()
    sink = JsonSink(tmp_path)
    result = await run(config(), sink, stop=stop, transport_factory=FakeTransport,
                       enricher=Enricher())
    assert result.artifacts["geo"]["rows"] == 0
    assert (sink.directory / "geo.jsonl.gz").exists()
    assert not list(sink.directory.glob("*.partial"))


async def test_geo_with_the_local_dbip_database(tmp_path):
    from milf_framework.enrichment import LocalEnricher, find_database

    try:
        find_database()
    except (ValueError, FileNotFoundError):
        pytest.skip("No local DB-IP database")
    with LocalEnricher() as enricher:
        result = await run(config(requests=3), JsonSink(tmp_path),
                           transport_factory=FakeTransport, enricher=enricher)
    assert result.artifacts["geo"]["rows"] == 1
    assert result.artifacts["geo"]["release"]["sha256"] == enricher.release["sha256"]
