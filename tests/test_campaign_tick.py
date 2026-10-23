"""CAMP-1d: hourly tick, collector daemon, orphan reconcile, alarms, traffic reports.

Offline only: fake launchers and harvesters, a fake clock, the in-process fake DigitalOcean of
the scope suite (seeded with production-like droplets) and a fake SSH remote.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import types
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    CID,
    MANAGER_ID,
    PROD_PROJECT,
    SID,
    SPEC_TIME,
    FakeDigitalOcean,
    running,
)

from proxy_index.campaign import collector as collector_module
from proxy_index.campaign import tick as tick_module
from proxy_index.campaign.collector import Pacing, run_collector, settled
from proxy_index.campaign.contracts import (
    WORKER_TAG,
    CampaignSpec,
    DropletRecord,
    HourRecord,
    alerts_path,
    audit_path,
    hour_path,
    spec_path,
    status_path,
)
from proxy_index.campaign.do_api import ApiError
from proxy_index.campaign.reports import write_traffic_report
from proxy_index.campaign.tick import (
    due_at,
    load_hour,
    read_alerts,
    run_tick,
    run_watchdog,
    save_hour,
    set_paused,
)
from proxy_index.cli import main

START = datetime(2027, 1, 1, 22, tzinfo=UTC)
IPV4 = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
OURS = [WORKER_TAG, f"pi-camp-{SID}", f"pi-camp-{SID}-h03"]


def make_spec(**changes) -> CampaignSpec:
    base = CampaignSpec(
        campaign_id=CID, created_at=SPEC_TIME, account="leftclick", project_id=CAMPAIGN_PROJECT,
        starts_at=START.isoformat(), grace_hours=48, regions=("lon1", "nyc3"),
        networks=("shifter", "geonode"), manifests={"shifter": "a.json", "geonode": "b.json"},
        size_by_region={"lon1": "c-2", "nyc3": "c-2"},
        image_by_region={"lon1": "11", "nyc3": "12"}, image_name="pi-camp-worker-0.9.5",
        wheel_sha256="a" * 64, framework_version="0.9.5", ssh_key_id="9",
        manager_droplet_id=MANAGER_ID)
    return replace(base, **changes)


def make_root(tmp_path: Path, **changes) -> Path:
    root = tmp_path / "campaign"
    root.mkdir()
    spec_path(root).write_text(make_spec(**changes).validate().to_json())
    return root


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def worker(network, region, hour, **values) -> DropletRecord:
    return DropletRecord(network, region, hour, f"pic-{SID}-h{hour:02d}-{region}-{network}",
                         **values)


def fake_launcher(clock, calls):
    async def launch(spec, record, root):
        calls.append((record.hour, clock(), spec.networks))
        record.droplets = [worker(net, region, record.hour, droplet_id=f"9{record.hour:02d}{i}",
                                  status="armed", requested_at=clock().isoformat())
                           for i, (net, region) in enumerate(
                               (n, r) for n in spec.networks for r in spec.regions)]
        record.state = "running"
        return record

    return launch


# 1, 4: 24 launches at :58:30, once each, in order; the timer is disabled after slot 23.
async def test_every_slot_launches_at_5830_exactly_once_in_order(tmp_path):
    root, calls, disabled = make_root(tmp_path), [], []
    clock = Clock(START - timedelta(minutes=5))
    launcher = fake_launcher(clock, calls)
    assert await run_tick(root, launcher=launcher, clock=clock) == []
    for slot in range(24):
        clock.now = START + timedelta(hours=slot) - timedelta(seconds=90)
        assert clock.now.strftime("%M:%S") == "58:30"
        assert await run_tick(root, launcher=launcher, clock=clock,
                              disable_timer=lambda: disabled.append(1)) == [slot]
        clock.now += timedelta(seconds=10)
        assert await run_tick(root, launcher=launcher, clock=clock,
                              disable_timer=lambda: disabled.append(1)) == []
        assert load_hour(root, make_spec(), slot).launch_delay_seconds == 0
    assert [slot for slot, _, _ in calls] == list(range(24))
    assert disabled == [1, 1]  # the slot-23 tick and the later one; then the timer is gone
    assert read_alerts(root) == []


# 2: a late timer launches every missed slot at once, each with its delay; nothing skipped.
async def test_a_late_timer_launches_every_missed_slot_with_its_delay(tmp_path):
    root, calls = make_root(tmp_path), []
    clock = Clock(due_at(make_spec(), 0) + timedelta(hours=3, minutes=10))
    assert await run_tick(root, launcher=fake_launcher(clock, calls), clock=clock) == [0, 1, 2, 3]
    delays = [load_hour(root, make_spec(), k).launch_delay_seconds for k in range(4)]
    assert delays == [11400.0, 7800.0, 4200.0, 600.0]
    assert [a["key"] for a in read_alerts(root) if a["code"] == "late_launch"] == [
        "h00", "h01", "h02", "h03"]
    assert load_hour(root, make_spec(), 4).state == "planned"


async def test_a_crashed_launch_resumes_its_record_and_a_failure_is_raised(tmp_path):
    root, calls = make_root(tmp_path), []
    clock = Clock(due_at(make_spec(), 0))

    async def crash(spec, record, root):
        raise RuntimeError("tick killed mid-launch")

    with pytest.raises(RuntimeError):
        await run_tick(root, launcher=crash, clock=clock)
    record = load_hour(root, make_spec(), 0)
    assert record.state == "launching" and record.fired_at == clock().isoformat()
    clock.now += timedelta(seconds=5)
    assert await run_tick(root, launcher=fake_launcher(clock, calls), clock=clock) == [0]
    assert load_hour(root, make_spec(), 0).fired_at == record.fired_at  # resumed, not re-fired


# 3: concurrent ticks are serialized by flock(ledger/.lock).
def test_concurrent_ticks_are_serialized_by_the_lock(tmp_path):
    root, calls, active, peak = make_root(tmp_path), [], [0], [0]
    clock = Clock(due_at(make_spec(), 0))
    inner = fake_launcher(clock, calls)

    async def slow(spec, record, root):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.3)
        active[0] -= 1
        return await inner(spec, record, root)

    threads = [threading.Thread(target=lambda: asyncio.run(
        run_tick(root, launcher=slow, clock=clock))) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak[0] == 1 and len(calls) == 1


async def test_now_mode_launches_only_the_given_slot_and_keeps_the_timer(tmp_path):
    root, calls = make_root(tmp_path), []
    clock = Clock(START + timedelta(minutes=7))
    assert await run_tick(root, launcher=fake_launcher(clock, calls), clock=clock,
                          now_mode=True, slot=0, disable_timer=lambda: 1 / 0) == [0]
    assert read_alerts(root) == []  # a dry run "now" is not a late launch
    assert await run_tick(root, launcher=fake_launcher(clock, calls), clock=clock,
                          now_mode=True, slot=0) == []
    with pytest.raises(ValueError):
        await run_tick(root, launcher=fake_launcher(clock, calls), clock=clock, now_mode=True)


# 9: pause and resume.
async def test_pause_and_resume_change_only_later_hours(tmp_path):
    root, calls = make_root(tmp_path), []
    clock = Clock(due_at(make_spec(), 0))
    assert set_paused(root, "shifter", True) == ["shifter"]
    await run_tick(root, launcher=fake_launcher(clock, calls), clock=clock)
    assert calls[-1][2] == ("geonode",)
    assert load_hour(root, make_spec(), 0).alerts == [
        {"code": "network_paused", "level": "info", "networks": ["shifter"]}]
    assert set_paused(root, "shifter", False) == []
    clock.now = due_at(make_spec(), 1)
    await run_tick(root, launcher=fake_launcher(clock, calls), clock=clock)
    assert calls[-1][2] == ("shifter", "geonode")
    set_paused(root, "shifter", True)
    set_paused(root, "geonode", True)
    clock.now = due_at(make_spec(), 2)
    await run_tick(root, launcher=fake_launcher(clock, calls), clock=clock)
    assert len(calls) == 2 and load_hour(root, make_spec(), 2).state == "done"
    with pytest.raises(ValueError):
        set_paused(root, "unknown", True)


# 8: watchdog alarms.
def test_the_watchdog_raises_missed_launch_and_starts_the_tick(tmp_path):
    root, started = make_root(tmp_path), []
    clock = Clock(START + timedelta(seconds=30))

    def check(kind, enabled=True):
        return run_watchdog(root, check=kind, clock=clock, start_tick=lambda: started.append(1),
                            timer_enabled=lambda: enabled)

    assert check("armed") == "missed_launch" and started == [1]
    assert check("armed") == "missed_launch" and len(read_alerts(root)) == 1  # one alert
    record = load_hour(root, make_spec(), 0)
    record.state, record.fired_at = "running", clock().isoformat()
    save_hour(root, record)
    assert check("armed") is None
    record.state = "launching"
    save_hour(root, record)
    assert check("armed", enabled=False) is None  # not planned: a stopped timer is no alarm
    clock.now = START + timedelta(minutes=2)
    assert check("fired") is None
    clock.now = START + timedelta(hours=1, minutes=2)
    assert check("fired") == "missed_launch"
    for outside in (START - timedelta(hours=1), START + timedelta(hours=24, minutes=1)):
        clock.now = outside
        assert check("armed") is None


def finishing_harvester(clock, finish_on: dict[int, int]):
    """Hour h becomes done on its finish_on[h]-th harvest pass."""
    passes: dict[int, int] = {}

    async def harvest(spec, record, root):
        passes[record.hour] = passes.get(record.hour, 0) + 1
        if passes[record.hour] < finish_on[record.hour]:
            record.state = "harvesting"
            return record
        stamp = clock().isoformat()
        for drop in record.droplets:
            drop.status, drop.finished_at, drop.collected_at, drop.destroyed_at = (
                "destroyed", stamp, stamp, stamp)
            drop.bytes_up, drop.bytes_down, drop.requests, drop.successes = 10**8, 4 * 10**8, 50, 45
            drop.errors = 5
        record.state = "done"
        record.cells = {f"{d.network}/{d.region}": "complete" for d in record.droplets}
        return record

    return harvest


def running_hour(root, hour, clock):
    record = HourRecord(CID, hour, (START + timedelta(hours=hour)).isoformat(), state="running")
    record.droplets = [worker(net, region, hour, droplet_id=f"8{hour}{i}", status="running",
                              requested_at=clock().isoformat(), started_at=clock().isoformat())
                       for i, (net, region) in enumerate(
                           [("shifter", "lon1"), ("shifter", "nyc3"), ("geonode", "lon1")])]
    save_hour(root, record)


def disk(free_pct):
    """A fixed disk reading: the collector's alerts never depend on the test host's disk."""
    return lambda _path: types.SimpleNamespace(total=100, free=free_pct)


def pacing(clock, step=timedelta(seconds=30)):
    async def sleep(_seconds):
        clock.now += step

    return Pacing(sleep=sleep, clock_synced=lambda: True, disk_usage=disk(50))


# 5: the collector finalizes overlapping hours independently, then exits when done.
async def test_the_collector_finalizes_overlapping_hours_independently(tmp_path):
    root, clock = make_root(tmp_path), Clock(START + timedelta(minutes=70))
    running_hour(root, 0, clock)
    running_hour(root, 1, clock)
    seen = []

    async def recorded(spec, record, root):
        result = await harvest(spec, record, root)
        seen.append((record.hour, (root / "reports/traffic/hour-01.md").exists(),
                     (root / "reports/traffic/hour-00.md").exists()))
        return result

    harvest = finishing_harvester(clock, {0: 3, 1: 1})
    status = await run_collector(root, harvester=recorded, cloud=None, remote_factory=None,
                                 clock=clock, until_done=True, pacing=pacing(clock))
    assert seen[:2] == [(0, False, False), (1, False, False)]
    assert seen[2] == (0, True, False)  # hour 1 was reported while hour 0 still ran
    assert settled(status) and not status["finished"]
    hours = {row["hour"]: row for row in status["hours"]}
    assert hours[0]["state"] == hours[1]["state"] == "done" and hours[2]["state"] == "planned"
    assert hours[0]["cells"] == {"complete": 3} and status["live_workers"] == 0
    assert status["traffic_latest"] == {"hour": 1, "path": "reports/traffic/hour-01.md",
                                        "total_bytes": 15 * 10**8, "top_network": "shifter",
                                        "top_bytes": 10**9}
    assert status["next_launch_at"] == due_at(make_spec(), 2).isoformat()
    assert len((root / "reports/traffic.tsv").read_text().splitlines()) == 1 + 4


async def test_the_daemon_exits_only_when_every_hour_is_final(tmp_path):
    root, clock = make_root(tmp_path, synthetic=True), Clock(START + timedelta(hours=25))
    for hour in range(24):
        running_hour(root, hour, clock)
    status = await run_collector(root, harvester=finishing_harvester(clock, dict.fromkeys(
        range(24), 2)), cloud=None, remote_factory=None, clock=clock, pacing=pacing(clock))
    assert status["finished"] and status["next_launch_at"] is None


# 6: traffic report totals equal the sum of the records (golden markdown and TSV).
GOLDEN = """# Campaign hour 03 — traffic per network

Campaign `20270101t220000z-0123456789ab`, hour scheduled 2027-01-02T01:00:00+00:00, state done. \
Start lateness: 0.2–1.4 s. Concurrency per worker: 2000. Launch delay: 0.4 s.

| Network | GB up | GB down | GB total | Requests | Successes | Error % | Countries | Regions \
| Slowest region | Worker-hours | Cumulative GB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|
| geonode | 0.200 | 1.000 | 1.200 | 300 | 270 | 10.0 | 20/25 | 1/2 | lon1 (2h 00m) \
| 2.50 | 1.200 |
| shifter | 0.300 | 2.700 | 3.000 | 700 | 693 | 1.0 | 50/50 | 2/2 | nyc3 (1h 30m) \
| 3.00 | 4.000 |
| **Total** | 0.500 | 3.700 | 4.200 | 1000 | 963 | 3.7 | 70/75 | 3/4 |  | 5.50 | 5.200 |

Cumulative GB: every worker collected so far, in all hours, when this report was written.

## CPU per region (worst worker)

| Region | Workers | CPU p50 % | CPU p95 % | CPU max % | Load1 p95 | Memory max % |
|---|---:|---:|---:|---:|---:|---:|
| lon1 | 2 | 41.0 | 71.0 | 93.0 | 1.9 | 38.0 |
| nyc3 | 1 | 30.0 | 60.0 | 80.0 | 1.5 | 30.0 |

Bytes are the framework's client-side wire counts (`bytes_up` and `bytes_down` of each country \
summary); a provider's own billing may differ slightly.
"""


def report_fixture(root):
    base = "2027-01-02T00:58:30+00:00"

    def at(minutes):
        return (datetime.fromisoformat(base) + timedelta(minutes=minutes)).isoformat()

    cpu = {"cpu_p50_pct": 30.0, "cpu_p95_pct": 60.0, "cpu_max_pct": 80.0, "load1_p95": 1.5,
           "mem_used_max_pct": 30.0, "concurrency": 2000}
    hot = cpu | {"cpu_p50_pct": 41.0, "cpu_p95_pct": 71.0, "cpu_max_pct": 93.0, "load1_p95": 1.9,
                 "mem_used_max_pct": 38.0}
    done = {"status": "destroyed", "requested_at": base, "started_at": at(1.5)}
    drops = [
        worker("shifter", "lon1", 3, droplet_id="1", bytes_up=10**8, bytes_down=9 * 10**8,
               requests=300, successes=297, errors=3, finished_at=at(61.5), collected_at=at(62),
               destroyed_at=at(90), start_lateness_seconds=0.2, **done, **cpu | hot),
        worker("shifter", "nyc3", 3, droplet_id="2", bytes_up=2 * 10**8, bytes_down=18 * 10**8,
               requests=400, successes=396, errors=4, finished_at=at(91.5), collected_at=at(92),
               destroyed_at=at(90), start_lateness_seconds=1.4, **done, **cpu),
        worker("geonode", "lon1", 3, droplet_id="3", bytes_up=2 * 10**8, bytes_down=10**9,
               requests=300, successes=270, errors=30, finished_at=at(121.5), collected_at=at(122),
               destroyed_at=at(150), **done, **cpu | hot),
    ]
    record = HourRecord(CID, 3, "2027-01-02T01:00:00+00:00", state="done", droplets=drops,
                        launch_delay_seconds=0.4,
                        traffic={"shifter": {"countries_done": 50, "countries_total": 50,
                                             "worker_hours": 3.0},
                                 "geonode": {"countries_done": 20, "countries_total": 25,
                                             "worker_hours": 2.5}})
    save_hour(root, record)
    earlier = HourRecord(CID, 2, "2027-01-02T00:00:00+00:00", state="done", droplets=[
        worker("shifter", "lon1", 2, droplet_id="4", bytes_up=5 * 10**8, bytes_down=5 * 10**8,
               collected_at=base)])
    save_hour(root, earlier)
    return record


def test_the_traffic_report_totals_equal_the_sum_of_the_records(tmp_path):
    root = make_root(tmp_path)
    record = report_fixture(root)
    path = write_traffic_report(root, 3)
    assert path == root / "reports" / "traffic" / "hour-03.md"
    assert path.read_text() == GOLDEN
    total = sum(d.bytes_up + d.bytes_down for d in record.droplets)
    assert "| **Total** | 0.500 | 3.700 | 4.200 |" in GOLDEN and total == 42 * 10**8
    assert (root / "reports" / "traffic.tsv").read_text().splitlines() == [
        "hour\tnetwork\tbytes_up\tbytes_down\trequests\tsuccesses\terrors\tcountries_done\t"
        "countries_total\tregions_done\tworker_hours",
        "02\tshifter\t500000000\t500000000\t0\t0\t0\t0\t0\t0\t0.000",
        "03\tgeonode\t200000000\t1000000000\t300\t270\t30\t20\t25\t1\t2.500",
        "03\tshifter\t300000000\t2700000000\t700\t693\t7\t50\t50\t2\t3.000"]


def test_an_hour_without_samples_or_traffic_still_reports(tmp_path):
    root = make_root(tmp_path)
    save_hour(root, HourRecord(CID, 0, START.isoformat(), state="failed"))
    text = write_traffic_report(root, 0).read_text()
    assert "No CPU samples recorded." in text and "Start lateness: not recorded" in text


def install_host_map(monkeypatch):
    """CAMP-1c's host-map helpers (`launch.hosts_path`, `launch.read_hosts`), same format."""
    def path(root, hour):
        return root / ".runtime" / f"hosts-{hour:02d}.json"

    hosts = types.SimpleNamespace(hosts_path=path, read_hosts=lambda root, hour: (
        json.loads(path(root, hour).read_text()) if path(root, hour).exists() else {}))
    monkeypatch.setitem(sys.modules, "proxy_index.campaign.launch", hosts)
    return hosts


class FakeRemote:
    def __init__(self, answers):
        self.answers = answers

    async def inspect(self, droplet):
        answer = self.answers[droplet.name]
        if isinstance(answer, Exception):
            raise answer
        return answer


# 7 and 11: orphan reconcile over the production-like fake account; no IPv4 anywhere.
async def test_orphan_reconcile_adopts_retires_and_never_touches_foreign_droplets(
        tmp_path, monkeypatch):
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
    launching = fake.add(f"pic-{SID}-h04-lon1-shifter", [*OURS[:2], f"pi-camp-{SID}-h04"],
                         project=None)
    record = HourRecord(CID, 3, (START + timedelta(hours=3)).isoformat(), state="done",
                        report_path="reports/traffic/hour-03.md", droplets=[
                            worker("shifter", "lon1", 3, droplet_id=gone, status="destroyed")])
    save_hour(root, record)
    save_hour(root, HourRecord(CID, 4, (START + timedelta(hours=4)).isoformat(),
                               state="launching"))
    remote = FakeRemote({name.format("nyc3", "shifter"): {"installed": True},
                         name.format("lon1", "geonode"): {"assignment": {"kind": "round"}},
                         name.format("nyc3", "geonode"): OSError("ssh down")})

    async def harvest(spec, record, root):
        return record

    async with running(fake) as cloud:
        status = await collector_module.Collector(
            root, harvest, cloud, lambda hour: remote, clock, pacing(clock)).pass_once()
    assert sorted(fake.deleted) == sorted([("droplet", gone), ("droplet", idle)])
    assert {foreign, bad_name, beyond, launching, active, silent} <= set(fake.droplets)
    hour = load_hour(root, make_spec(), 3)
    by_id = {d.droplet_id: d for d in hour.droplets}
    assert by_id[idle].status == "destroyed" and by_id[idle].error_class == "orphan_not_started"
    assert by_id[active].status == "running" and hour.state == "harvesting"
    assert silent not in by_id and hour.report_path is None
    codes = {(a["code"], a["key"]) for a in read_alerts(root)}
    assert {("foreign_tagged", foreign), ("foreign_tagged", bad_name), ("foreign_tagged", beyond),
            ("orphan_unreachable", silent), ("orphan_adopted", idle)} <= codes
    audit = [json.loads(line) for line in audit_path(root).read_text().splitlines()]
    assert [a["result"] for a in audit] == ["attempt", "deleted", "attempt", "deleted"]
    assert status["ratelimit_remaining"] == 4321
    late = fake.add(name.format("ams3", "geonode"), OURS, project=CAMPAIGN_PROJECT)
    remote.answers[name.format("ams3", "geonode")] = {"assignment": {"kind": "round"}}
    async with running(fake) as cloud:  # known droplets are left alone; a non-final hour stays
        await collector_module.Collector(root, harvest, cloud, lambda hour: remote, clock,
                                         pacing(clock)).reconcile(
            make_spec(), tick_module.load_hours(root, make_spec()))
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
    save_hour(root, HourRecord(CID, 3, START.isoformat(), state="running", droplets=[
        worker("shifter", "lon1", 3, droplet_id=first, status="destroyed")]))
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


async def test_collector_alerts_for_record_alerts_errors_and_the_manager_host(tmp_path):
    root = make_root(tmp_path, synthetic=True)
    clock = Clock(START + timedelta(hours=12, minutes=1))
    record = HourRecord(CID, 0, START.isoformat(), state="running", alerts=[
        {"code": "auth_failed", "level": "high", "network": "shifter"}], droplets=[
        worker("shifter", "lon1", 0, droplet_id="1", status="running"),
        worker("shifter", "nyc3", 0, droplet_id=None, status="create_failed")])
    save_hour(root, record)
    save_hour(root, HourRecord(CID, 1, START.isoformat(), state="running"))

    async def harvest(spec, record, root):
        if record.hour == 1:
            raise TimeoutError("ssh")
        return record

    cloud = types.SimpleNamespace(ratelimit_remaining=0)
    status = await collector_module.Collector(root, harvest, cloud, None, clock, Pacing(
        clock_synced=lambda: False, disk_usage=disk(10))).pass_once()
    codes = {(a["code"], a["level"]) for a in read_alerts(root)}
    assert {("auth_failed", "high"),
            ("create_failures", "high"), ("harvest_error", "info"), ("disk_low", "high"),
            ("clock_unsynced", "high"), ("ratelimit_low", "high")} == codes
    assert status["alerts_open"] == 5 and status["disk_free_pct"] == 10.0
    assert status["clock_synced"] is False and status["live_workers"] == 1


def test_ntp_synced_reads_timedatectl(monkeypatch):
    replies = iter([types.SimpleNamespace(stdout="yes\n"), types.SimpleNamespace(stdout="no\n")])
    monkeypatch.setattr(collector_module.subprocess, "run", lambda *a, **k: next(replies))
    assert collector_module.ntp_synced() is True
    assert collector_module.ntp_synced() is False

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("timedatectl")

    monkeypatch.setattr(collector_module.subprocess, "run", missing)
    assert collector_module.ntp_synced() is None


def test_the_default_systemd_hooks(tmp_path, monkeypatch):
    root, commands = make_root(tmp_path), []

    def run(command, **_kwargs):
        commands.append(command)
        return types.SimpleNamespace(stdout="enabled\n")

    monkeypatch.setattr(tick_module.subprocess, "run", run)
    clock = Clock(START + timedelta(seconds=30))
    assert run_watchdog(root, check="armed", clock=clock) == "missed_launch"
    unit = f"proxy-index-campaign-tick@{CID}"
    assert commands == [["systemctl", "is-enabled", f"{unit}.timer"],
                        ["systemctl", "start", "--no-block", f"{unit}.service"]]
    for slot in range(24):
        save_hour(root, HourRecord(CID, slot, START.isoformat(), state="running"))
    asyncio.run(run_tick(root, launcher=None, clock=clock))
    assert commands[-1] == ["systemctl", "disable", "--now", f"{unit}.timer"]


# The CLI wiring, with fake CAMP-1c modules standing in for launch, harvest and fanout.
def test_the_campaign_commands(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, synthetic=True)
    created = {}

    async def launch_hour(spec, record, root, *, cloud, remote_factory, clock=None):
        created["remote"] = remote_factory(record.hour)
        record.state = "running"
        return record

    async def harvest_hour(spec, record, root, *, cloud, remote_factory, clock=None):
        record.state = "done"
        return record

    class Client:
        def __init__(self, token_source):
            self.ratelimit_remaining = None

        async def close(self):
            created["closed"] = True

    modules = {"launch": {"launch_hour": launch_hour}, "harvest": {"harvest_hour": harvest_hour},
               "fanout": {"OpenSSHRemote": lambda *args: args}}
    for name, values in modules.items():
        monkeypatch.setitem(sys.modules, f"proxy_index.campaign.{name}",
                            types.SimpleNamespace(**values))
    monkeypatch.setattr("proxy_index.campaign.do_api.DigitalOcean", Client)
    monkeypatch.setattr(collector_module, "ntp_synced", lambda: True)
    assert main(["campaign", "network", "pause", "geonode", "--root", str(root)]) == 0
    assert main(["campaign", "tick", "--root", str(root), "--slot", "0", "--now"]) == 0
    assert created["remote"][2] == 0 and created["closed"]
    assert main(["campaign", "collector", "--root", str(root), "--until-done"]) == 0
    assert main(["campaign", "watchdog", "--root", str(root), "--check", "fired"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [{"paused": ["geonode"]}, {"launched": [0]},
                     {"finished": False, "live_workers": 0}, {"alert": None}]


# Review findings S1-S7: each test failed at 34bdc8b.
def test_s1_the_armed_watchdog_accepts_a_launching_hour_with_armed_workers(tmp_path):
    root = make_root(tmp_path)
    clock = Clock(START + timedelta(seconds=30))
    record = load_hour(root, make_spec(), 0)
    record.state, record.fired_at = "launching", due_at(make_spec(), 0).isoformat()
    record.droplets = [worker("shifter", "lon1", 0, droplet_id="1", status="armed"),
                       worker("shifter", "nyc3", 0, droplet_id="2", status="booting")]
    save_hour(root, record)

    def check():
        return run_watchdog(root, check="armed", clock=clock, start_tick=lambda: None,
                            timer_enabled=lambda: True)

    assert check() is None and read_alerts(root) == []
    record.droplets[0].status = "booting"
    save_hour(root, record)
    assert check() == "missed_launch"  # launching with no worker armed yet


async def test_s2_launcher_and_harvester_alerts_keep_their_kind_and_name(tmp_path):
    root, clock = make_root(tmp_path, synthetic=True), Clock(START + timedelta(hours=1))
    name = f"pic-{SID}-h00-lon1-shifter"
    save_hour(root, HourRecord(CID, 0, START.isoformat(), state="running", alerts=[
        {"time": START.isoformat(), "level": "high", "kind": "auth_failed", "name": name},
        {"time": START.isoformat(), "level": "info", "kind": "round_long_3h", "name": name}]))

    async def harvest(spec, record, root):
        return record

    await collector_module.Collector(root, harvest, None, None, clock,
                                     Pacing(clock_synced=lambda: True,
                                            disk_usage=disk(50))).pass_once()
    mirrored = {(a["code"], a["level"], a.get("name")) for a in read_alerts(root)}
    assert mirrored == {("auth_failed", "high", name), ("round_long_3h", "info", name)}


async def test_s3_a_reconcile_error_never_stops_reports_or_status(tmp_path):
    root, clock = make_root(tmp_path), Clock(START + timedelta(hours=2))
    save_hour(root, HourRecord(CID, 0, START.isoformat(), state="done"))

    class Broken:
        ratelimit_remaining = 900

        async def list_tag(self, tag):
            raise ApiError("GET", "/v2/droplets", 503)

    async def harvest(spec, record, root):
        return record

    collector = collector_module.Collector(root, harvest, Broken(), None, clock,
                                           pacing(clock))
    for _ in range(3):
        status = await collector.pass_once()
        collector.reconciled_at = None
    assert (root / "reports/traffic/hour-00.md").exists() and status_path(root).exists()
    assert status["hours"][0]["state"] == "done"
    assert {(a["code"], a["level"]) for a in read_alerts(root)} == {
        ("reconcile_error", "info"), ("reconcile_error", "high")}


def test_s4_cumulative_gb_counts_every_hour_collected_so_far(tmp_path):
    root = make_root(tmp_path)
    late = START + timedelta(hours=2)
    save_hour(root, HourRecord(CID, 0, START.isoformat(), state="done", droplets=[
        worker("shifter", "lon1", 0, droplet_id="1", bytes_up=10**9, bytes_down=10**9,
               collected_at=late.isoformat())]))
    save_hour(root, HourRecord(CID, 1, (START + timedelta(hours=1)).isoformat(), state="done",
                               droplets=[worker("shifter", "lon1", 1, droplet_id="2",
                                                bytes_up=10**9, bytes_down=2 * 10**9,
                                                collected_at=late.isoformat())]))
    text = write_traffic_report(root, 0).read_text()
    assert "| shifter | 1.000 | 1.000 | 2.000 |" in text
    assert text.split("| shifter |")[1].splitlines()[0].endswith("| 5.000 |")


async def test_s5_failed_retires_are_retried_and_unreachable_orphans_keep_it_alive(tmp_path):
    root, clock = make_root(tmp_path), Clock(START + timedelta(hours=5))
    fake = FakeDigitalOcean()
    stuck = fake.add(f"pic-{SID}-h03-lon1-shifter", OURS, project=CAMPAIGN_PROJECT)
    lost = fake.add(f"pic-{SID}-h03-nyc3-shifter", OURS, project=CAMPAIGN_PROJECT)
    save_hour(root, HourRecord(CID, 3, (START + timedelta(hours=3)).isoformat(), state="done",
                               report_path="reports/traffic/hour-03.md", droplets=[
                                   worker("shifter", "lon1", 3, droplet_id=stuck,
                                          status="destroy_failed")]))
    remote = FakeRemote({f"pic-{SID}-h03-nyc3-shifter": OSError("ssh down")})

    async def harvest(spec, record, root):
        return record

    async with running(fake) as cloud:
        status = await collector_module.Collector(
            root, harvest, cloud, lambda hour: remote, clock, pacing(clock)).pass_once()
    assert fake.deleted == [("droplet", stuck)] and lost in fake.droplets
    assert load_hour(root, make_spec(), 3).droplets[0].status == "destroyed"
    assert status["live_workers"] == 1 and not status["finished"]


async def test_s6_an_adopted_active_orphan_gets_its_host_map_entry(tmp_path, monkeypatch):
    root, clock = make_root(tmp_path), Clock(START + timedelta(hours=5))
    hosts = install_host_map(monkeypatch)
    fake = FakeDigitalOcean()
    active = fake.add(f"pic-{SID}-h03-lon1-shifter", OURS, project=CAMPAIGN_PROJECT)
    save_hour(root, HourRecord(CID, 3, (START + timedelta(hours=3)).isoformat(), state="done",
                               report_path="reports/traffic/hour-03.md"))
    remote = FakeRemote({f"pic-{SID}-h03-lon1-shifter": {"assignment": {"kind": "round"}}})

    async def harvest(spec, record, root):
        return record

    async with running(fake) as cloud:
        await collector_module.Collector(root, harvest, cloud, lambda hour: remote, clock,
                                         pacing(clock)).pass_once()
    assert hosts.read_hosts(root, 3) == {active: "203.0.113.10"}
    assert not IPV4.search(hour_path(root, 3).read_text())


async def test_s7_the_timer_stays_while_any_slot_is_planned_or_launching(tmp_path):
    root, calls, disabled = make_root(tmp_path), [], []
    clock = Clock(START + timedelta(hours=23, minutes=5))
    assert await run_tick(root, launcher=fake_launcher(clock, calls), clock=clock,
                          now_mode=True, slot=23, disable_timer=lambda: disabled.append(1)) == [23]
    assert disabled == []
    inner = fake_launcher(clock, calls)

    async def fail_22(spec, record, root):
        if record.hour == 22:
            raise RuntimeError("crash")
        return await inner(spec, record, root)

    with pytest.raises(RuntimeError):
        await run_tick(root, launcher=fail_22, clock=clock,
                       disable_timer=lambda: disabled.append(1))
    assert load_hour(root, make_spec(), 22).state == "launching" and disabled == []
    await run_tick(root, launcher=inner, clock=clock, disable_timer=lambda: disabled.append(1))
    assert disabled == [1]


def test_s7_the_watchdog_alarms_when_the_timer_is_off_but_a_due_slot_is_planned(tmp_path):
    root, started = make_root(tmp_path), []
    clock = Clock(START + timedelta(hours=2, seconds=30))
    assert run_watchdog(root, check="armed", clock=clock, start_tick=lambda: started.append(1),
                        timer_enabled=lambda: False) == "missed_launch"
    assert started == []  # a disabled timer is the operator's; only the alarm is raised


async def test_m1_a_launcher_that_saved_its_record_is_not_overwritten(tmp_path):
    root, clock = make_root(tmp_path), Clock(due_at(make_spec(), 23))
    for slot in range(23):
        save_hour(root, HourRecord(CID, slot, START.isoformat(), state="done"))

    async def saving(spec, record, root):  # like CAMP-1c: saves, then the collector owns it
        record.state, record.launched_at = "running", "saved-by-launcher"
        save_hour(root, record)
        record.launched_at = "stale-copy"
        return record

    def refuse():
        raise tick_module.subprocess.CalledProcessError(1, "systemctl")

    assert await run_tick(root, launcher=saving, clock=clock, disable_timer=refuse) == [23]
    assert load_hour(root, make_spec(), 23).launched_at == "saved-by-launcher"
    assert [a["code"] for a in read_alerts(root)] == ["timer_disable_failed"]
