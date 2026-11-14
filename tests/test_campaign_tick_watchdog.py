"""CAMP-1d: the watchdog alarms, the default systemd hooks and the campaign commands.

Offline only: fake launchers, a fake clock and fake systemd hooks; the shared helpers are in
`campaign_tick_cases.py`.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from datetime import UTC, datetime, timedelta

from campaign_fakes import CID
from campaign_tick_cases import START, Clock, make_root, make_spec, watch_data, worker

from atlas.campaign import collector as collector_module
from atlas.campaign import tick as tick_module
from atlas.campaign.contracts import HourRecord
from atlas.campaign.tick import (
    due_at,
    load_hour,
    read_alerts,
    run_tick,
    run_watchdog,
    save_hour,
)
from atlas.cli import main


# 8: watchdog alarms.
def test_the_watchdog_raises_missed_launch_and_starts_the_tick(tmp_path):
    root, started = make_root(tmp_path), []
    clock = Clock(START + timedelta(seconds=30))

    def check(kind, enabled=True):
        return run_watchdog(
            root,
            check=kind,
            clock=clock,
            start_tick=lambda: started.append(1),
            timer_enabled=lambda: enabled,
        )

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


def test_the_default_systemd_hooks(tmp_path, monkeypatch):
    root, commands = make_root(tmp_path), []

    def run(command, **_kwargs):
        commands.append(command)
        return types.SimpleNamespace(stdout="enabled\n")

    monkeypatch.setattr(tick_module.subprocess, "run", run)
    clock = Clock(START + timedelta(seconds=30))
    assert run_watchdog(root, check="armed", clock=clock) == "missed_launch"
    unit = f"atlas-campaign-tick@{CID}"
    assert commands == [
        ["systemctl", "is-enabled", f"{unit}.timer"],
        ["systemctl", "start", "--no-block", f"{unit}.service"],
    ]
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

    modules = {
        "launch": {"launch_hour": launch_hour},
        "harvest": {"harvest_hour": harvest_hour},
        "fanout": {"OpenSSHRemote": lambda *args: args},
    }
    # Each fake keeps the real module's other names: parser setup imports probe and shot,
    # which import from these modules, and must work when no earlier test loaded them.
    for name, values in modules.items():
        real = importlib.import_module(f"atlas.campaign.{name}")
        monkeypatch.setitem(
            sys.modules, real.__name__, types.SimpleNamespace(**(vars(real) | values))
        )
    monkeypatch.setattr("atlas.campaign.do_api.DigitalOcean", Client)
    monkeypatch.setattr(collector_module, "ntp_synced", lambda: True)
    assert main(["campaign", "network", "pause", "geonode", "--root", str(root)]) == 0
    assert main(["campaign", "tick", "--root", str(root), "--slot", "0", "--now"]) == 0
    assert created["remote"][2] == 0 and created["closed"]
    assert main(["campaign", "collector", "--root", str(root), "--until-done"]) == 0
    assert main(["campaign", "watchdog", "--root", str(root), "--check", "fired"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [
        {"paused": ["geonode"]},
        {"launched": [0]},
        {"finished": False, "live_workers": 0},
        {"alert": None},
    ]
    inventory = tmp_path / "inventory.json"
    inventory.write_text("{}")
    monkeypatch.setattr(
        "atlas.campaign.watch.ssh_fetch",
        lambda *a: watch_data(datetime.now(UTC), finished=True),
    )
    monkeypatch.setattr("atlas.campaign.watch.notify", lambda _text: None)
    assert (
        main(
            [
                "campaign",
                "watch",
                "--inventory",
                str(inventory),
                "--status-file",
                str(tmp_path / "s.md"),
                "--traffic-dir",
                str(tmp_path / "t"),
                "--once",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "campaign",
                "watch",
                "--inventory",
                str(inventory),
                "--status-file",
                str(tmp_path / "s.md"),
                "--traffic-dir",
                str(tmp_path / "t"),
                "--ack",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"acked": 0}


# Review findings S1-S7: each test failed at 34bdc8b.
def test_s1_the_armed_watchdog_accepts_a_launching_hour_with_armed_workers(tmp_path):
    root = make_root(tmp_path)
    clock = Clock(START + timedelta(seconds=30))
    record = load_hour(root, make_spec(), 0)
    record.state, record.fired_at = "launching", due_at(make_spec(), 0).isoformat()
    record.droplets = [
        worker("shifter", "lon1", 0, droplet_id="1", status="armed"),
        worker("shifter", "nyc3", 0, droplet_id="2", status="booting"),
    ]
    save_hour(root, record)

    def check():
        return run_watchdog(
            root, check="armed", clock=clock, start_tick=lambda: None, timer_enabled=lambda: True
        )

    assert check() is None and read_alerts(root) == []
    record.droplets[0].status = "booting"
    save_hour(root, record)
    assert check() == "missed_launch"  # launching with no worker armed yet


def test_s7_the_watchdog_alarms_when_the_timer_is_off_but_a_due_slot_is_planned(tmp_path):
    root, started = make_root(tmp_path), []
    clock = Clock(START + timedelta(hours=2, seconds=30))
    assert (
        run_watchdog(
            root,
            check="armed",
            clock=clock,
            start_tick=lambda: started.append(1),
            timer_enabled=lambda: False,
        )
        == "missed_launch"
    )
    assert started == []  # a disabled timer is the operator's; only the alarm is raised
