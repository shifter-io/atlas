"""ERR-5: the broad handlers in the onboarding cli and scheduler, the runner, the node and the
orchestrator jobs are narrowed, or are boundaries that record only the error's type and let an
interrupt, an exit or a cancellation go on. (The browser host's outbox loop is in
`test_cov_browser.py`.)"""

import asyncio
import json
import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest
from helpers import FakeTransport
from runner_cases import config
from test_error_boundaries import INTERRUPTS, failing_close
from test_node import assignment
from test_onboarding import ROOT, START, Clock, Proxy, fake_measurement

import atlas.onboarding.scheduler as scheduler
from atlas import JsonSink, RunOptions, run
from atlas.node import Active, NodeAgent, NodeSettings
from atlas.onboarding import cli as onboarding_cli
from atlas.onboarding import manifest as campaign
from atlas.onboarding.files import read_json
from atlas.orchestrator import jobs

SECRET = "token=hunter2"

# --- onboarding/cli.py -----------------------------------------------------------------------


def test_a_round_command_prints_only_the_type_of_any_error_and_an_interrupt_goes_on(capsys):
    def broken():
        raise LookupError(SECRET)

    def interrupted():
        raise KeyboardInterrupt

    assert onboarding_cli._round_command(broken) == 1
    assert capsys.readouterr().err == "Round command failed (LookupError).\n"
    with pytest.raises(KeyboardInterrupt):
        onboarding_cli._round_command(interrupted)


# --- onboarding/scheduler.py -----------------------------------------------------------------


def region_manifest(folder):
    return campaign.create(
        ROOT / "configs/shifter.toml",
        "residential",
        campaign.timestamp(START),
        folder,
        now=START - timedelta(hours=1),
    )


async def execute(path, measure):
    clock = Clock(START.timestamp())
    return await scheduler.execute(path, "tor1", Proxy, clock=clock, run_country=measure)


def region_state(path):
    return read_json(path.parent / "regions/tor1/state.json")


def fails(error):
    def raising(*args, **kwargs):
        raise error

    return raising


async def test_scheduler_checkpoint_stops_on_a_write_error_but_lets_a_bug_through(
    tmp_path, monkeypatch
):
    async def measured(config, sink, options):
        monkeypatch.setattr(scheduler, "peak_memory_mb", fails(KeyError("a bug")))
        with pytest.raises(KeyError):
            await options.progress({"requests": 1})
        assert not options.stop.is_set()
        monkeypatch.setattr(scheduler, "peak_memory_mb", lambda: 1.0)
        monkeypatch.setattr(scheduler, "atomic_json", fails(OSError("disk full")))
        await options.progress({"requests": 2})
        assert options.stop.is_set()
        monkeypatch.undo()
        return fake_measurement()

    path = region_manifest(tmp_path)
    with pytest.raises(OSError, match="disk full"):
        await execute(path, measured)
    state = region_state(path)
    assert state["status"] == "failed" and state["jobs"][0]["status"] == "failed"


@pytest.mark.parametrize("interrupt", INTERRUPTS)
async def test_scheduler_saves_an_interrupted_country_then_re_raises(tmp_path, interrupt):
    async def interrupted(config, sink, options):
        await options.observation_consumer("203.0.113.1", options.wall_clock())
        raise interrupt()

    path = region_manifest(tmp_path)
    with pytest.raises(interrupt):
        await execute(path, interrupted)
    state = region_state(path)
    job = state["jobs"][0]
    expected = "interrupted" if interrupt is asyncio.CancelledError else "failed"
    assert state["status"] == "failed" and job["status"] == expected
    assert job["observations"] == 1 and job["stop_reason"] == "error"
    (saved,) = (path.parent / "regions/tor1").rglob("summary.json")
    assert read_json(saved)["status"] == expected


async def test_scheduler_records_a_writer_failure_on_close(tmp_path, monkeypatch):
    async def quiet(config, sink, options):
        return fake_measurement()

    monkeypatch.setattr(scheduler, "ObservationWriter", failing_close(OSError("disk full")))
    path = region_manifest(tmp_path / "first")
    with pytest.raises(OSError, match="disk full"):
        await execute(path, quiet)
    assert region_state(path)["jobs"][0]["status"] == "failed"
    monkeypatch.setattr(scheduler, "ObservationWriter", failing_close(LookupError("a bug")))
    with pytest.raises(LookupError):
        await execute(region_manifest(tmp_path / "second"), quiet)


# --- runner.py -------------------------------------------------------------------------------


async def test_a_progress_error_of_any_type_is_logged_by_type_and_the_run_goes_on(tmp_path, caplog):
    def progress(counters):
        raise LookupError(SECRET)

    caplog.set_level(logging.WARNING)
    result = await run(
        config(requests=5),
        JsonSink(tmp_path),
        RunOptions(transport_factory=FakeTransport, progress=progress, progress_interval=0.001),
    )
    assert result.status == "completed"
    assert "Progress callback failed (LookupError)" in caplog.text
    assert "hunter2" not in caplog.text


async def test_an_unexpected_run_error_is_saved_as_failed_and_raised_by_type_only(tmp_path):
    async def broken(ip, at):
        raise LookupError(SECRET)

    sink = JsonSink(tmp_path)
    options = RunOptions(transport_factory=FakeTransport, observation_consumer=broken)
    with pytest.raises(RuntimeError, match=r"Run failed \(LookupError\)") as raised:
        await run(config(requests=5), sink, options)
    assert "hunter2" not in str(raised.value) and raised.value.__cause__ is None
    saved = json.loads((sink.directory / "summary.json").read_text())
    assert saved["status"] == "failed" and saved["stop_reason"] == "error"


# --- node.py ---------------------------------------------------------------------------------


def agent(tmp_path, enricher=None) -> NodeAgent:
    settings = NodeSettings(
        "http://orchestrator.invalid",
        "t",
        provider="shifter",
        product="residential",
        region="fra1",
        proxy_factory=lambda country: None,
        work_dir=tmp_path / "runs",
        enricher=enricher,
    )
    return NodeAgent(settings)


async def test_a_failed_database_reopen_of_any_type_keeps_the_release(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    node = agent(tmp_path, SimpleNamespace(reload=fails(LookupError(SECRET))))
    await node.reopen_database()  # the poll loop goes on
    assert "could not reopen the DB-IP database (LookupError)" in caplog.text
    assert "hunter2" not in caplog.text


@pytest.mark.parametrize("failure", [LookupError, asyncio.CancelledError])
async def test_a_batch_error_fails_only_that_batch_and_a_cancel_goes_on(tmp_path, caplog, failure):
    caplog.set_level(logging.WARNING)
    node, failed = agent(tmp_path), []

    async def fail(batch_id, error):
        failed.append((batch_id, error))

    node._fail = fail
    node.config = fails(failure(SECRET))
    active = node.active[7] = Active(assignment())
    if failure is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await node.execute(active)
        assert failed == []
    else:
        await node.execute(active)
        assert failed == [(7, "node error (LookupError)")]
        assert "batch 7 failed (LookupError)" in caplog.text
    assert node.active == {} and "hunter2" not in caplog.text


# --- orchestrator/jobs.py --------------------------------------------------------------------


class NoDatabase:
    def __init__(self, error):
        self.error = error

    def connection(self):
        raise self.error


async def test_a_report_error_is_logged_by_type_and_a_cancel_goes_on(caplog):
    caplog.set_level(logging.WARNING)
    assert await jobs.write_reports(NoDatabase(LookupError(f"dsn {SECRET}")), [1, 2]) is None
    assert "could not write the HTML reports for batches [1, 2] (LookupError)" in caplog.text
    assert "hunter2" not in caplog.text
    with pytest.raises(asyncio.CancelledError):
        await jobs.write_reports(NoDatabase(asyncio.CancelledError()), [3])
