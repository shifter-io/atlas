"""Onboarding runner and stream simulations: no provider traffic or database."""

import asyncio
import contextlib
import gzip
import json
import time

import coverage
import pytest
from helpers import FakeTransport
from test_onboarding import START

from atlas import ProxyConfig, RunConfig, RunOptions, run
from atlas.models import Outcome
from atlas.onboarding import manifest as campaign
from atlas.onboarding.writer import ObservationWriter, SummarySink, WriterBounds


async def test_real_runner_streams_each_success_and_uses_new_rule(tmp_path):
    class Mixed(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            if self.count % 2:
                return Outcome(1, error="timeout")
            return Outcome(1, ip="2001:db8::1", status=200)

    writer = ObservationWriter(
        tmp_path,
        country="US",
        scheduled=campaign.timestamp(time.time()),
        started=campaign.timestamp(time.time()),
        deadline=time.time() + 60,
    )
    summary = await run(
        RunConfig(
            "generic_http",
            ProxyConfig("http", "localhost", 8080),
            requests=None,
            concurrency=1,
            stop_rule="unique_ratio",
        ),
        SummarySink(),
        RunOptions(
            observation_consumer=writer.observe, aggregate_ips=False, transport_factory=Mixed
        ),
    )
    await writer.close()
    assert summary.completed == 1000 and summary.successes == writer.rows == 500
    assert summary.stop_reason == "unique_ratio"
    assert summary.config["census_rule_version"] == 2
    assert summary.config["census_ratio_basis"] == "completed_requests"
    assert not (tmp_path / "ips.jsonl.gz").exists()


async def test_deadline_cancels_slow_requests_without_observations():
    observed = []

    class Slow(FakeTransport):
        async def execute(self, target, *, context=None):
            await asyncio.sleep(10)
            return Outcome(1, ip="203.0.113.1", status=200)

    async def observe(ip, at):
        observed.append(ip)

    summary = await asyncio.wait_for(
        run(
            RunConfig("generic_http", ProxyConfig("http", "localhost", 8080), requests=2),
            SummarySink(),
            RunOptions(
                transport_factory=Slow, observation_consumer=observe, deadline=time.time() + 0.06
            ),
        ),
        1,
    )
    assert summary.status == "cancelled" and summary.stop_reason == "deadline"
    assert summary.completed == 0 and not observed
    assert summary.cancelled == 2


async def test_observation_failure_stops_shared_runner():
    async def broken(ip, at):
        raise OSError("disk full")

    with pytest.raises(RuntimeError, match="Run failed"):
        await run(
            RunConfig("generic_http", ProxyConfig("http", "localhost", 8080)),
            SummarySink(),
            RunOptions(transport_factory=FakeTransport, observation_consumer=broken),
        )


@contextlib.contextmanager
def coverage_paused():
    """Stop every running coverage measurement for the block, then restart it in order.

    pytest-cov's `no_cover` stops only its own instance, and an xdist worker runs two.
    """
    stopped = []
    while (current := coverage.Coverage.current()) is not None:
        current.stop()
        stopped.append(current)
    try:
        yield
    finally:
        for measurement in reversed(stopped):
            measurement.start()


# Two million rows in four writers of half a million each, one per param, so each takes a
# quarter of the time. Each writer still fills its queue to the bound (the producer never yields
# until the queue is full, and the writer thread is far slower than it) and commits at least
# five 100,000-row chunks.
STREAM_PARTS = 4
STREAM_ROWS = 2_000_000 // STREAM_PARTS


@pytest.mark.scale
@pytest.mark.parametrize("part", range(STREAM_PARTS))
async def test_two_million_observations_stream_with_bounded_queue(tmp_path, part):
    # Tracing two million writer rows doubled this test (47 s alone, 73 s under tier1's
    # load). The same stream and bound are asserted; the next test covers backpressure.
    with coverage_paused():
        writer = ObservationWriter(
            tmp_path,
            country="US",
            scheduled=campaign.timestamp(START),
            started=campaign.timestamp(START),
            deadline=START.timestamp() + 60,
            bounds=WriterBounds(queue_size=4096),
        )
        observed = START.timestamp() + 1
        for _ in range(STREAM_ROWS):
            await writer.observe("2001:db8::1", observed)
        await writer.close()
    assert writer.rows == STREAM_ROWS and writer.peak_queue == 4096 and writer.chunks >= 5
    count = 0
    for path in sorted(tmp_path.glob("*.gz")):
        lines = gzip.decompress(path.read_bytes()).decode().splitlines()
        count += len(lines)
        # Every line is checked: identical lines need parsing only once.
        for line in set(lines):
            assert json.loads(line)["ip"] == "2001:db8::1"
    assert count == STREAM_ROWS


async def test_full_intake_waits_for_the_writer(tmp_path):
    writer = ObservationWriter(
        tmp_path,
        country="US",
        scheduled=campaign.timestamp(START),
        started=campaign.timestamp(START),
        deadline=START.timestamp() + 60,
        bounds=WriterBounds(queue_size=1),
    )
    for _ in range(100):
        await writer.observe("2001:db8::1", START.timestamp() + 1)
    await writer.close()
    assert writer.rows == 100 and writer.peak_queue == 1


async def test_exactly_half_unique_keeps_running_and_failure_crosses_threshold():
    class Half(FakeTransport):
        async def execute(self, target, *, context=None):
            self.count += 1
            if self.count <= 500:
                return Outcome(1, ip=f"10.0.{self.count // 256}.{self.count % 256}", status=200)
            return Outcome(1, error="timeout")

    result = await run(
        RunConfig(
            "generic_http",
            ProxyConfig("http", "localhost", 8080),
            requests=None,
            concurrency=1,
            stop_rule="unique_ratio",
        ),
        SummarySink(),
        RunOptions(transport_factory=Half),
    )
    assert result.completed == 1001 and result.successes == 500
    assert result.stop_reason == "unique_ratio"


async def test_deadline_rejects_a_late_response_even_without_watchdog_tick(monkeypatch):
    import atlas.runner as runner

    now = [time.time()]
    monkeypatch.setattr(runner.time, "time", lambda: now[0])

    class Late(FakeTransport):
        async def execute(self, target, *, context=None):
            # Advance the wall clock before a watcher can get an event-loop turn.
            now[0] += 1
            return Outcome(1, ip="203.0.113.1", status=200)

    observed = []

    async def observe(ip, at):
        observed.append(ip)

    result = await run(
        RunConfig("generic_http", ProxyConfig("http", "localhost", 8080), requests=1),
        SummarySink(),
        RunOptions(
            transport_factory=Late, observation_consumer=observe, deadline=time.time() + 0.01
        ),
    )
    assert not observed and result.stop_reason == "deadline"
    assert result.completed == 0


async def test_cancellation_during_backpressure_keeps_completed_observation():
    entered, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = []

    async def observe(ip, at):
        entered.set()
        await release.wait()
        observed.append(ip)

    task = asyncio.create_task(
        run(
            RunConfig(
                "generic_http", ProxyConfig("http", "localhost", 8080), requests=10, concurrency=1
            ),
            SummarySink(),
            RunOptions(transport_factory=FakeTransport, observation_consumer=observe, stop=stop),
        )
    )
    await entered.wait()
    stop.set()
    await asyncio.sleep(0.06)
    release.set()
    result = await asyncio.wait_for(task, 1)
    assert len(observed) == result.successes == 1
    assert result.started == 1


async def test_runner_uses_campaign_clock_for_observation_timestamp():
    observed = []
    anchor = START.timestamp()

    async def observe(ip, at):
        observed.append(at)

    result = await run(
        RunConfig("generic_http", ProxyConfig("http", "localhost", 8080), requests=1),
        SummarySink(),
        RunOptions(
            transport_factory=FakeTransport,
            observation_consumer=observe,
            deadline=anchor + 60,
            wall_clock=lambda: anchor,
        ),
    )
    assert result.completed == 1
    assert anchor <= observed[0] < anchor + 1
