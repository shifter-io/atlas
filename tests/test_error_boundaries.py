"""ERR-2: the broad handlers in onboarding and daily are narrowed, or are boundaries that record
the failure and let an interrupt, an exit or a cancellation go on."""

import asyncio
import threading

import pytest
from campaign_fakes import CAMPAIGN_PROJECT, CID, FakeDigitalOcean, running
from campaign_hour_cases import PRE_CREATE, FakeClock, make_root, make_spec
from test_daily import NOW, hanging_run, hours, in_flight, inputs, removed, template
from test_onboarding import START
from test_onboarding_monitor import shifter

import proxy_index.daily.runner as runner
import proxy_index.onboarding.probe as probe_module
from proxy_index.campaign import launch
from proxy_index.campaign.contracts import WORKER_TAG, campaign_tag, droplet_name, hour_tag
from proxy_index.campaign.tick import load_hour, run_tick, save_hour
from proxy_index.daily.runner import clean_up
from proxy_index.onboarding.files import read_json
from proxy_index.onboarding.manifest import timestamp
from proxy_index.onboarding.probe import execute_probe
from proxy_index.onboarding.writer import ObservationWriter, WriterBounds

INTERRUPTS = [KeyboardInterrupt, SystemExit, asyncio.CancelledError]

# --- onboarding/probe.py ---------------------------------------------------------------------


async def test_probe_checkpoint_stops_on_a_write_error_but_lets_a_bug_through(
    tmp_path, monkeypatch
):
    def fails(error):
        def raising(*args, **kwargs):
            raise error

        return raising

    async def measured(config, sink, options):
        monkeypatch.setattr(probe_module, "peak_memory_mb", fails(KeyError("a bug")))
        with pytest.raises(KeyError):
            await options.progress({"requests": 1})
        assert not options.stop.is_set()
        monkeypatch.setattr(probe_module, "peak_memory_mb", lambda: 1.0)
        monkeypatch.setattr(probe_module, "atomic_json", fails(OSError("disk full")))
        await options.progress({"requests": 2})
        assert options.stop.is_set()
        monkeypatch.undo()

    with pytest.raises(RuntimeError, match=r"Probe failed \(OSError\)"):
        await execute_probe(tmp_path, shifter(), "nyc3", "us", run_country=measured)
    assert read_json(tmp_path / "summary.json")["error_type"] == "OSError"


@pytest.mark.parametrize("interrupt", INTERRUPTS)
async def test_probe_interrupt_is_saved_with_the_partial_results_then_re_raised(
    tmp_path, interrupt
):
    async def interrupted(config, sink, options):
        await options.observation_consumer("203.0.113.1", options.wall_clock())
        raise interrupt()

    with pytest.raises(interrupt):
        await execute_probe(tmp_path, shifter(), "nyc3", "us", run_country=interrupted)
    summary = read_json(tmp_path / "summary.json")
    assert summary["status"] == "failed" and summary["error_type"] == interrupt.__name__
    assert summary["observations"] == 1 and summary["active"] is None


def failing_close(error):
    class Writer(ObservationWriter):
        async def close(self):
            await super().close()
            raise error

    return Writer


async def test_probe_records_a_writer_failure_on_close(tmp_path, monkeypatch):
    async def quiet(config, sink, options):
        return None

    monkeypatch.setattr(probe_module, "ObservationWriter", failing_close(OSError("disk full")))
    with pytest.raises(RuntimeError, match=r"Probe failed \(OSError\)"):
        await execute_probe(tmp_path, shifter(), "nyc3", "us", run_country=quiet)
    assert read_json(tmp_path / "summary.json")["error_type"] == "OSError"
    monkeypatch.setattr(probe_module, "ObservationWriter", failing_close(LookupError("a bug")))
    with pytest.raises(LookupError):
        await execute_probe(tmp_path / "other", shifter(), "nyc3", "us", run_country=quiet)


# --- onboarding/writer.py --------------------------------------------------------------------


@pytest.mark.parametrize("failure", [ValueError, KeyboardInterrupt, SystemExit])
async def test_writer_thread_failure_is_recorded_and_an_interrupt_goes_on(
    tmp_path, monkeypatch, failure
):
    def broken(self, chunk, item):
        raise failure("private detail")

    escaped = []
    monkeypatch.setattr(threading, "excepthook", lambda args: escaped.append(args.exc_type))
    monkeypatch.setattr(ObservationWriter, "_record", broken)
    writer = ObservationWriter(
        tmp_path,
        country="US",
        scheduled=timestamp(START),
        started=timestamp(START),
        deadline=START.timestamp() + 60,
        bounds=WriterBounds(chunk_rows=1, queue_size=1),
    )
    await writer.observe("203.0.113.1", START.timestamp() + 1)
    with pytest.raises(OSError, match="writer failed") as raised:
        await asyncio.wait_for(writer.close(), 3)
    assert "private detail" not in str(raised.value)
    assert isinstance(writer.error, failure)
    assert escaped == ([] if issubclass(failure, Exception) else [failure])


# --- daily/runner.py -------------------------------------------------------------------------


async def test_any_failed_take_of_in_flight_workers_still_retires_the_recorded_ones(tmp_path):
    """The clean-up guard is broad: even an unexpected error type stops no recorded removal."""
    spec = make_spec()
    root = make_root(tmp_path, spec)
    fake = FakeDigitalOcean()
    in_flight(root, fake, spec)
    record = load_hour(root, spec, 0)
    name = droplet_name(CID, 0, "lon1", spec.networks[1])
    tags = [WORKER_TAG, campaign_tag(CID), hour_tag(CID, 0)]
    recorded = fake.add(name, tags, project=CAMPAIGN_PROJECT)
    record.traffic[spec.networks[1]] = dict.fromkeys(launch.TRAFFIC_KEYS, 0)
    record.droplets.append(
        launch.DropletRecord(spec.networks[1], "lon1", 0, name, droplet_id=recorded, status="armed")
    )
    save_hour(root, record)

    async def failing(names, tag):
        raise LookupError("unexpected")

    async with running(fake) as cloud:
        cloud.adopt_uncertain = failing
        left, skipped = await clean_up(root, spec, cloud, FakeClock(PRE_CREATE))
    assert ("droplet", str(recorded)) in fake.deleted and str(recorded) not in left
    assert "LookupError" in skipped


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
async def test_an_interrupt_during_the_run_removes_every_worker_then_goes_on(
    tmp_path, monkeypatch, interrupt
):
    async def interrupted(slot):
        await run_tick(slot.root, launcher=slot.services[2], now_mode=True, slot=0)
        raise interrupt()

    monkeypatch.setattr(runner, "_work", interrupted)
    root = template(tmp_path)
    fake, clock = FakeDigitalOcean(), FakeClock(PRE_CREATE)
    async with running(fake) as cloud:
        services = hanging_run(tmp_path, cloud, clock, lambda: asyncio.sleep(0))
        due = inputs(tmp_path, root, GB=22, DE=22)
        with pytest.raises(interrupt):
            await runner.run_hour(due, services=services, now=NOW, clock=clock, limit=3600.0)
    (hour_root,) = hours(root)
    outcome = read_json(hour_root / "daily.json")
    assert outcome["error"] == interrupt.__name__ and outcome["leftover"] == []
    assert removed(fake, hour_root)
