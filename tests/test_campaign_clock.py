"""The `clock_unsynced` alert waits out the boot grace: NTP needs a moment after a reboot."""

from __future__ import annotations

from datetime import timedelta

import pytest
from campaign_tick_cases import START, Clock, disk, make_root

from proxy_index.campaign import collector as collector_module
from proxy_index.campaign.collector import Pacing
from proxy_index.campaign.tick import read_alerts

GRACE = 300  # collector.BOOT_GRACE_SECONDS, literal so the test also runs (and fails) on base


def collector(root, uptime):
    clock = Clock(START + timedelta(hours=12, minutes=1))
    pacing = Pacing(clock_synced=lambda: False, disk_usage=disk(50))
    pacing.uptime = uptime
    return collector_module.Collector(root, None, None, None, clock, pacing)


@pytest.mark.parametrize(
    ("uptime", "raised"),
    [
        (15.0, False),  # just booted: NTP has not synced yet
        (GRACE - 0.5, False),
        (float(GRACE), True),
        (600.0, True),
        (None, True),  # unknown uptime gets no grace
    ],
)
async def test_clock_unsynced_waits_out_the_boot_grace(tmp_path, uptime, raised):
    root = make_root(tmp_path, synthetic=True)
    status = await collector(root, lambda: uptime).pass_once()
    codes = [alert["code"] for alert in read_alerts(root)]
    assert codes == (["clock_unsynced"] if raised else [])
    assert status["clock_synced"] is False  # the recorded value ignores the grace


async def test_a_synced_clock_never_reads_the_uptime(tmp_path):
    root = make_root(tmp_path, synthetic=True)

    def unread():
        raise AssertionError("uptime read for a synced clock")

    instance = collector(root, unread)
    instance.pacing.clock_synced = lambda: True
    await instance.pass_once()
    assert read_alerts(root) == []


def test_boot_uptime_reads_proc_uptime(monkeypatch):
    assert collector_module.BOOT_GRACE_SECONDS == GRACE
    replies = iter(["15.27 40.10\n", "garbage\n", ""])
    monkeypatch.setattr(collector_module.Path, "read_text", lambda _self: next(replies))
    assert collector_module.boot_uptime() == 15.27
    assert collector_module.boot_uptime() is None
    assert collector_module.boot_uptime() is None

    def missing(_self):
        raise FileNotFoundError("/proc/uptime")

    monkeypatch.setattr(collector_module.Path, "read_text", missing)
    assert collector_module.boot_uptime() is None
