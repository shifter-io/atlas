"""PULSE-ONDEMAND-1: the on-demand pulses' hourly timer (on only with the owner's go), its
units and the orchestrator's switch to hourly on-demand pulses (fakes only, no live calls; the
database is the test's own)."""

from __future__ import annotations

import json
import subprocess
from argparse import Namespace

import pytest
from campaign_deploy_fakes import fast_assign, inventory, make_job
from manager_orch_cases import deployed, directives, installable, owner_go
from orchestrator_cases import prepare

from atlas.campaign import deploy, host_layout, manager_daily, manager_orch, manager_pulse
from atlas.campaign.host_layout import Layout
from atlas.cli import main
from atlas.daily.pulses import switch

pytest_plugins = ["test_storage"]
SWITCHED = '{"on_demand":true,"pack_minutes":40,"targets":{"generic_http":60}}'


@pytest.fixture(autouse=True)
def quick_assign(monkeypatch):
    fast_assign(monkeypatch)


@pytest.fixture
async def job(tmp_path):
    made = make_job(tmp_path, account="atlas")
    await deployed(made)
    return made


class Shell:
    """Fake SSH: records each script and answers as the manager would."""

    def __init__(self, code=0, timer="active"):
        self.scripts, self.code, self.timer = [], code, timer

    async def __call__(self, argv, stdin):
        self.scripts.append(argv[-1])
        if "add-wants" in argv[-1]:
            return self.code, f"{manager_pulse.SWITCH}{SWITCHED}\n{self.timer}\n"
        return 0, "inactive\n"


def orchestrated(job):
    deploy.save(job.inventory, inventory(job), orchestrator={"orchestrator_url": "https://m"})


async def test_pulse_enable_needs_the_orchestrator_and_the_owner_go(job, tmp_path):
    with pytest.raises(deploy.DeployError, match="run orchestrator-enable --apply first"):
        await manager_pulse.pulse_enable(job.inventory, apply=True, owner_go=owner_go(tmp_path))
    orchestrated(job)
    shell, before = Shell(), inventory(job)
    dry = await manager_pulse.pulse_enable(job.inventory, shell=shell)
    assert dry == {"applied": False, "timer": manager_pulse.PULSE_TIMER}
    with pytest.raises(deploy.DeployError, match="--owner-go"):
        await manager_pulse.pulse_enable(job.inventory, apply=True, shell=shell)
    assert shell.scripts == [] and inventory(job) == before


async def test_pulse_enable_checks_switches_then_adds_the_timer(job, tmp_path):
    orchestrated(job)
    shell = Shell()
    result = await manager_pulse.pulse_enable(
        job.inventory, apply=True, owner_go=owner_go(tmp_path), shell=shell
    )
    script, timer = shell.scripts[0].splitlines(), manager_pulse.PULSE_TIMER
    assert script[-3:] == [
        f"systemctl add-wants timers.target {timer}",
        f"systemctl start {timer}",
        f"systemctl is-active {timer}",
    ]
    plan = next(i for i, line in enumerate(script) if "daily pulse-plan" in line)
    on = next(i for i, line in enumerate(script) if "daily pulse-switch on" in line)
    assert script.index(f"test -f {manager_pulse.PULSE_ROOT}/fleet.json") < plan < on
    assert on < script.index(f"systemctl add-wants timers.target {timer}")
    assert result["switch"] == SWITCHED
    pulses = inventory(job)["pulses"]
    assert pulses["on"] is True and pulses["owner_go_sha256"]


async def test_pulse_enable_refuses_a_failed_step_or_an_inactive_timer(job, tmp_path):
    orchestrated(job)
    for shell, message in (
        (Shell(code=3), r"pulse-enable \(exit 3\)"),
        (Shell(timer="failed"), "the pulse timer is not active"),
    ):
        with pytest.raises(deploy.DeployError, match=message):
            await manager_pulse.pulse_enable(
                job.inventory, apply=True, owner_go=owner_go(tmp_path), shell=shell
            )
    assert "pulses" not in inventory(job)


@pytest.mark.parametrize("switch_code, reached", [(0, True), (1, False)])
def test_the_enable_script_stops_before_the_timer_when_the_switch_fails(
    tmp_path, switch_code, reached
):
    """The real script under sh, with stand-ins for atlas and systemctl that log their calls."""
    log, root = tmp_path / "calls.log", tmp_path / "pulse"
    root.mkdir()
    (root / "fleet.json").write_text("{}")
    stubs = {
        "atlas": f'echo "atlas $*" >> {log}; case "$2" in pulse-switch) exit {switch_code};; esac',
        "systemctl": f'echo "systemctl $*" >> {log}; if [ "$1" = is-active ]; then echo active; fi',
    }
    for name, body in stubs.items():
        stub = tmp_path / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    script = "\n".join(manager_pulse.enable_lines())
    script = script.replace(manager_daily.ATLAS, str(tmp_path / "atlas"))
    script = script.replace(manager_pulse.PULSE_ROOT, str(root))
    script = script.replace("systemctl ", f"{tmp_path / 'systemctl'} ")
    done = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
    calls = log.read_text()
    assert ("add-wants" in calls) is reached and (done.returncode == 0) is reached
    assert "atlas daily pulse-switch on" in calls


async def test_pulse_disable_takes_the_timer_out_and_it_stays_off(job):
    shell = Shell()
    assert (await manager_pulse.pulse_disable(job.inventory, shell=shell))["applied"] is False
    assert shell.scripts == []
    await manager_pulse.pulse_disable(job.inventory, apply=True, shell=shell)
    assert f"rm -f {manager_pulse.WANTS}" in shell.scripts[0]
    assert inventory(job)["pulses"]["on"] is False

    async def still_active(argv, stdin):
        return 0, "active\n"

    with pytest.raises(deploy.DeployError, match="did not stop"):
        await manager_pulse.pulse_disable(job.inventory, apply=True, shell=still_active)


async def test_the_manager_cli_runs_the_pulse_steps(job, tmp_path, monkeypatch):
    calls = []

    async def fake(path, **options):
        calls.append(sorted(options))
        return {}

    monkeypatch.setattr(manager_pulse, "pulse_enable", fake)
    monkeypatch.setattr(manager_pulse, "pulse_disable", fake)
    for action in ("pulse-enable", "pulse-disable"):
        args = Namespace(manager_action=action, inventory=job.inventory, apply=False, owner_go=None)
        assert await manager_orch.run(args, None) == {}
    assert calls == [["apply", "owner_go"], ["apply"]]
    assert set(manager_pulse.ACTIONS) <= set(manager_orch.ACTIONS)


def test_the_pulse_units_are_installed_off():
    found = {name: data.decode() for name, data, _ in manager_orch.units()}
    timer, service = found[manager_pulse.PULSE_TIMER], found[manager_pulse.PULSE_SERVICE]
    assert "OnCalendar=*-*-* *:57:00 UTC" in directives(timer)
    assert not installable(timer) and not installable(service)
    assert "daily pulse-hour --root /opt/atlas-campaign/pulse " in service
    assert '--unit=atlas-pulse-$$(date -u -d "+10 min" +%%Y%%m%%dT%%H)' in service


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_a_host_renders_the_pulse_units_and_translates_their_names(environment):
    layout = Layout(environment)
    units = dict((name, data.decode()) for name, data, _ in layout.unit_files())
    service = units[layout.unit("pulse.service")]
    assert f"--root %h/atlas/{environment}/pulse " in service and "@" not in service
    assert not installable(units[layout.unit("pulse.timer")])
    host = {"manager_kind": "host", "environment": environment}
    lines = host_layout.script(host, "\n".join(manager_pulse.off_lines()))
    assert layout.unit("pulse.timer") in lines and "atlas-pulse" not in lines


def test_switch_turns_on_demand_on_and_the_http_targets_hourly(database, capsys):
    prepare(database, ("DE",))
    assert switch(database, "status") == {
        "on_demand": False,
        "pack_minutes": 40,
        "targets": {"generic_http": 30, "generic_https": 30},
    }
    assert main(["daily", "pulse-switch", "on", "--database-file", str(database)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["on_demand"] is True
    assert printed["targets"] == {"generic_http": 60, "generic_https": 60}
    with pytest.raises(ValueError, match="on or status"):
        switch(database, "off")
