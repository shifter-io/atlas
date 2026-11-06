"""Local simulations: no provider traffic, database, orchestrator or website."""

import asyncio
import gzip
import json
import stat
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import FakeTransport

from proxy_index import ProxyConfig, RunConfig, RunOptions, run
from proxy_index.cli import main
from proxy_index.models import Outcome
from proxy_index.onboarding import manifest as campaign
from proxy_index.onboarding.export import export_campaign
from proxy_index.onboarding.files import atomic_json, lock, read_json
from proxy_index.onboarding.scheduler import execute, new_state
from proxy_index.onboarding.writer import ObservationWriter, SummarySink

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2027, 1, 1, 22, tzinfo=UTC)


@pytest.fixture
def manifest(tmp_path):
    return campaign.create(
        ROOT / "configs/shifter.toml",
        "residential",
        campaign.timestamp(START),
        tmp_path / "onboarding-networks",
        now=START - timedelta(hours=1),
    )


class Clock:
    def __init__(self, now):
        self.value = now

    def now(self):
        return self.value

    async def sleep(self, seconds):
        # Scheduler idle waits advance to the next UTC hour; the deadline watcher
        # observes the same clock without accelerating an active country's work.
        if seconds >= 0.2:
            self.value = (self.value // 3600 + 1) * 3600
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(0.001)


class Proxy:
    def __init__(self, country):
        self.country = country.lower()

    def public(self):
        return {"provider": "shifter", "product": "residential", "requested_country": self.country}

    def resolve(self, context):
        return ProxyConfig("http", "localhost", 8080)


def fake_measurement(**extra):
    values = dict(status="completed", stop_reason="unique_ratio", completed=1000, total_errors=0)
    values.update(extra)
    return SimpleNamespace(**values, to_dict=lambda: values)


def test_manifest_freezes_all_countries_in_existing_order(manifest):
    data = campaign.load(manifest)
    assert len(data["rounds"]) == 24
    assert data["rounds"][0] == "2027-01-01T22:00:00.000Z"
    assert data["rounds"][-1] == "2027-01-02T21:00:00.000Z"
    assert data["ends_at"] == "2027-01-02T22:00:00.000Z"
    mapping = campaign.load_regions()
    assert data["regions"] == {r.slug: list(r.countries) for r in mapping.regions}
    assert sum(map(len, data["regions"].values())) == 249
    assert data["settings"]["timeout_seconds"] == 10
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "start",
    [
        "2027-01-01T22:00:00",
        "2027-01-01T22:01:00Z",
        "2027-01-01T21:00:00Z",
        "2027-01-01T22:00:00+04:00",
    ],
)
def test_reject_invalid_start(tmp_path, start):
    with pytest.raises(ValueError):
        campaign.create(
            ROOT / "configs/shifter.toml",
            "residential",
            start,
            tmp_path,
            now=START - timedelta(minutes=30),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("concurrency", 0),
        ("concurrency", True),
        ("concurrency", 1.5),
        ("timeout_seconds", 0),
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", float("inf")),
    ],
)
def test_invalid_settings(field, value):
    with pytest.raises(ValueError):
        campaign.settings(campaign.DEFAULTS | {field: value})


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
        clock=clock,
        run_country=measure,
        concurrency=37,
        request_timeout=3.5,
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

    await execute(manifest, "tor1", Proxy, clock=clock, run_country=measure, resume=True)
    assert calls == [("pm", 7, 4)]
    saved = read_json(path)
    assert saved["jobs"][1]["status"] == "interrupted"
    with pytest.raises(ValueError, match="preserve"):
        await execute(manifest, "tor1", Proxy, resume=True, concurrency=8)
    # A closed campaign is inert even when explicitly resumed.
    await execute(manifest, "tor1", Proxy, clock=clock, run_country=measure, resume=True)
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
        chunk_rows=2,
        queue_size=2,
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
        chunk_seconds=0.02,
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
    import proxy_index.onboarding.writer as module

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(module, "atomic_json", broken)
    writer = ObservationWriter(
        tmp_path,
        country="US",
        scheduled=campaign.timestamp(START),
        started=campaign.timestamp(START),
        deadline=START.timestamp() + 60,
        chunk_rows=1,
        queue_size=1,
    )
    await writer.observe("203.0.113.1", START.timestamp() + 1)
    with pytest.raises(OSError, match="writer failed"):
        await asyncio.wait_for(writer.close(), 3)


async def test_export_verifies_chunks_and_reports_missing_regions(manifest):
    data = campaign.load(manifest)
    state = new_state(data, "tor1", data["settings"])
    job = state["jobs"][0]
    job.update(status="completed", started_at=data["starts_at"], observations=2)
    directory = manifest.parent / "regions/tor1"
    atomic_json(directory / "state.json", state)
    folder = directory / "rounds/00/ca"
    writer = ObservationWriter(
        folder,
        country="CA",
        scheduled=data["starts_at"],
        started=data["starts_at"],
        deadline=START.timestamp() + 86400,
        chunk_rows=1,
    )
    for _ in range(2):
        await writer.observe("203.0.113.1", START.timestamp() + 1)
    await writer.close()
    (folder / "observations-000003.jsonl.gz.partial").write_bytes(b"truncated")
    result = export_campaign(manifest)
    assert result["rows"] == 2 and result["chunks"] == 2 and not result["complete"]
    assert any("uncommitted" in issue for issue in result["issues"])
    assert any("fra1: missing" in issue for issue in result["issues"])
    assert len(list(gzip.open(manifest.parent / "export/observations.jsonl.gz", "rt"))) == 2
    assert (manifest.parent / "export/report.html").read_text().startswith("<!doctype html>")
    original_meta = next(folder.glob("*.meta.json"))
    duplicate_meta = folder / "observations-999999.jsonl.meta.json"
    duplicate_meta.write_bytes(original_meta.read_bytes())
    with pytest.raises(ValueError, match="sidecar"):
        export_campaign(manifest)
    duplicate_meta.unlink()
    chunk = next(folder.glob("*.gz"))
    chunk.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        export_campaign(manifest)


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


def test_cli_options_and_no_database_flags(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["onboarding", "run", "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "--concurrency" in text and "--timeout" in text and "--resume" in text
    assert "--database" not in text


def test_runtime_git_and_package_exclusion():
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=ROOT,
        input="onboarding-networks/shifter/residential/test/campaign.json\n",
        text=True,
        capture_output=True,
        check=True,
    )
    assert "onboarding-networks/" in result.stdout
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert (
        "onboarding-networks/**" in config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    )


@pytest.mark.slow
@pytest.mark.scale
async def test_two_million_observations_stream_with_bounded_queue(tmp_path):
    writer = ObservationWriter(
        tmp_path,
        country="US",
        scheduled=campaign.timestamp(START),
        started=campaign.timestamp(START),
        deadline=START.timestamp() + 60,
        queue_size=4096,
    )
    for _ in range(2_000_000):
        await writer.observe("2001:db8::1", START.timestamp() + 1)
    await writer.close()
    assert writer.rows == 2_000_000 and writer.peak_queue <= 4096
    count = 0
    for path in sorted(tmp_path.glob("*.gz")):
        with gzip.open(path, "rt") as stream:
            for line in stream:
                row = json.loads(line)
                assert row["ip"] == "2001:db8::1"
                count += 1
    assert count == 2_000_000


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
    import proxy_index.runner as runner

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


async def test_partial_country_is_exportable_after_crash_without_replay(manifest):
    data = campaign.load(manifest)
    state = new_state(data, "tor1", data["settings"])
    state["status"] = "running"
    state["jobs"][0].update(status="running", started_at=data["starts_at"])
    directory = manifest.parent / "regions/tor1"
    atomic_json(directory / "state.json", state)
    writer = ObservationWriter(
        directory / "rounds/00/ca",
        country="CA",
        scheduled=data["starts_at"],
        started=data["starts_at"],
        deadline=START.timestamp() + 86400,
    )
    await writer.observe("203.0.113.1", START.timestamp() + 1)
    await writer.close()
    await execute(manifest, "tor1", Proxy, resume=True, clock=Clock(START.timestamp() + 86400))
    result = export_campaign(manifest)
    assert result["rows"] == 1 and not result["complete"]
    assert result["regions"][0]["observations"] == 1


async def test_onboarding_never_calls_normal_persistence(manifest, monkeypatch):
    from proxy_index import storage
    from proxy_index.sinks import JsonSink

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
    import proxy_index.runner as runner

    monkeypatch.setattr(runner.time, "time", lambda: START.timestamp() + 1)
    result = await execute(manifest, "tor1", Proxy, clock=clock, run_country=measure)
    assert result["observations"] == 1


def test_export_rejects_swapped_regional_state(manifest):
    data = campaign.load(manifest)
    wrong = new_state(data, "fra1", data["settings"])
    atomic_json(manifest.parent / "regions/tor1/state.json", wrong)
    with pytest.raises(ValueError, match="Checkpoint"):
        export_campaign(manifest)


async def test_reordered_checkpoint_cannot_change_execution_order(manifest):
    data = campaign.load(manifest)
    state = new_state(data, "tor1", data["settings"])
    state["jobs"][0], state["jobs"][1] = state["jobs"][1], state["jobs"][0]
    atomic_json(manifest.parent / "regions/tor1/state.json", state)
    with pytest.raises(ValueError, match="queue differs"):
        await execute(manifest, "tor1", Proxy, resume=True)


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


def test_ephemeral_manifest_records_rule_4(tmp_path):
    def plan(name, **options):
        return campaign.load(
            campaign.create(
                ROOT / "configs/shifter.toml",
                "residential",
                campaign.timestamp(START),
                tmp_path / name,
                now=START - timedelta(hours=1),
                **options,
            )
        )

    data = plan("rule-4", execution_mode="ephemeral")
    assert data["settings"]["answer_classes"] is True and data["census_rule_version"] == 4
    assert data["census_ratio_basis"] == "completed_requests_excluding_rate_limits"
    off = plan("off", execution_mode="ephemeral", answer_classes=False)
    assert "answer_classes" not in off["settings"] and off["census_rule_version"] == 2
    retry = plan("retry", execution_mode="ephemeral", retry_http_469=True)
    assert "answer_classes" not in retry["settings"] and retry["census_rule_version"] == 3
    parallel = plan("parallel")
    assert "answer_classes" not in parallel["settings"] and parallel["census_rule_version"] == 2
    # A version 3 manifest written before rule 4 still validates under rule 2.
    old = dict(
        data,
        settings={k: v for k, v in data["settings"].items() if k != "answer_classes"},
        census_rule_version=2,
        census_ratio_basis="completed_requests",
    )
    assert campaign.validate(old) is old
    with pytest.raises(ValueError, match="census rule"):
        campaign.validate(dict(data, census_rule_version=2))


def test_answer_classes_refused_outside_ephemeral_or_with_469(tmp_path):
    def plan(**options):
        return campaign.create(
            ROOT / "configs/shifter.toml",
            "residential",
            campaign.timestamp(START),
            tmp_path,
            now=START - timedelta(hours=1),
            **options,
        )

    for options, message in (
        ({"answer_classes": True}, "ephemeral"),
        (
            {
                "answer_classes": True,
                "execution_mode": "sequential",
                "region_order": ["lon1", "fra1", "ams3", "nyc3", "tor1", "sgp1", "blr1", "syd1"],
            },
            "ephemeral",
        ),
        (
            {"answer_classes": True, "retry_http_469": True, "execution_mode": "ephemeral"},
            "retry_http_469",
        ),
        ({"answer_classes": "yes", "execution_mode": "ephemeral"}, "boolean"),
        ({"run_settings": {"answer_classes": True}, "execution_mode": "ephemeral"}, "boolean"),
    ):
        with pytest.raises(ValueError, match=message):
            plan(**options)
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValueError, match="retry_http_469"):
        campaign.settings(campaign.DEFAULTS | {"answer_classes": True, "retry_http_469": True})
    with pytest.raises(ValueError, match="boolean"):
        campaign.settings(campaign.DEFAULTS | {"answer_classes": 1})
    data = campaign.load(plan())
    data["settings"]["answer_classes"] = True
    data.update(
        census_rule_version=4, census_ratio_basis="completed_requests_excluding_rate_limits"
    )
    with pytest.raises(ValueError, match="ephemeral"):
        campaign.validate(data)
