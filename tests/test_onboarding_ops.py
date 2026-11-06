import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest
from test_onboarding import START, Clock, Proxy, fake_measurement

from proxy_index.onboarding import manifest
from proxy_index.onboarding.files import read_json
from proxy_index.onboarding.scheduler import execute

ROOT = Path(__file__).resolve().parents[1]


async def test_calibration_stops_before_second_round_even_when_first_overruns(
    tmp_path,
    monkeypatch,
):
    root = ROOT
    spec = importlib.util.spec_from_file_location(
        "calibration_driver", root / "scripts/onboarding-calibration.py"
    )
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    path = manifest.create(
        root / "configs/shifter.toml",
        "residential",
        manifest.timestamp(START),
        tmp_path,
        now=START - timedelta(hours=1),
    )
    clock = Clock(START.timestamp())
    calls = []

    async def measured(config, sink, options):
        calls.append(config.requested_country)
        clock.value += 3700  # Every country overruns the next hourly boundary.
        return fake_measurement()

    async def scheduled(*args, **kwargs):
        return await execute(*args, **kwargs, clock=clock)

    monkeypatch.setattr(driver, "run", measured)
    monkeypatch.setattr(driver, "execute", scheduled)
    monkeypatch.setattr(
        driver, "load_provider", lambda _, parameters, **kw: Proxy(parameters["country"])
    )
    monkeypatch.setattr(driver.asyncio.get_running_loop(), "add_signal_handler", lambda *a: None)
    result = await driver.calibrate(path, "tor1", root / "configs/shifter.toml", None)
    assert calls == ["ca", "gl", "pm"]
    assert result["status"] == "stopped"
    jobs = read_json(path.parent / "regions/tor1/state.json")["jobs"]
    assert all(j["status"] == "completed" for j in jobs[:3])
    assert all(j["status"] == "pending" for j in jobs[3:])


async def test_region_checkpoint_is_refused_unless_resumed_and_a_crash_fails_it(tmp_path):
    path = manifest.create(
        ROOT / "configs/shifter.toml",
        "residential",
        manifest.timestamp(START),
        tmp_path,
        now=START - timedelta(hours=1),
    )
    with pytest.raises(ValueError, match="No region checkpoint"):
        await execute(path, "tor1", Proxy, resume=True)

    async def crashed(config, sink, options):
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        await execute(path, "tor1", Proxy, clock=Clock(START.timestamp()), run_country=crashed)
    state = read_json(path.parent / "regions/tor1/state.json")
    assert state["status"] == "failed" and state["jobs"][0]["status"] == "failed"
    with pytest.raises(ValueError, match="use --resume"):
        await execute(path, "tor1", Proxy)
