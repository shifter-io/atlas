"""CAMP-1n: the slot interval (`slot_minutes`) and slot count of a compressed campaign test.

One test hour with a slot every 15 minutes: 4 slots at :00, :15, :30 and :45, each pre-created
90 s before, overlapping rounds of consecutive slots, and a watchdog and late (Persistent)
timer that count 15-minute slots. Offline only; the hourly default is covered by the suites of
CAMP-1a-1g unchanged.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from campaign_fakes import CID, FakeDigitalOcean
from test_campaign_hour import NETWORKS, PRE_CREATE, REGIONS, launched
from test_campaign_hour import make_spec as hour_spec
from test_campaign_tick import START, Clock, fake_launcher, make_root, make_spec
from test_onboarding import ROOT

from proxy_index.campaign.contracts import HourRecord
from proxy_index.campaign.reports import write_traffic_report
from proxy_index.campaign.tick import (
    due_at,
    load_hour,
    read_alerts,
    run_tick,
    run_watchdog,
    save_hour,
    scheduled_at,
    slot_label,
)
from proxy_index.campaign.watch import report_label, report_message
from proxy_index.onboarding import manifest

QUARTER = timedelta(minutes=15)
COMPRESSED = {"slot_minutes": 15, "hours": 4}


def test_a_15_minute_spec_schedules_4_slots_with_the_lead_before_each():
    spec = make_spec(**COMPRESSED).validate()
    assert [f"{scheduled_at(spec, k):%H:%M:%S}" for k in range(4)] == [
        "22:00:00", "22:15:00", "22:30:00", "22:45:00"]
    assert [f"{due_at(spec, k):%H:%M:%S}" for k in range(4)] == [
        "21:58:30", "22:13:30", "22:28:30", "22:43:30"]
    assert [slot_label(spec, k) for k in (0, 3)] == ["slot 00 (22:00 UTC)", "slot 03 (22:45 UTC)"]
    assert slot_label(make_spec(), 3) == "hour 03" and slot_label(make_spec(), 3, True) == "h03"


async def test_4_slots_launch_90_s_before_each_quarter_exactly_once(tmp_path):
    root, calls, disabled = make_root(tmp_path, **COMPRESSED), [], []
    clock = Clock(START - timedelta(minutes=5))
    launcher = fake_launcher(clock, calls)
    spec = make_spec(**COMPRESSED)
    assert await run_tick(root, launcher=launcher, clock=clock) == []
    for slot in range(4):
        clock.now = START + slot * QUARTER - timedelta(seconds=90)
        assert await run_tick(root, launcher=launcher, clock=clock,
                              disable_timer=lambda: disabled.append(1)) == [slot]
        clock.now += timedelta(seconds=10)
        assert await run_tick(root, launcher=launcher, clock=clock,
                              disable_timer=lambda: disabled.append(1)) == []
        record = load_hour(root, spec, slot)
        assert record.launch_delay_seconds == 0
        assert record.scheduled_at == (START + slot * QUARTER).isoformat()
    assert [(slot, f"{at:%H:%M:%S}") for slot, at, _ in calls] == [
        (0, "21:58:30"), (1, "22:13:30"), (2, "22:28:30"), (3, "22:43:30")]
    assert disabled == [1, 1]  # after slot 3: one test hour, then the timer is gone
    clock.now = START + timedelta(hours=1, minutes=13, seconds=30)
    assert await run_tick(root, launcher=launcher, clock=clock,
                          disable_timer=lambda: None) == []
    assert read_alerts(root) == []


async def test_a_restarted_timer_launches_every_missed_quarter_with_its_delay(tmp_path):
    """B4 compressed: the timer stopped from 21:58 to 22:20; the watchdog counts 15-minute
    slots, and the Persistent timer's tick at 22:20 launches slots 0 and 1, nothing skipped."""
    root, calls, started = make_root(tmp_path, **COMPRESSED), [], []
    clock = Clock(START + timedelta(minutes=2))

    def watchdog(check):
        return run_watchdog(root, check=check, clock=clock, start_tick=lambda: started.append(1),
                            timer_enabled=lambda: True)

    assert watchdog("fired") == "missed_launch"
    clock.now = START + QUARTER + timedelta(seconds=30)
    assert watchdog("armed") == "missed_launch" and len(started) == 2
    assert [(a["key"], a["hour"]) for a in read_alerts(root) if a["code"] == "missed_launch"] == [
        ("h00-fired", 0), ("h01-armed", 1)]
    clock.now = START + timedelta(minutes=20)
    assert await run_tick(root, launcher=fake_launcher(clock, calls), clock=clock) == [0, 1]
    spec = make_spec(**COMPRESSED)
    assert [load_hour(root, spec, k).launch_delay_seconds for k in range(3)] == [
        1290.0, 390.0, None]
    assert [a["key"] for a in read_alerts(root) if a["code"] == "late_launch"] == ["h00", "h01"]
    clock.now = START + 2 * QUARTER + timedelta(seconds=30)
    assert watchdog("armed") == "missed_launch"  # slot 2 is due and still planned
    for record in (load_hour(root, spec, k) for k in range(4)):
        record.state, record.fired_at = "running", clock().isoformat()
        save_hour(root, record)
    for outside in (START + timedelta(hours=1, seconds=30), START - QUARTER):
        clock.now = outside
        assert watchdog("armed") is None  # no slot 4: the compressed test is one hour


async def test_a_slot_still_running_at_the_next_quarter_is_recorded_as_overlap(tmp_path):
    fake = FakeDigitalOcean()
    spec = hour_spec(NETWORKS[:1], REGIONS[:1], **COMPRESSED)
    first, *_ = await launched(tmp_path, fake, spec)
    assert first.droplets[0].overlap == 0
    assert first.droplets[0].requested_at == "2027-01-01T21:58:30.000Z"
    record = HourRecord(CID, 1, "2027-01-01T22:15:00Z")
    second, *_ = await launched(tmp_path, FakeDigitalOcean(), spec, start=PRE_CREATE + 900,
                                record=record)
    assert second.droplets[0].overlap == 1 and second.droplets[0].status == "armed"
    assert second.droplets[0].requested_at == "2027-01-01T22:13:30.000Z"
    assert first.droplets[0].status == "armed"


def test_a_15_minute_slot_report_names_its_slot_and_time(tmp_path):
    root = make_root(tmp_path, **COMPRESSED)
    save_hour(root, HourRecord(CID, 3, (START + 3 * QUARTER).isoformat(), state="failed"))
    path = write_traffic_report(root, 3)
    lines = path.read_text().splitlines()
    assert path.name == "hour-03.md"
    assert lines[0] == "# Campaign slot 03 (22:45 UTC) — traffic per network"
    assert lines[2].startswith(f"Campaign `{CID}`, slot scheduled 2027-01-01T22:45:00+00:00 "
                               "(a slot every 15 min), state failed.")


def _create(tmp_path, **options):
    return manifest.create(ROOT / "configs" / "shifter.toml", "residential",
                           manifest.timestamp(START), tmp_path, now=START - timedelta(hours=1),
                           **options)


def test_a_15_minute_manifest_spaces_its_24_rounds_15_minutes_apart(tmp_path):
    data = manifest.load(_create(tmp_path / "q", execution_mode="ephemeral", slot_minutes=15))
    assert data["slot_minutes"] == 15 and len(data["rounds"]) == 24
    assert data["rounds"][:5] == [manifest.timestamp(START + k * QUARTER) for k in range(5)]
    assert data["ends_at"] == manifest.timestamp(START + timedelta(hours=6 + 48))
    hourly = manifest.load(_create(tmp_path / "h", execution_mode="ephemeral"))
    assert "slot_minutes" not in hourly and hourly["rounds"][1] == manifest.timestamp(
        START + timedelta(hours=1))
    with pytest.raises(ValueError, match="Only ephemeral"):
        _create(tmp_path / "p", slot_minutes=15)
    for bad in (7, 60, 15.0, "15", 30):  # 30: the rounds and end are 15 minutes apart
        with pytest.raises(ValueError):
            manifest.validate(data | {"slot_minutes": bad})
    with pytest.raises(ValueError):
        manifest.validate(hourly | {"slot_minutes": 15})


async def test_a_manifest_with_another_slot_interval_is_refused_before_any_create(tmp_path):
    """m3: a hand-edited spec whose manifests are hourly fails before anything is created."""
    fake = FakeDigitalOcean()
    spec = hour_spec(NETWORKS[:1], REGIONS[:1], **COMPRESSED)
    hourly = hour_spec(NETWORKS[:1], REGIONS[:1])
    _, root, *_ = await launched(tmp_path, FakeDigitalOcean(), hourly)
    with pytest.raises(ValueError, match="slot_minutes differs"):
        await launched(tmp_path, fake, spec, record=HourRecord(CID, 1, "2027-01-01T22:15:00Z"))
    assert not [r for r in fake.requests if r[0] == "POST"]


def test_the_mac_watch_names_the_slot_and_its_time(tmp_path):
    """m4: the notification and Traffic-Latest name `slot NN (HH:MM UTC)`, not `hour NN`."""
    root = make_root(tmp_path, **COMPRESSED)
    save_hour(root, HourRecord(CID, 2, (START + 2 * QUARTER).isoformat(), state="failed"))
    text = write_traffic_report(root, 2).read_text()
    totals = {"02": {"shifter": 3 * 10**9}}
    assert report_label("hour-02.md", text) == "slot 02 (22:30 UTC)"
    assert report_message("hour-02.md", totals, text) == (
        "CAMP-1 slot 02 (22:30 UTC): 3.00 GB; top network shifter 3.00 GB")
    assert report_label("hour-02.md") == "hour 02"
    assert report_label("hour-02.md", "# Campaign hour 02 — traffic per network\n") == "hour 02"
