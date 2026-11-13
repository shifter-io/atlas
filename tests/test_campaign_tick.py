"""CAMP-1d: launch decisions of the hourly tick: missed slots, the lock, pause and the timer.

Offline only: fake launchers and a fake clock; the shared helpers are in
`campaign_tick_cases.py`. The watchdog and the commands are in `test_campaign_tick_watchdog.py`.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import timedelta

import pytest
from campaign_fakes import CID
from campaign_tick_cases import START, Clock, fake_launcher, make_root, make_spec

from atlas.campaign import tick as tick_module
from atlas.campaign.contracts import HourRecord
from atlas.campaign.tick import (
    due_at,
    load_hour,
    read_alerts,
    run_tick,
    save_hour,
    set_paused,
)


# 1, 4: 24 launches at :58:30, once each, in order; the timer is disabled after slot 23.
async def test_every_slot_launches_at_5830_exactly_once_in_order(tmp_path):
    root, calls, disabled = make_root(tmp_path), [], []
    clock = Clock(START - timedelta(minutes=5))
    launcher = fake_launcher(clock, calls)
    assert await run_tick(root, launcher=launcher, clock=clock) == []
    for slot in range(24):
        clock.now = START + timedelta(hours=slot) - timedelta(seconds=90)
        assert clock.now.strftime("%M:%S") == "58:30"
        assert await run_tick(
            root, launcher=launcher, clock=clock, disable_timer=lambda: disabled.append(1)
        ) == [slot]
        clock.now += timedelta(seconds=10)
        assert (
            await run_tick(
                root, launcher=launcher, clock=clock, disable_timer=lambda: disabled.append(1)
            )
            == []
        )
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
        "h00",
        "h01",
        "h02",
        "h03",
    ]
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

    threads = [
        threading.Thread(target=lambda: asyncio.run(run_tick(root, launcher=slow, clock=clock)))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak[0] == 1 and len(calls) == 1


async def test_now_mode_launches_only_the_given_slot_and_keeps_the_timer(tmp_path):
    root, calls = make_root(tmp_path), []
    clock = Clock(START + timedelta(minutes=7))
    assert await run_tick(
        root,
        launcher=fake_launcher(clock, calls),
        clock=clock,
        now_mode=True,
        slot=0,
        disable_timer=lambda: 1 / 0,
    ) == [0]
    assert read_alerts(root) == []  # a dry run "now" is not a late launch
    assert (
        await run_tick(
            root, launcher=fake_launcher(clock, calls), clock=clock, now_mode=True, slot=0
        )
        == []
    )
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
        {"code": "network_paused", "level": "info", "networks": ["shifter"]}
    ]
    assert set_paused(root, "shifter", False) == []
    clock.now = due_at(make_spec(), 1)
    await run_tick(root, launcher=fake_launcher(clock, calls), clock=clock)
    assert calls[-1][2] == ("shifter", "geonode")
    set_paused(root, "shifter", True)
    set_paused(root, "geonode", True)
    clock.now = due_at(make_spec(), 2)
    await run_tick(root, launcher=fake_launcher(clock, calls), clock=clock)
    assert len(calls) == 2 and load_hour(root, make_spec(), 2).state == "done"
    assert [load_hour(root, make_spec(), k).paused_networks for k in range(3)] == [
        ["shifter"],
        [],
        ["geonode", "shifter"],
    ]
    with pytest.raises(ValueError):
        set_paused(root, "unknown", True)


async def test_a_resumed_slot_records_the_networks_it_launched_without(tmp_path):
    """CAMP-1p: merge reads `paused_networks`, so it names the overlay the launch used."""
    root, calls = make_root(tmp_path), []
    clock = Clock(due_at(make_spec(), 0))
    set_paused(root, "shifter", True)

    async def crash(spec, record, root):
        raise RuntimeError("tick killed mid-launch")

    with pytest.raises(RuntimeError):
        await run_tick(root, launcher=crash, clock=clock)
    assert load_hour(root, make_spec(), 0).paused_networks == ["shifter"]
    set_paused(root, "shifter", False)
    set_paused(root, "geonode", True)
    await run_tick(root, launcher=fake_launcher(clock, calls), clock=clock)
    assert calls == [(0, clock(), ("shifter",))]
    assert load_hour(root, make_spec(), 0).paused_networks == ["geonode"]


# Review findings S7 and M1: each test failed at 34bdc8b.
async def test_s7_the_timer_stays_while_any_slot_is_planned_or_launching(tmp_path):
    root, calls, disabled = make_root(tmp_path), [], []
    clock = Clock(START + timedelta(hours=23, minutes=5))
    assert await run_tick(
        root,
        launcher=fake_launcher(clock, calls),
        clock=clock,
        now_mode=True,
        slot=23,
        disable_timer=lambda: disabled.append(1),
    ) == [23]
    assert disabled == []
    inner = fake_launcher(clock, calls)

    async def fail_22(spec, record, root):
        if record.hour == 22:
            raise RuntimeError("crash")
        return await inner(spec, record, root)

    with pytest.raises(RuntimeError):
        await run_tick(
            root, launcher=fail_22, clock=clock, disable_timer=lambda: disabled.append(1)
        )
    assert load_hour(root, make_spec(), 22).state == "launching" and disabled == []
    await run_tick(root, launcher=inner, clock=clock, disable_timer=lambda: disabled.append(1))
    assert disabled == [1]


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
