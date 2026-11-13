"""Local simulations: no provider traffic, database, orchestrator or website. The scheduler and
the observation writer; test_onboarding_campaign.py holds the manifest, export and CLI."""

import asyncio
import gzip
import json
import stat
import time
from dataclasses import replace

import pytest
from helpers import FakeTransport
from onboarding_cases import ROOT, START, Clock, Proxy, fake_measurement

from atlas import RunConfig, run
from atlas.onboarding import manifest as campaign
from atlas.onboarding.files import atomic_json, lock, read_json
from atlas.onboarding.scheduler import RegionOptions, execute, new_state
from atlas.onboarding.writer import ObservationWriter, WriterBounds

# Other test modules import onboarding_cases' helpers from this module.
pytest_plugins = ["onboarding_cases"]
__all__ = ["ROOT", "START", "Clock", "Proxy", "fake_measurement"]


async def test_fifo_overruns_preserve_all_rounds_and_country_order(manifest):
    clock = Clock(START.timestamp())
    calls = []

    async def measure(config, sink, options):
        calls.append((config.requested_country, clock.now()))
        # Toronto takes 66 minutes per round, ensuring a growing backlog.
        clock.value += 22 * 60
        return fake_measurement()

    result = await execute(
        manifest,
        "tor1",
        Proxy,
        RegionOptions(concurrency=37, request_timeout=3.5),
        clock=clock,
        run_country=measure,
    )
    state = read_json(manifest.parent / "regions/tor1/state.json")
    assert result["status"] == "closed"
    assert [country for country, _ in calls] == (["ca", "gl", "pm"] * 24)[: len(calls)]
    assert calls[3][1] == START.timestamp() + 66 * 60
    assert state["jobs"][3]["scheduled_at"] == "2027-01-01T23:00:00.000Z"
    assert state["jobs"][3]["delay_seconds"] == 360
    assert len(calls) < 72
    assert state["jobs"][-1]["status"] == "not_started_deadline"
    assert all(start < START.timestamp() + 86400 for _, start in calls)
    assert result["settings"]["concurrency"] == 37
    assert result["settings"]["timeout_seconds"] == 3.5


async def test_all_rounds_wait_for_absolute_hours_and_close_at_deadline(manifest):
    clock = Clock(START.timestamp())
    calls = []

    async def measure(config, sink, options):
        calls.append(clock.now())
        clock.value += 10
        return fake_measurement()

    result = await execute(manifest, "tor1", Proxy, clock=clock, run_country=measure)
    assert len(calls) == 72
    assert calls[::3] == [START.timestamp() + i * 3600 for i in range(24)]
    assert result["unfinished_countries"] == 0
    assert clock.now() == START.timestamp() + 86400


async def test_restart_preserves_settings_skips_interrupted_and_completed(manifest):
    data = campaign.load(manifest)
    state = new_state(data, "tor1", data["settings"] | {"concurrency": 7, "timeout_seconds": 4})
    state["jobs"][0].update(status="completed", observations=0)
    state["jobs"][1].update(status="running", started_at=data["starts_at"])
    path = manifest.parent / "regions/tor1/state.json"
    atomic_json(path, state)
    clock = Clock(START.timestamp() + 30)
    calls = []

    async def measure(config, sink, options):
        calls.append((config.requested_country, config.concurrency, config.timeout_seconds))
        clock.value = START.timestamp() + 86400
        return fake_measurement(status="cancelled", stop_reason="deadline")

    await execute(
        manifest, "tor1", Proxy, RegionOptions(resume=True), clock=clock, run_country=measure
    )
    assert calls == [("pm", 7, 4)]
    saved = read_json(path)
    assert saved["jobs"][1]["status"] == "interrupted"
    with pytest.raises(ValueError, match="preserve"):
        await execute(manifest, "tor1", Proxy, RegionOptions(resume=True, concurrency=8))
    # A closed campaign is inert even when explicitly resumed.
    await execute(
        manifest, "tor1", Proxy, RegionOptions(resume=True), clock=clock, run_country=measure
    )
    assert len(calls) == 1


async def test_expired_campaign_never_runs(manifest):
    async def forbidden(*args, **kwargs):
        pytest.fail("Expired campaign ran traffic")

    result = await execute(
        manifest, "tor1", Proxy, run_country=forbidden, clock=Clock(START.timestamp() + 86400)
    )
    assert result["unfinished_countries"] == 72
    assert result["status"] == "closed"


def test_exclusive_lock(tmp_path):
    with lock(tmp_path / ".lock"), pytest.raises(ValueError, match="already running"):
        with lock(tmp_path / ".lock"):
            pass


async def test_writer_duplicates_ipv6_rotation_and_deadline(tmp_path):
    start = START.timestamp()
    writer = ObservationWriter(
        tmp_path,
        country="US",
        scheduled=campaign.timestamp(start),
        started=campaign.timestamp(start + 2),
        deadline=start + 60,
        bounds=WriterBounds(chunk_rows=2, queue_size=2),
    )
    for address in ("203.0.113.1", "203.0.113.1", "2001:db8::1"):
        await writer.observe(address, start + 3)
    await writer.observe("203.0.113.2", start + 60)
    await writer.close()
    chunks = sorted(tmp_path.glob("*.gz"))
    rows = [json.loads(line) for chunk in chunks for line in gzip.open(chunk, "rt")]
    assert len(chunks) == 2 and writer.rows == 3
    assert [r["ip"] for r in rows] == ["203.0.113.1", "203.0.113.1", "2001:db8::1"]
    assert all(r["country_code"] == "us" for r in rows)
    assert all(r["country_started_at"] == campaign.timestamp(start + 2) for r in rows)
    assert writer.peak_queue <= 2
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in tmp_path.iterdir())


async def test_writer_rotates_idle_pending_records(tmp_path):
    writer = ObservationWriter(
        tmp_path,
        country="US",
        scheduled=campaign.timestamp(START),
        started=campaign.timestamp(START),
        deadline=START.timestamp() + 60,
        bounds=WriterBounds(chunk_seconds=0.02),
    )
    await writer.observe("203.0.113.1", START.timestamp() + 1)
    for _ in range(100):
        if writer.chunks:
            break
        await asyncio.sleep(0.01)
    assert writer.chunks == 1
    await writer.close()


async def test_writer_close_does_not_wait_for_the_idle_poll(tmp_path):
    # Every scheduled country opens and closes one writer; an idle writer must stop
    # as soon as it is closed, not after its queue poll times out.
    began = time.perf_counter()
    for index in range(100):
        writer = ObservationWriter(
            tmp_path / str(index),
            country="US",
            scheduled=campaign.timestamp(START),
            started=campaign.timestamp(START),
            deadline=START.timestamp() + 60,
        )
        await writer.close()
    assert time.perf_counter() - began < 5


async def test_writer_failure_is_fatal_not_a_deadlock(tmp_path, monkeypatch):
    import atlas.onboarding.writer as module

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(module, "atomic_json", broken)
    writer = ObservationWriter(
        tmp_path,
        country="US",
        scheduled=campaign.timestamp(START),
        started=campaign.timestamp(START),
        deadline=START.timestamp() + 60,
        bounds=WriterBounds(chunk_rows=1, queue_size=1),
    )
    await writer.observe("203.0.113.1", START.timestamp() + 1)
    with pytest.raises(OSError, match="writer failed"):
        await asyncio.wait_for(writer.close(), 3)


async def test_onboarding_never_calls_normal_persistence(manifest, monkeypatch):
    from atlas import storage
    from atlas.sinks import JsonSink

    def forbidden(*args, **kwargs):
        pytest.fail("Onboarding reached normal persistence")

    monkeypatch.setattr(storage, "ingest", forbidden)
    monkeypatch.setattr(JsonSink, "start", forbidden)
    clock = Clock(START.timestamp())

    async def measure(config, sink, options):
        # Exercise the real shared runner while controlling campaign time outside it.
        config = RunConfig("generic_http", config.proxy, requests=1, concurrency=1)
        options = replace(options, deadline=None, transport_factory=FakeTransport)
        result = await run(config, sink, options)
        clock.value = START.timestamp() + 86400
        return result

    # Use a matching fake observation timestamp instead of wall time for this integration.
    import atlas.runner as runner

    monkeypatch.setattr(runner.time, "time", lambda: START.timestamp() + 1)
    result = await execute(manifest, "tor1", Proxy, clock=clock, run_country=measure)
    assert result["observations"] == 1


async def test_reordered_checkpoint_cannot_change_execution_order(manifest):
    data = campaign.load(manifest)
    state = new_state(data, "tor1", data["settings"])
    state["jobs"][0], state["jobs"][1] = state["jobs"][1], state["jobs"][0]
    atomic_json(manifest.parent / "regions/tor1/state.json", state)
    with pytest.raises(ValueError, match="queue differs"):
        await execute(manifest, "tor1", Proxy, RegionOptions(resume=True))
