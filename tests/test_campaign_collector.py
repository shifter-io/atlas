"""CAMP-1d: the collector daemon's passes, its exit and its alerts.

Offline only: fake harvesters, a fake clock and fake clouds; the shared helpers are in
`campaign_tick_cases.py`. The orphan reconcile tests are in
`test_campaign_collector_orphans.py`.
"""

from __future__ import annotations

import types
from datetime import timedelta

from campaign_fakes import CID, SID
from campaign_tick_cases import (
    START,
    Clock,
    disk,
    finishing_harvester,
    make_root,
    make_spec,
    pacing,
    running_hour,
    worker,
)

from proxy_index.campaign import collector as collector_module
from proxy_index.campaign.collector import Pacing, run_collector, settled
from proxy_index.campaign.contracts import HourRecord, status_path
from proxy_index.campaign.do_api import ApiError
from proxy_index.campaign.tick import due_at, read_alerts, save_hour


# 5: the collector finalizes overlapping hours independently, then exits when done.
async def test_the_collector_finalizes_overlapping_hours_independently(tmp_path):
    root, clock = make_root(tmp_path), Clock(START + timedelta(minutes=70))
    running_hour(root, 0, clock)
    running_hour(root, 1, clock)
    seen = []

    async def recorded(spec, record, root):
        result = await harvest(spec, record, root)
        seen.append(
            (
                record.hour,
                (root / "reports/traffic/hour-01.md").exists(),
                (root / "reports/traffic/hour-00.md").exists(),
            )
        )
        return result

    harvest = finishing_harvester(clock, {0: 3, 1: 1})
    status = await run_collector(
        root,
        harvester=recorded,
        cloud=None,
        remote_factory=None,
        clock=clock,
        until_done=True,
        pacing=pacing(clock),
    )
    assert seen[:2] == [(0, False, False), (1, False, False)]
    assert seen[2] == (0, True, False)  # hour 1 was reported while hour 0 still ran
    assert settled(status) and not status["finished"]
    hours = {row["hour"]: row for row in status["hours"]}
    assert hours[0]["state"] == hours[1]["state"] == "done" and hours[2]["state"] == "planned"
    assert hours[0]["cells"] == {"complete": 3} and status["live_workers"] == 0
    assert status["traffic_latest"] == {
        "hour": 1,
        "path": "reports/traffic/hour-01.md",
        "total_bytes": 15 * 10**8,
        "top_network": "shifter",
        "top_bytes": 10**9,
    }
    assert status["next_launch_at"] == due_at(make_spec(), 2).isoformat()
    assert len((root / "reports/traffic.tsv").read_text().splitlines()) == 1 + 4


async def test_the_daemon_exits_only_when_every_hour_is_final(tmp_path):
    root, clock = make_root(tmp_path, synthetic=True), Clock(START + timedelta(hours=25))
    for hour in range(24):
        running_hour(root, hour, clock)
    status = await run_collector(
        root,
        harvester=finishing_harvester(clock, dict.fromkeys(range(24), 2)),
        cloud=None,
        remote_factory=None,
        clock=clock,
        pacing=pacing(clock),
    )
    assert status["finished"] and status["next_launch_at"] is None


async def test_collector_alerts_for_record_alerts_errors_and_the_manager_host(tmp_path):
    root = make_root(tmp_path, synthetic=True)
    clock = Clock(START + timedelta(hours=12, minutes=1))
    record = HourRecord(
        CID,
        0,
        START.isoformat(),
        state="running",
        alerts=[{"code": "auth_failed", "level": "high", "network": "shifter"}],
        droplets=[
            worker("shifter", "lon1", 0, droplet_id="1", status="running"),
            worker("shifter", "nyc3", 0, droplet_id=None, status="create_failed"),
        ],
    )
    save_hour(root, record)
    save_hour(root, HourRecord(CID, 1, START.isoformat(), state="running"))

    async def harvest(spec, record, root):
        if record.hour == 1:
            raise TimeoutError("ssh")
        return record

    cloud = types.SimpleNamespace(ratelimit_remaining=0)
    status = await collector_module.Collector(
        root,
        harvest,
        cloud,
        None,
        clock,
        Pacing(clock_synced=lambda: False, uptime=lambda: 600.0, disk_usage=disk(10)),
    ).pass_once()
    codes = {(a["code"], a["level"]) for a in read_alerts(root)}
    assert {
        ("auth_failed", "high"),
        ("create_failures", "high"),
        ("harvest_error", "info"),
        ("disk_low", "high"),
        ("clock_unsynced", "high"),
        ("ratelimit_low", "high"),
    } == codes
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


# Review findings S2 and S3: each test failed at 34bdc8b.
async def test_s2_launcher_and_harvester_alerts_keep_their_kind_and_name(tmp_path):
    root, clock = make_root(tmp_path, synthetic=True), Clock(START + timedelta(hours=1))
    name = f"pic-{SID}-h00-lon1-shifter"
    save_hour(
        root,
        HourRecord(
            CID,
            0,
            START.isoformat(),
            state="running",
            alerts=[
                {"time": START.isoformat(), "level": "high", "kind": "auth_failed", "name": name},
                {"time": START.isoformat(), "level": "info", "kind": "round_long_3h", "name": name},
            ],
        ),
    )

    async def harvest(spec, record, root):
        return record

    await collector_module.Collector(
        root, harvest, None, None, clock, Pacing(clock_synced=lambda: True, disk_usage=disk(50))
    ).pass_once()
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

    collector = collector_module.Collector(root, harvest, Broken(), None, clock, pacing(clock))
    for _ in range(3):
        status = await collector.pass_once()
        collector.reconciled_at = None
    assert (root / "reports/traffic/hour-00.md").exists() and status_path(root).exists()
    assert status["hours"][0]["state"] == "done"
    assert {(a["code"], a["level"]) for a in read_alerts(root)} == {
        ("reconcile_error", "info"),
        ("reconcile_error", "high"),
    }


async def test_r1_every_reconcile_outage_alerts_again(tmp_path):
    root, clock = make_root(tmp_path), Clock(START + timedelta(hours=2))
    failing = [True]

    class Flaky:
        ratelimit_remaining = 900

        async def list_tag(self, tag):
            if failing[0]:
                raise ApiError("GET", "/v2/droplets", 503)
            return []

        async def project_droplet_ids(self, project_id):
            return frozenset()

    async def harvest(spec, record, root):
        return record

    collector = collector_module.Collector(root, harvest, Flaky(), None, clock, pacing(clock))

    async def passes(count):
        for _ in range(count):
            await collector.pass_once()
            collector.reconciled_at = None
            clock.now += timedelta(minutes=5)

    await passes(3)
    failing[0] = False
    await passes(1)
    failing[0] = True
    clock.now += timedelta(hours=3)
    await passes(3)
    levels = [a["level"] for a in read_alerts(root) if a["code"] == "reconcile_error"]
    assert levels == ["info", "high"] * 2  # one of each per outage, not once per campaign
