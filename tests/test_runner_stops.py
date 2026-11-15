"""The runner's stop rules: unique ratio, no success, stall, request count, operator and error.

The shared run configuration is in `runner_cases.py`.
"""

import asyncio
import json
from datetime import datetime

import pytest
from helpers import FakeTransport
from runner_cases import config

from atlas import JsonSink, ProxyConfig, RunConfig, RunOptions, run
from atlas.models import Outcome
from atlas.targets import EchoIpTarget, TargetRegistry


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


@pytest.mark.parametrize(
    "scenario,address,fail_every",
    [
        ("low_diversity", random_pool(20), 0),
        ("low_diversity", random_pool(20), 7),
        ("low_diversity", random_pool(20), 2),
        ("high_diversity", random_pool(1500), 0),
        ("early_duplicates", early_duplicates, 3),
    ],
)
async def test_unique_ratio_after_the_warm_up_sample(tmp_path, scenario, address, fail_every):
    transports = []

    def factory(cfg):
        transports.append(PoolTransport(cfg, address=address, fail_every=fail_every))
        return transports[0]

    result = await run(
        config(requests=None, stop_rule="unique_ratio", concurrency=50),
        JsonSink(tmp_path),
        RunOptions(transport_factory=factory),
    )
    trace = transports[0].trace
    first = next(
        index
        for index, (ok, unique, _, done) in enumerate(trace)
        if done >= 1000 and done > 2 * unique
    )
    ok, unique, in_flight, done = trace[first]
    # Before the warm-up sample nothing stops the run, whatever the ratio.
    assert all(n < 1000 or n <= 2 * u for s, u, _, n in trace[:first])
    if scenario == "low_diversity":
        # Under 50% unique when the sample completes: stop at once.
        assert done == 1000
    else:
        # Diverse enough at 1,000: continue until successes > 2 x unique.
        assert done >= 1000 and done > 2 * unique
    if scenario == "early_duplicates":
        assert any(s > 2 * u for s, u, _, _ in trace[:first] if s < 1000)
    if fail_every:
        # Failures count toward both the warm-up and the ratio denominator.
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

    result = await run(
        config(requests=None, stop_rule="unique_ratio", concurrency=1, min_sample=10),
        JsonSink(tmp_path),
        RunOptions(transport_factory=factory),
    )
    assert result.completed == 10 and result.stop_reason == "unique_ratio"


async def test_census_without_any_success_stops_after_300_completions(tmp_path):
    # LOWAVAIL-1: no unique IP after ZERO_IPS_STOP answers ends the run (it was the minimum
    # sample, 1000, before); nothing succeeded, so it is still 'no_success'.
    class Failing(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            return Outcome(1, error="proxy_connect")

    result = await run(
        config(requests=None, stop_rule="unique_ratio", concurrency=10),
        JsonSink(tmp_path),
        RunOptions(transport_factory=Failing),
    )
    assert result.stop_reason == "no_success" and result.status == "completed"
    assert 300 <= result.completed < 310 and result.completed == result.started
    assert result.successes == 0 and result.proxy_errors == result.completed


async def test_census_finding_no_ip_stops_after_300_answers(tmp_path):
    # Answers come back, but none brings an exit IP to count: a country with no exits.
    class NoExit(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            return Outcome(1, status=200) if self.count % 2 else Outcome(1, error="timeout")

    result = await run(
        config(requests=None, stop_rule="unique_ratio", concurrency=10),
        JsonSink(tmp_path),
        RunOptions(transport_factory=NoExit),
    )
    assert result.stop_reason == "no_ips" and 300 <= result.completed < 310
    assert result.unique_ips == 0 and result.successes > 0


async def test_one_exit_ip_keeps_a_census_running_past_300(tmp_path):
    class OneExit(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            return Outcome(1, ip="10.0.0.1", status=200)

    result = await run(
        config(requests=None, stop_rule="unique_ratio", concurrency=10, min_sample=500),
        JsonSink(tmp_path),
        RunOptions(transport_factory=OneExit),
    )
    assert result.stop_reason == "unique_ratio" and result.completed >= 500


async def test_census_without_any_success_stops_after_ten_minutes(tmp_path, monkeypatch):
    import atlas.runner as runner

    clock = [0.0]
    monkeypatch.setattr(runner.time, "perf_counter", lambda: clock[0])

    class Hanging(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            clock[0] += 15  # each request times out after 15 s
            return Outcome(15000, error="timeout")

    sink = JsonSink(tmp_path)
    result = await run(
        config(requests=None, stop_rule="unique_ratio", concurrency=1),
        sink,
        RunOptions(transport_factory=Hanging),
    )
    assert result.stop_reason == "no_success" and result.completed == 40
    # No success, no latency sample: the mean is unknown, never 0.
    assert result.latency_mean_ms is None
    saved = json.loads((sink.directory / "summary.json").read_text())
    assert "latency_mean_ms" in saved and saved["latency_mean_ms"] is None


async def test_one_success_disarms_the_no_success_guard(tmp_path, monkeypatch):
    import atlas.runner as runner

    monkeypatch.setattr(runner, "NO_SUCCESS_REQUESTS", 5)

    class LateStart(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            if self.count < 4 or self.count > 40:
                return Outcome(1, error="timeout")
            return Outcome(1, ip=f"10.0.{self.count}.1", status=200)

    result = await run(
        config(requests=60, stop_rule="unique_ratio", concurrency=1),
        JsonSink(tmp_path),
        RunOptions(transport_factory=LateStart),
    )
    # 37 distinct successes, then failures only: neither the ratio nor the
    # guard fires, so the request cap ends the run.
    assert result.stop_reason == "requests" and result.completed == 60
    assert result.successes == 37


def clocked(monkeypatch, successes, *, step=15):
    """A transport whose first `successes` requests succeed (distinct exits) and the rest time
    out; each request advances the runner's monotonic clock by `step` seconds."""
    import atlas.runner as runner

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
    result = await run(
        config(requests=None, stop_rule="unique_ratio", concurrency=1),
        sink,
        RunOptions(transport_factory=clocked(monkeypatch, 10)),
    )
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
    result = await run(
        config(requests=None, stop_rule="unique_ratio", concurrency=1, stall_seconds=60),
        JsonSink(tmp_path),
        RunOptions(transport_factory=clocked(monkeypatch, 10)),
    )
    assert result.stop_reason == "stalled" and result.completed == 10 + 4


async def test_no_stall_before_the_first_success(tmp_path, monkeypatch):
    # Never a success: the no_success guard (10 minutes) ends it, not the stall rule.
    result = await run(
        config(requests=None, stop_rule="unique_ratio", concurrency=1, stall_seconds=30),
        JsonSink(tmp_path),
        RunOptions(transport_factory=clocked(monkeypatch, 0)),
    )
    assert result.stop_reason == "no_success" and result.completed == 40
    assert result.outage_started_at is None


async def test_count_runs_never_stall(tmp_path, monkeypatch):
    sink = JsonSink(tmp_path)
    result = await run(
        config(requests=60, concurrency=1),
        sink,
        RunOptions(transport_factory=clocked(monkeypatch, 1)),
    )
    assert result.stop_reason == "requests" and result.completed == 60
    saved = json.loads((sink.directory / "summary.json").read_text())
    assert "outage_started_at" not in saved and "stall_seconds" not in saved["config"]


def test_stall_seconds_validation():
    for value in (0, -1, float("nan"), float("inf"), "600", None):
        with pytest.raises(ValueError, match="stall_seconds"):
            config(requests=None, stop_rule="unique_ratio", stall_seconds=value)
    assert config(requests=None, stop_rule="unique_ratio", stall_seconds=0.5).stall_seconds == 0.5


async def test_unique_ratio_still_honours_a_request_cap(tmp_path):
    result = await run(
        config(requests=30, stop_rule="unique_ratio", concurrency=5),
        JsonSink(tmp_path),
        RunOptions(transport_factory=lambda cfg: PoolTransport(cfg, address=random_pool(10_000))),
    )
    assert result.completed == 30 and result.stop_reason == "requests"
    assert result.requested == 30 and result.not_started == 0


async def test_count_rule_stops_at_n(tmp_path):
    transports = []

    def factory(cfg):
        transports.append(PoolTransport(cfg, address=random_pool(2)))
        return transports[0]

    result = await run(
        config(requests=100, concurrency=25),
        JsonSink(tmp_path),
        RunOptions(transport_factory=factory),
    )
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
    cfg = RunConfig(
        "no_ips", ProxyConfig("http", "localhost", 8080), requests=None, stop_rule="unique_ratio"
    )
    with pytest.raises(ValueError, match="pool-eligible"):
        await run(
            cfg, JsonSink(tmp_path), RunOptions(targets=targets, transport_factory=FakeTransport)
        )


async def test_operator_stop_reason(tmp_path):
    stop = asyncio.Event()
    stop.set()
    result = await run(
        config(requests=None, stop_rule="unique_ratio"),
        JsonSink(tmp_path),
        RunOptions(stop=stop, transport_factory=FakeTransport),
    )
    assert result.stop_reason == "operator" and result.status == "cancelled"
    assert result.requested is None and result.not_started == 0


async def test_failed_run_stop_reason(tmp_path):
    class Broken(FakeTransport):
        async def execute(self, target, *, context=None):
            raise RuntimeError("boom")

    sink = JsonSink(tmp_path)
    with pytest.raises(RuntimeError):
        await run(config(requests=3), sink, RunOptions(transport_factory=Broken))
    assert json.loads((sink.directory / "summary.json").read_text())["stop_reason"] == "error"


async def test_a_census_that_still_finds_new_ips_stops_at_its_cap(tmp_path, monkeypatch):
    # DAILY-OVERTIME-1 (#1152): a country test runs until it is finished, but never longer
    # than its own cap; its data is kept as a completed run.
    sink = JsonSink(tmp_path)
    capped = config(requests=None, stop_rule="unique_ratio", concurrency=1, cap_seconds=100)
    transport = clocked(monkeypatch, 99, step=10)
    result = await run(capped, sink, RunOptions(transport_factory=transport))
    assert result.stop_reason == "cap" and result.status == "completed"
    assert result.completed == result.successes == result.unique_ips == 10  # 10 s each
    assert json.loads((sink.directory / "summary.json").read_text())["config"]["cap_seconds"] == 100
    assert "cap_seconds" not in config(requests=None, stop_rule="unique_ratio").public()
    with pytest.raises(ValueError, match="cap_seconds requires"):
        config(requests=10, cap_seconds=5)
    with pytest.raises(ValueError, match="cap_seconds must be finite"):
        config(requests=None, stop_rule="unique_ratio", cap_seconds=0)


async def test_a_run_with_its_own_pace_keeps_its_requests_in_flight_under_it(tmp_path):
    peak = {"now": 0, "most": 0}

    class Counted(FakeTransport):
        async def execute(self, target, *, context=None):
            peak["now"] += 1
            peak["most"] = max(peak["most"], peak["now"])
            await asyncio.sleep(0.001)
            peak["now"] -= 1
            self.count += 1
            return Outcome(1, ip=f"10.0.{self.count // 256}.{self.count % 256}", status=200)

    result = await run(
        config(requests=60, concurrency=50, start_concurrency=3),
        JsonSink(tmp_path),
        RunOptions(transport_factory=Counted),
    )
    assert result.completed == 60 and peak["most"] == 3
    unpaced = await run(
        config(requests=60, concurrency=50),
        JsonSink(tmp_path / "plain"),
        RunOptions(transport_factory=Counted),
    )
    assert unpaced.completed == 60 and peak["most"] == 50


@pytest.mark.parametrize("start", [0, 51, 2.0, True])
def test_a_pace_must_be_a_positive_integer_up_to_the_concurrency(start):
    with pytest.raises(ValueError, match="start_concurrency"):
        config(concurrency=50, start_concurrency=start)


def test_the_pace_stays_out_of_the_scope():
    from atlas.storage_read import _scope

    labels = {"provider": "shifter", "product": "residential", "concurrency": 50}
    hashes = {
        _scope({"target": "generic_http", "config": config(**labels, **pace).public()})[3]
        for pace in ({}, {"start_concurrency": 5}, {"start_concurrency": 20})
    }
    assert len(hashes) == 1
