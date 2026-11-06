import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from helpers import FakeTransport

from proxy_index.onboarding.files import read_json
from proxy_index.onboarding.fleet import classify, cpu_usage, render
from proxy_index.onboarding.manifest import timestamp
from proxy_index.onboarding.probe import ProbeOptions, execute_probe
from proxy_index.providers import load_provider
from proxy_index.runner import run

ROOT = Path(__file__).resolve().parents[1]


def test_cpu_monitor_uses_intervals_and_rejects_reused_pid():
    previous = {
        "server_time": 10,
        "processes": [{"pid": 3, "cpu_seconds": 2, "start_ticks": "100"}],
        "core_ticks": {"cpu0": {"total": 100, "idle": 20}},
    }
    current = {
        "server_time": 12,
        "processes": [
            {"pid": 3, "cpu_seconds": 3.5, "start_ticks": "100"},
            {"pid": 4, "cpu_seconds": 8, "start_ticks": "110"},
        ],
        "core_ticks": {"cpu0": {"total": 300, "idle": 70}},
    }
    result = cpu_usage(current, previous)
    assert result["processes"][0]["cpu_pct"] == 75
    assert "cpu_pct" not in result["processes"][1]
    assert result["core_cpu_pct"] == {"cpu0": 75}
    current["processes"][0] = {"pid": 3, "cpu_seconds": 1, "start_ticks": "900"}
    assert "cpu_pct" not in cpu_usage(current, previous)["processes"][0]


def probe_state(now, **extra):
    return {
        "kind": "probe",
        "status": "running",
        "updated_at": timestamp(now),
        "started_at": timestamp(now - 60),
        "country": "us",
        "active": {"country": "us"},
        "progress": {"requests": 1000, "successes": 800, "unique_ips": 600},
        **extra,
    }


def test_monitor_distinguishes_live_dead_stale_and_unreachable():
    now = time.time()
    data = {
        "region": "nyc3",
        "installed": True,
        "server_time": now,
        "state": probe_state(now),
        "service": {"ActiveState": "active"},
    }
    assert classify(dict(data), now)["health"] == "running"
    assert classify(data | {"state": probe_state(now - 30)}, now)["health"] == "stale"
    assert classify(data | {"service": {"ActiveState": "failed"}}, now)["health"] == "failed"
    assert classify({"error": "no SSH"}, now)["health"] == "unreachable"
    assert classify({"installed": False}, now)["health"] == "not_deployed"
    assert (
        classify(
            data
            | {
                "state": probe_state(
                    now,
                    status="completed",
                    measurement={"completed": 2000, "unique_ips": 900, "total_errors": 400},
                )
            },
            now,
        )["unique_ips"]
        == 900
    )


def test_monitor_detects_older_round_backlog():
    now = time.time()
    state = probe_state(
        now,
        kind="campaign",
        jobs=[
            {"round": 0, "status": "running", "scheduled_at": timestamp(now - 3700)},
            {"round": 1, "status": "pending", "scheduled_at": timestamp(now - 100)},
        ],
    )
    result = classify({"state": state, "server_time": now}, now)
    assert result["health"] == "behind" and result["queue_depth"] == 1


def test_dashboard_is_standalone_and_escapes_errors():
    page = render(
        {
            "collected_at": "now",
            "collected_epoch": 0,
            "servers": [{"region": "nyc3", "health": "failed", "error": "<script>bad</script>"}],
        }
    )
    assert b"&lt;script&gt;bad" in page
    assert b"MONITOR STALE" in page
    assert b"<script src=" not in page


async def test_probe_uses_shared_census_and_writes_live_and_final_status(tmp_path):
    proxy = load_provider(
        ROOT / "configs/shifter.toml",
        parameters={"country": "us"},
        environ={"SHIFTER_ACCOUNT": "fixture", "SHIFTER_PASSWORD": "fixture"},
    )

    async def measured(config, sink, options):
        assert config.concurrency == 7 and config.timeout_seconds == 2
        return await run(config, sink, replace(options, transport_factory=FakeTransport))

    result = await execute_probe(
        tmp_path,
        proxy,
        "nyc3",
        "us",
        ProbeOptions(concurrency=7, request_timeout=2),
        run_country=measured,
    )
    assert result["status"] == "completed"
    assert result["measurement"]["completed"] >= 1000
    assert result["measurement"]["unique_ips"] == 1
    assert result["observations"] == json.loads(json.dumps(result["measurement"]))["successes"]
    assert read_json(tmp_path / "state.json")["measurement"] == json.loads(
        json.dumps(result["measurement"])
    )
    with pytest.raises(ValueError, match="already exists"):
        await execute_probe(tmp_path, proxy, "nyc3", "us")
    with pytest.raises(ValueError, match="does not belong"):
        await execute_probe(tmp_path / "wrong", proxy, "fra1", "us")


def shifter(country="us"):
    return load_provider(
        ROOT / "configs/shifter.toml",
        parameters={"country": country},
        environ={"SHIFTER_ACCOUNT": "fixture", "SHIFTER_PASSWORD": "fixture"},
    )


@pytest.mark.parametrize(
    ("country", "kwargs", "message"),
    [
        ("de", {}, "must target the requested country"),
        ("us", {"max_seconds": 0}, "finite and positive"),
        ("us", {"start": timestamp(time.time() - 3600)}, "must be in the future"),
        ("us", {"start": timestamp(time.time() // 3600 * 3600 + 7230)}, "whole UTC hour"),
    ],
)
async def test_probe_rejects_bad_requests_before_creating_state(tmp_path, country, kwargs, message):
    with pytest.raises(ValueError, match=message):
        await execute_probe(tmp_path, shifter(country), "nyc3", "us", ProbeOptions(**kwargs))
    assert not (tmp_path / "state.json").exists()


async def test_probe_armed_for_a_later_hour_stops_before_measuring(tmp_path):
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)
    start = timestamp(time.time() // 3600 * 3600 + 7200)

    async def never(*args, **kwargs):
        raise AssertionError("A stopped probe must not measure")

    result = await execute_probe(
        tmp_path, shifter(), "nyc3", "us", ProbeOptions(start=start), stop=stop, run_country=never
    )
    assert result["status"] == "stopped" and result["scheduled_at"] == start
    assert read_json(tmp_path / "state.json")["status"] == "stopped"
    assert not (tmp_path / "summary.json").exists()


async def test_probe_failure_is_saved_and_reported_without_details(tmp_path):
    async def broken(config, sink, options):
        raise ConnectionError("private detail")

    with pytest.raises(RuntimeError, match=r"Probe failed \(ConnectionError\)") as raised:
        await execute_probe(tmp_path, shifter(), "nyc3", "us", run_country=broken)
    assert "private detail" not in str(raised.value)
    summary = read_json(tmp_path / "summary.json")
    assert summary["status"] == "failed" and summary["error_type"] == "ConnectionError"
    assert summary["active"] is None and summary["measurement"] is None


async def test_probe_checkpoint_error_stops_the_run_and_fails_it(tmp_path):
    async def measured(config, sink, options):
        await options.progress({"requests": 1, "unsaved": object()})
        assert options.stop.is_set()
        await options.progress({"requests": 2})

    with pytest.raises(RuntimeError, match=r"Probe failed \(TypeError\)"):
        await execute_probe(tmp_path, shifter(), "nyc3", "us", run_country=measured)
    summary = read_json(tmp_path / "summary.json")
    assert summary["status"] == "failed" and summary["error_type"] == "TypeError"
    assert summary["progress"]["requests"] == 2
