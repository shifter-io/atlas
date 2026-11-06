"""CAMP-1d: the collector's orphan reconcile, its retires and its alerts.

Offline only: fake harvesters, a fake clock, the in-process fake DigitalOcean of the scope suite
(seeded with production-like droplets) and a fake SSH remote; the shared helpers are in
`campaign_tick_cases.py`. The daemon and alert tests are in `test_campaign_collector.py`.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta

import pytest
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    CID,
    PROD_PROJECT,
    SID,
    FakeDigitalOcean,
    running,
)
from campaign_tick_cases import (
    IPV4,
    OURS,
    START,
    Clock,
    FakeRemote,
    install_host_map,
    make_root,
    make_spec,
    pacing,
    worker,
)

from proxy_index.campaign import collector as collector_module
from proxy_index.campaign import tick as tick_module
from proxy_index.campaign.contracts import (
    WORKER_TAG,
    HourRecord,
    alerts_path,
    audit_path,
    hour_path,
    status_path,
)
from proxy_index.campaign.tick import load_hour, read_alerts, save_hour


# 7 and 11: orphan reconcile over the production-like fake account; no IPv4 anywhere.
async def test_orphan_reconcile_adopts_retires_and_never_touches_foreign_droplets(
    tmp_path, monkeypatch
):
    install_host_map(monkeypatch)
    root, clock = make_root(tmp_path), Clock(START + timedelta(hours=4))
    fake = FakeDigitalOcean()
    fake.seed_production(200)
    name = f"pic-{SID}-h03-{{}}-{{}}"
    gone = fake.add(name.format("lon1", "shifter"), OURS, project=CAMPAIGN_PROJECT)
    idle = fake.add(name.format("nyc3", "shifter"), OURS, project=CAMPAIGN_PROJECT)
    active = fake.add(name.format("lon1", "geonode"), OURS, project=CAMPAIGN_PROJECT)
    silent = fake.add(name.format("nyc3", "geonode"), OURS, project=CAMPAIGN_PROJECT)
    foreign = fake.add(name.format("ams3", "shifter"), OURS, project=PROD_PROJECT)
    bad_name = fake.add(f"pic-{SID}-oops", OURS, project=CAMPAIGN_PROJECT)
    beyond = fake.add(f"pic-{SID}-h30-lon1-shifter", OURS, project=CAMPAIGN_PROJECT)
    launching = fake.add(
        f"pic-{SID}-h04-lon1-shifter", [*OURS[:2], f"pi-camp-{SID}-h04"], project=None
    )
    record = HourRecord(
        CID,
        3,
        (START + timedelta(hours=3)).isoformat(),
        state="done",
        report_path="reports/traffic/hour-03.md",
        droplets=[worker("shifter", "lon1", 3, droplet_id=gone, status="destroyed")],
    )
    save_hour(root, record)
    save_hour(root, HourRecord(CID, 4, (START + timedelta(hours=4)).isoformat(), state="launching"))
    remote = FakeRemote(
        {
            name.format("nyc3", "shifter"): {"installed": True},
            name.format("lon1", "geonode"): {"assignment": {"kind": "round"}},
            name.format("nyc3", "geonode"): OSError("ssh down"),
        }
    )

    async def harvest(spec, record, root):
        return record

    async with running(fake) as cloud:
        status = await collector_module.Collector(
            root, harvest, cloud, lambda hour: remote, clock, pacing(clock)
        ).pass_once()
    assert sorted(fake.deleted) == sorted([("droplet", gone), ("droplet", idle)])
    assert {foreign, bad_name, beyond, launching, active, silent} <= set(fake.droplets)
    hour = load_hour(root, make_spec(), 3)
    by_id = {d.droplet_id: d for d in hour.droplets}
    assert by_id[idle].status == "destroyed" and by_id[idle].error_class == "orphan_not_started"
    assert by_id[active].status == "running" and hour.state == "harvesting"
    assert silent not in by_id and hour.report_path is None
    codes = {(a["code"], a["key"]) for a in read_alerts(root)}
    assert {
        ("foreign_tagged", foreign),
        ("foreign_tagged", bad_name),
        ("foreign_tagged", beyond),
        ("orphan_unreachable", silent),
        ("orphan_adopted", idle),
    } <= codes
    audit = [json.loads(line) for line in audit_path(root).read_text().splitlines()]
    assert [a["result"] for a in audit] == ["attempt", "deleted", "attempt", "deleted"]
    assert status["ratelimit_remaining"] == 4321
    late = fake.add(name.format("ams3", "geonode"), OURS, project=CAMPAIGN_PROJECT)
    remote.answers[name.format("ams3", "geonode")] = {"assignment": {"kind": "round"}}
    async with running(fake) as cloud:  # known droplets are left alone; a non-final hour stays
        await collector_module.Collector(
            root, harvest, cloud, lambda hour: remote, clock, pacing(clock)
        ).reconcile(make_spec(), tick_module.load_hours(root, make_spec()))
    hour = load_hour(root, make_spec(), 3)
    assert hour.state == "harvesting" and late in {d.droplet_id for d in hour.droplets}
    assert len(fake.deleted) == 2
    for path in (status_path(root), alerts_path(root), hour_path(root, 3)):
        assert not IPV4.search(path.read_text()), path


async def test_a_refused_or_failed_retire_is_recorded_and_alerted(tmp_path, monkeypatch):
    root, clock = make_root(tmp_path, project_id=PROD_PROJECT), Clock(START + timedelta(hours=4))
    fake = FakeDigitalOcean()
    first = fake.add(f"pic-{SID}-h03-lon1-shifter", OURS, project=PROD_PROJECT)
    second = fake.add(f"pic-{SID}-h03-nyc3-shifter", [WORKER_TAG], project=PROD_PROJECT)
    save_hour(
        root,
        HourRecord(
            CID,
            3,
            START.isoformat(),
            state="running",
            droplets=[worker("shifter", "lon1", 3, droplet_id=first, status="destroyed")],
        ),
    )
    async with running(fake) as cloud:
        collector = collector_module.Collector(root, None, cloud, None, clock, pacing(clock))
        spec = make_spec(project_id=PROD_PROJECT)
        hours = tick_module.load_hours(root, spec)
        await collector.reconcile(spec, hours)
        assert hours[3].droplets[0].status == "destroy_refused"

        async def broken(*_args, **_kwargs):
            raise ConnectionError("lost")

        monkeypatch.setattr(collector_module, "guarded_delete", broken)
        drop = worker("shifter", "nyc3", 3, droplet_id=second, status="destroyed")
        await collector._retire(collector_module.scope_for(spec), hours[3], drop, hours)
        assert drop.status == "destroy_failed"
    assert fake.deleted == []
    assert {a["code"] for a in read_alerts(root)} == {"destroy_refused", "destroy_failed"}


# Review findings S5 and S6: each test failed at 34bdc8b.
async def test_s5_failed_retires_are_retried_and_unreachable_orphans_keep_it_alive(tmp_path):
    root, clock = make_root(tmp_path), Clock(START + timedelta(hours=5))
    fake = FakeDigitalOcean()
    stuck = fake.add(f"pic-{SID}-h03-lon1-shifter", OURS, project=CAMPAIGN_PROJECT)
    lost = fake.add(f"pic-{SID}-h03-nyc3-shifter", OURS, project=CAMPAIGN_PROJECT)
    save_hour(
        root,
        HourRecord(
            CID,
            3,
            (START + timedelta(hours=3)).isoformat(),
            state="done",
            report_path="reports/traffic/hour-03.md",
            droplets=[worker("shifter", "lon1", 3, droplet_id=stuck, status="destroy_failed")],
        ),
    )
    remote = FakeRemote({f"pic-{SID}-h03-nyc3-shifter": OSError("ssh down")})

    async def harvest(spec, record, root):
        return record

    async with running(fake) as cloud:
        status = await collector_module.Collector(
            root, harvest, cloud, lambda hour: remote, clock, pacing(clock)
        ).pass_once()
    assert fake.deleted == [("droplet", stuck)] and lost in fake.droplets
    assert load_hour(root, make_spec(), 3).droplets[0].status == "destroyed"
    assert status["live_workers"] == 1 and not status["finished"]


async def test_s6_an_adopted_active_orphan_gets_its_host_map_entry(tmp_path, monkeypatch):
    root, clock = make_root(tmp_path), Clock(START + timedelta(hours=5))
    hosts = install_host_map(monkeypatch)
    fake = FakeDigitalOcean()
    active = fake.add(f"pic-{SID}-h03-lon1-shifter", OURS, project=CAMPAIGN_PROJECT)
    save_hour(
        root,
        HourRecord(
            CID,
            3,
            (START + timedelta(hours=3)).isoformat(),
            state="done",
            report_path="reports/traffic/hour-03.md",
        ),
    )
    remote = FakeRemote({f"pic-{SID}-h03-lon1-shifter": {"assignment": {"kind": "round"}}})

    async def harvest(spec, record, root):
        return record

    async with running(fake) as cloud:
        await collector_module.Collector(
            root, harvest, cloud, lambda hour: remote, clock, pacing(clock)
        ).pass_once()
    assert hosts.read_hosts(root, 3) == {active: "203.0.113.10"}
    assert not IPV4.search(hour_path(root, 3).read_text())


@pytest.mark.parametrize("case", ["no_ipv4", "no_launcher"])
async def test_r2_r4_an_active_orphan_without_a_host_entry_stays_unreachable(
    tmp_path, monkeypatch, case
):
    root, clock = make_root(tmp_path), Clock(START + timedelta(hours=5))
    if case == "no_ipv4":
        install_host_map(monkeypatch)
    else:  # CAMP-1c's launcher is not installed yet
        monkeypatch.setitem(sys.modules, "proxy_index.campaign.launch", None)
    fake = FakeDigitalOcean()
    name = f"pic-{SID}-h03-lon1-shifter"
    active = fake.add(
        name, OURS, project=CAMPAIGN_PROJECT, ip=None if case == "no_ipv4" else "203.0.113.10"
    )
    stuck = fake.add(f"pic-{SID}-h03-nyc3-shifter", OURS, project=CAMPAIGN_PROJECT)
    save_hour(
        root,
        HourRecord(
            CID,
            3,
            (START + timedelta(hours=3)).isoformat(),
            state="done",
            report_path="reports/traffic/hour-03.md",
            droplets=[worker("shifter", "nyc3", 3, droplet_id=stuck, status="destroyed")],
        ),
    )
    remote = FakeRemote({name: {"assignment": {"kind": "round"}}})

    async def harvest(spec, record, root):
        return record

    async with running(fake) as cloud:
        status = await collector_module.Collector(
            root, harvest, cloud, lambda hour: remote, clock, pacing(clock)
        ).pass_once()
    hour = load_hour(root, make_spec(), 3)
    assert active not in {d.droplet_id for d in hour.droplets} and hour.state == "done"
    assert fake.deleted == [("droplet", stuck)]  # the rest of the reconcile still ran
    assert status["live_workers"] == 1 and not (root / ".runtime" / "hosts-03.json").exists()
    assert {a["code"] for a in read_alerts(root)} == {"orphan_unreachable"}
