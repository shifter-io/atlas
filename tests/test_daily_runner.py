"""DAILY-1: the daily test's runner, offline.

The runner's clean-up on failure, at the time limit and on a signal, against the in-process fake
DigitalOcean, and the `daily run` CLI. No droplet or request outside the test is touched.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fakes import CAMPAIGN_PROJECT, CID, FakeDigitalOcean, running
from campaign_hour_cases import PRE_CREATE, FakeClock, FakeRemote, make_root, make_spec
from daily_cases import (
    NOW,
    Borrowed,
    formulas,
    hanging_run,
    hours,
    in_flight,
    inputs,
    removed,
    template,
)
from onboarding_round_cases import make

from atlas.campaign import launch
from atlas.campaign.collector import Pacing
from atlas.campaign.contracts import (
    WORKER_TAG,
    campaign_tag,
    droplet_name,
    hour_path,
    hour_tag,
    spec_path,
)
from atlas.campaign.do_api import ApiError, ScopeError
from atlas.campaign.tick import load_hour, save_hour
from atlas.cli import main
from atlas.daily.runner import RunError, RunInputs, clean_up, run_hour, time_limit
from atlas.onboarding import manifest as campaign
from atlas.onboarding.files import lock, read_json


async def test_a_failure_after_the_launch_still_removes_every_worker(tmp_path):
    root = template(tmp_path)
    fake, clock = FakeDigitalOcean(), FakeClock(PRE_CREATE)
    async with running(fake) as cloud:
        remote = FakeRemote(clock)

        async def broken(spec, record, hour_root):
            await launch.launch_hour(
                spec, record, hour_root, cloud=cloud, remote_factory=lambda h: remote, clock=clock
            )
            raise RuntimeError("the manager lost its connection")

        def services(hour_root, spec):
            return Borrowed(cloud), None, broken, None

        due = inputs(tmp_path, root, GB=22, DE=22, US=3)
        with pytest.raises(RuntimeError):
            await run_hour(due, services=services, now=NOW, clock=clock)
    (hour_root,) = hours(root)
    outcome = read_json(hour_root / "daily.json")
    assert outcome["error"] == "RuntimeError" and outcome["leftover"] == []
    assert outcome["plan"]["droplets"] == 2
    record = read_json(hour_path(hour_root, 0))
    ids = [d["droplet_id"] for d in record["droplets"]]
    assert len(ids) == 2 and len(remote.pushes) == 2
    assert all(("droplet", str(i)) in fake.deleted and str(i) not in fake.droplets for i in ids)
    spec = json.loads(spec_path(hour_root).read_text())
    assert spec["regions"] == ["lon1", "fra1"] and spec["hours"] == 1
    hour = campaign.load(hour_root / spec["manifests"]["shifter"])
    planned = campaign.load(root / make_spec(networks=("shifter",)).manifests["shifter"])
    assert hour["due_countries"] == {"lon1": ["GB"], "fra1": ["DE"]}
    assert hour["settings"] == planned["settings"]
    assert hour["target_url"] == planned["target_url"]


async def test_a_worker_still_tagged_for_the_hour_is_reported_left_over(tmp_path):
    spec = make_spec()
    root = make_root(tmp_path, spec)
    fake = FakeDigitalOcean()
    stray = fake.add("atlas-stray", tags=[hour_tag(CID, 0)])
    async with running(fake) as cloud:
        assert await clean_up(root, spec, cloud, FakeClock(PRE_CREATE)) == ([str(stray)], None)
        assert await clean_up(root, make_spec(synthetic=True), cloud) == ([], None)


async def test_a_worker_whose_create_was_in_flight_is_taken_by_name_and_removed(tmp_path):
    spec = make_spec()
    root = make_root(tmp_path, spec)
    fake = FakeDigitalOcean()
    worker = in_flight(root, fake, spec)
    fake.add(droplet_name(CID, 0, "lon1", spec.networks[0]), ["atlas-camp-other"])  # not ours
    async with running(fake) as cloud:
        assert await clean_up(root, spec, cloud, FakeClock(PRE_CREATE)) == ([], None)
    assert ("droplet", str(worker)) in fake.deleted and str(worker) not in fake.droplets
    assert len(fake.droplets) == 1  # the same name without our tags stays
    assert read_json(hour_path(root, 0))["droplets"][0]["status"] == "destroyed"


async def test_a_held_hour_lock_says_why_nothing_was_removed(tmp_path):
    spec = make_spec()
    root = make_root(tmp_path, spec)
    fake = FakeDigitalOcean()
    worker = in_flight(root, fake, spec)
    async with running(fake) as cloud:
        with lock(hour_path(root, 0).parent / ".lock"):
            left, skipped = await clean_up(root, spec, cloud, FakeClock(PRE_CREATE))
    assert left == [str(worker)] and "locked by a launcher still running" in skipped


@pytest.mark.parametrize("fault", [ApiError("GET", "/v2/droplets", 500), ScopeError("refused")])
async def test_a_failed_take_of_in_flight_workers_still_retires_the_recorded_ones(tmp_path, fault):
    """N1: an API or scope error while taking an in-flight worker stops no recorded removal."""
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
        raise fault

    async with running(fake) as cloud:
        cloud.adopt_uncertain = failing
        left, skipped = await clean_up(root, spec, cloud, FakeClock(PRE_CREATE))
    assert ("droplet", str(recorded)) in fake.deleted and str(recorded) not in left
    assert type(fault).__name__ in skipped and "locked" not in skipped


async def test_the_time_limit_stops_a_run_that_never_settles_and_removes_its_workers(tmp_path):
    root = template(tmp_path)
    fake, clock = FakeDigitalOcean(), FakeClock(PRE_CREATE)
    async with running(fake) as cloud:
        services = hanging_run(tmp_path, cloud, clock, lambda: asyncio.sleep(3600))
        due = inputs(tmp_path, root, GB=22, DE=22)
        outcome = await run_hour(due, services=services, now=NOW, clock=clock, limit=1.0)
    (hour_root,) = hours(root)
    assert outcome["error"] == "stopped at the time limit (1 s)" and outcome["leftover"] == []
    assert read_json(hour_root / "daily.json") == outcome and removed(fake, hour_root)


def test_the_time_limit_is_no_longer_the_slot_plus_a_margin():
    # DAILY-OVERTIME-1 (#1152): a country test runs until it is finished; the limit is the
    # safety stop and its grace (test_daily_overtime.py), whatever the slot.
    begins = datetime(2027, 1, 1, 22, tzinfo=UTC)
    assert time_limit(begins, begins - timedelta(minutes=2)) == (6 * 60 + 17) * 60
    assert time_limit(begins, begins + timedelta(hours=7)) == 0.0


async def test_a_timeout_inside_the_run_is_a_failure_not_the_time_limit(tmp_path):
    """N2: a TimeoutError raised by the run itself is re-raised; the workers still go."""
    root = template(tmp_path)
    fake, clock = FakeDigitalOcean(), FakeClock(PRE_CREATE)

    async def times_out():
        raise TimeoutError("a socket timed out")

    async with running(fake) as cloud:
        services = hanging_run(tmp_path, cloud, clock, times_out)
        due = inputs(tmp_path, root, GB=22, DE=22)
        with pytest.raises(TimeoutError, match="socket"):
            await run_hour(due, services=services, now=NOW, clock=clock, limit=3600.0)
    (hour_root,) = hours(root)
    outcome = read_json(hour_root / "daily.json")
    assert outcome["error"] == "TimeoutError" and outcome["leftover"] == []
    assert removed(fake, hour_root)


def test_a_signal_during_the_run_still_removes_every_worker(tmp_path, monkeypatch, capsys):
    """SIGTERM through `daily run`: the run is cancelled and the clean-up still runs."""
    import atlas.daily.runner as runner

    root = template(tmp_path)
    fake, clock = FakeDigitalOcean(), FakeClock(PRE_CREATE)
    real = runner.run_hour

    async def signalled():
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.sleep(2)
        raise AssertionError("the signal did not stop the run")

    async def run_with_fakes(due):
        async with running(fake) as cloud:
            services = hanging_run(tmp_path, cloud, clock, signalled)
            return await real(due, services=services, now=NOW, clock=clock)

    monkeypatch.setattr(runner, "run_hour", run_with_fakes)
    folder = formulas(tmp_path, {"shifter": {"GB": 22, "DE": 22}})
    args = ["daily", "run", "--formulas", str(folder), "--template", str(root)]
    previous = signal.signal(signal.SIGTERM, lambda *_: None)  # never kill the test runner
    try:
        assert main(args + ["--start", "next-hour"]) == 3
    finally:
        signal.signal(signal.SIGTERM, previous)
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["error"] == "cancelled by a signal" and outcome["leftover"] == []
    (hour_root,) = hours(root)
    assert removed(fake, hour_root)


async def test_an_hour_with_nothing_due_writes_nothing(tmp_path):
    root = template(tmp_path)
    outcome = await run_hour(inputs(tmp_path, root, GB=3), now=NOW)
    assert outcome["state"] == "nothing_due" and outcome["plan"]["droplets"] == 0
    assert hours(root) == []


async def test_a_region_missing_from_the_template_is_refused(tmp_path):
    root = template(tmp_path)
    with pytest.raises(RunError, match="no region tor1"):
        await run_hour(inputs(tmp_path, root, CA=22), now=NOW)
    assert hours(root) == []


async def test_a_template_that_is_not_ephemeral_leaves_no_hour_behind(tmp_path):
    root = template(tmp_path)
    path = root / make_spec(networks=("shifter",)).manifests["shifter"]
    shutil.copyfile(make(tmp_path / "parallel", execution_mode="parallel"), path)
    with pytest.raises(RunError, match="not an ephemeral campaign"):
        await run_hour(inputs(tmp_path, root, GB=22), now=NOW)
    assert hours(root) == []


async def test_a_finished_hour_is_done_with_nothing_left(tmp_path):
    root = template(tmp_path)

    async def finished(spec, record, hour_root):
        record.state, record.finished_at = "done", NOW.isoformat()
        save_hour(hour_root, record)

    async def harvester(*args, **kwargs):
        raise AssertionError("nothing is left to harvest")

    def services(hour_root, spec):
        return None, None, finished, harvester

    pacing = Pacing(interval=0, clock_synced=lambda: True)
    outcome = await run_hour(
        inputs(tmp_path, root, GB=22), services=services, now=NOW, pacing=pacing
    )
    assert outcome["state"] == "done" and outcome["leftover"] == []


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        ({"state": "nothing_due"}, 0),
        ({"state": "done", "leftover": []}, 0),
        ({"state": "done", "leftover": ["1"]}, 3),
        ({"state": "failed", "leftover": []}, 3),
    ],
)
def test_run_cli_exits_3_unless_the_hour_is_done_and_nothing_is_left(
    tmp_path, monkeypatch, capsys, outcome, code
):
    import atlas.daily.runner as runner

    seen = []

    async def fake_run(inputs):
        seen.append(inputs)
        return outcome

    monkeypatch.setattr(runner, "run_hour", fake_run)
    args = ["daily", "run", "--formulas", str(tmp_path), "--template", str(tmp_path / "t")]
    assert main(args + ["--start", "next-hour"]) == code
    assert json.loads(capsys.readouterr().out) == outcome
    assert seen == [RunInputs(tmp_path / "t", tmp_path, "next-hour")]
