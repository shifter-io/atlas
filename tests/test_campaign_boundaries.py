"""ERR-1: the collector's and the launcher's error handlers, typed or a tested boundary.

A boundary (`# boundary:` in the source) catches every error on purpose: one hour, region or
worker never stops the others, and a guarded removal that failed is retried later. It records
the error's class, never its message. A narrowed handler catches only what its call raises;
anything else reaches the boundary above it. Offline: fake clouds and remotes only.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from campaign_fakes import CAMPAIGN_PROJECT, CID, SID, FakeDigitalOcean, running
from campaign_hour_cases import NETWORKS, REGIONS, SCHEDULED, FakeClock, launched
from campaign_hour_cases import make_root as make_hour_root
from campaign_hour_cases import make_spec as make_hour_spec
from campaign_tick_cases import OURS, START, Clock, FakeRemote, make_root, make_spec, pacing

from proxy_index.campaign import collector as collector_module
from proxy_index.campaign import launch, tick
from proxy_index.campaign.contracts import (
    DropletRecord,
    HourRecord,
    alerts_path,
    droplet_name,
    status_path,
)
from proxy_index.campaign.fanout import RemoteError
from proxy_index.campaign.tick import read_alerts, save_hour

SECRET = "token-" + "x" * 40  # a message no record may keep


class Unexpected(Exception):
    """No narrowed handler names this type: only a boundary catches it."""


def alerts(root, code):
    return [a for a in read_alerts(root) if a["code"] == code]


async def harvest_nothing(spec, record, root):
    return record


async def test_a_harvest_error_of_any_type_stays_in_its_hour(tmp_path):
    root, clock = make_root(tmp_path), Clock(START + timedelta(hours=2))
    for hour in (0, 1):
        save_hour(root, HourRecord(CID, hour, START.isoformat(), state="running"))
    harvested = []

    async def harvest(spec, record, root):
        if record.hour == 0:
            raise Unexpected(SECRET)
        harvested.append(record.hour)
        return record

    await collector_module.Collector(root, harvest, None, None, clock, pacing(clock)).pass_once()
    [alert] = alerts(root, "harvest_error")
    assert (alert["key"], alert["error"], harvested) == ("h00-Unexpected", "Unexpected", [1])
    assert SECRET not in alerts_path(root).read_text()


async def test_a_reconcile_error_of_any_type_is_alerted_and_the_pass_goes_on(tmp_path):
    root, clock = make_root(tmp_path), Clock(START + timedelta(hours=2))

    class Broken:
        ratelimit_remaining = 900

        async def list_tag(self, tag):
            raise Unexpected(SECRET)

    collector = collector_module.Collector(
        root, harvest_nothing, Broken(), None, clock, pacing(clock)
    )
    await collector.pass_once()
    [alert] = alerts(root, "reconcile_error")
    assert alert["error"] == "Unexpected" and collector.reconcile_failures == 1
    assert status_path(root).exists()  # the rest of the pass still ran
    assert SECRET not in alerts_path(root).read_text()


def orphan_world(tmp_path):
    root, clock = make_root(tmp_path), Clock(START + timedelta(hours=5))
    save_hour(root, HourRecord(CID, 3, (START + timedelta(hours=3)).isoformat(), state="done"))
    fake = FakeDigitalOcean()
    names = [f"pic-{SID}-h03-{region}-shifter" for region in ("lon1", "nyc3")]
    for name in names:
        fake.add(name, OURS, project=CAMPAIGN_PROJECT)
    return root, clock, fake, names


async def test_an_orphan_inspect_catches_only_what_an_ssh_inspect_raises(tmp_path):
    root, clock, fake, names = orphan_world(tmp_path)
    remote = FakeRemote({names[0]: RemoteError("inspect failed"), names[1]: ValueError("not json")})
    async with running(fake) as cloud:
        collector = collector_module.Collector(
            root, None, cloud, lambda hour: remote, clock, pacing(clock)
        )
        await collector.reconcile(make_spec(), tick.load_hours(root, make_spec()))
        unreachable = set(collector.unreachable)
        assert len(unreachable) == 2 and not fake.deleted
        remote.answers[names[0]] = Unexpected("a bug, not an unreachable worker")
        with pytest.raises(Unexpected):
            await collector.reconcile(make_spec(), tick.load_hours(root, make_spec()))
    assert {a["key"] for a in alerts(root, "orphan_unreachable")} == unreachable


async def test_a_failed_retire_of_any_type_is_recorded_and_retried(tmp_path, monkeypatch):
    root, clock = make_root(tmp_path), Clock(START + timedelta(hours=4))
    drop = DropletRecord("shifter", "lon1", 3, f"pic-{SID}-h03-lon1-shifter", "1")
    save_hour(root, HourRecord(CID, 3, START.isoformat(), state="done", droplets=[drop]))

    async def broken(*_args, **_kwargs):
        raise Unexpected(SECRET)

    monkeypatch.setattr(collector_module, "guarded_delete", broken)
    hours = tick.load_hours(root, make_spec())
    [drop] = hours[3].droplets
    collector = collector_module.Collector(root, None, None, None, clock, pacing(clock))
    await collector._retire(collector_module.scope_for(make_spec()), hours[3], drop, hours)
    assert drop.status == "destroy_failed" and collector._retry(hours[3], drop)
    assert tick.load_hours(root, make_spec())[3].droplets[0].status == "destroy_failed"
    assert [a["key"] for a in alerts(root, "destroy_failed")] == ["1-Unexpected"]


def hour_work(tmp_path, remote):
    spec = make_hour_spec(NETWORKS[:1], REGIONS[:1])
    root = make_hour_root(tmp_path, spec)
    worker = DropletRecord("net00", "lon1", 0, droplet_name(CID, 0, "lon1", "net00"), "1")
    record = HourRecord(CID, 0, SCHEDULED, droplets=[worker])
    work = launch._Launch(spec, record, root, None, lambda hour: remote, FakeClock(0.0))
    return work, worker


async def test_a_failed_launch_retire_of_any_type_is_recorded(tmp_path, monkeypatch):
    work, worker = hour_work(tmp_path, None)

    async def broken(*_args, **_kwargs):
        raise Unexpected(SECRET)

    monkeypatch.setattr(launch.scope, "guarded_delete", broken)
    await work.retire(worker)
    assert (worker.status, worker.error_class, worker.destroyed_at) == (
        "destroy_failed",
        "Unexpected",
        None,
    )


async def test_an_error_of_any_type_stays_in_its_region(tmp_path, monkeypatch):
    spec = make_hour_spec(NETWORKS[:1], REGIONS[:2])
    original = launch._Launch._create_call

    async def broken(self, workers):
        if workers[0].region == "fra1":
            raise Unexpected(SECRET)
        return await original(self, workers)

    monkeypatch.setattr(launch._Launch, "_create_call", broken)
    record, *_ = await launched(tmp_path, FakeDigitalOcean(), spec)
    by_region = {d.region: d for d in record.droplets}
    assert by_region["lon1"].status == "armed"
    assert (by_region["fra1"].status, by_region["fra1"].error_class) == (
        "create_failed",
        "Unexpected",
    )
    assert record.cells == {"net00/lon1": "running", "net00/fra1": "failed_ours"}


async def test_a_bring_up_error_of_any_type_fails_only_its_worker(tmp_path):
    spec = make_hour_spec(NETWORKS[:1], REGIONS[:2])
    broken = droplet_name(CID, 0, "fra1", "net00")

    def setup(remote):
        ready, raised = remote.wait_ready, []

        async def wait_ready(droplet, limit_seconds):
            if droplet.name == broken and not raised:
                raised.append(droplet.name)
                raise Unexpected(SECRET)
            return await ready(droplet, limit_seconds)

        remote.wait_ready = wait_ready

    record, *_ = await launched(tmp_path, FakeDigitalOcean(), spec, setup=setup)
    first = next(d for d in record.droplets if d.name == broken)
    assert (first.status, first.error_class) == ("destroyed", "Unexpected")
    assert first.error_detail["error"] == "Unexpected"
    assert SECRET not in first.error_detail["message"]
    assert {d.region for d in record.droplets if d.status == "armed"} == {"lon1", "fra1"}


class Remote:
    """A push that raises `error`; an inspect that raises `read_error`, else shows the unit."""

    def __init__(self, error=None, read_error=None):
        self.error, self.read_error, self.inspected = error, read_error, 0

    async def push_start(self, droplet, payload, command):
        raise self.error

    async def inspect(self, droplet):
        self.inspected += 1
        if self.read_error:
            raise self.read_error
        return {"service": {"ActiveState": "active"}}


@pytest.mark.parametrize("error", [RemoteError("x"), OSError("x"), ValueError("not json")])
async def test_an_inspect_catches_only_what_an_ssh_inspect_raises(tmp_path, error):
    work, _ = hour_work(tmp_path, Remote(read_error=error))
    assert await work._started(object()) is None
    work.remote = Remote(read_error=Unexpected("a bug"))
    with pytest.raises(Unexpected):
        await work._started(object())


@pytest.mark.parametrize("error", [RemoteError("x"), OSError("x")])
async def test_a_push_catches_only_what_an_ssh_push_raises(tmp_path, error):
    remote = Remote(error)
    work, worker = hour_work(tmp_path, remote)
    work._bundle = lambda worker: b""
    assert await work._push(worker, object(), resumed=False) is True
    assert remote.inspected == 1 and worker.error_class is None  # started despite the error
    work.remote = remote = Remote(Unexpected("a bug"))
    with pytest.raises(Unexpected):
        await work._push(worker, object(), resumed=False)
    assert remote.inspected == 0  # never read as if the push had only failed over SSH
