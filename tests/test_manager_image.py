"""IMAGE-DAILY-1: the daily image timer on production's host manager, on only with the owner's
go, and its unit files (fake SSH; no live call)."""

from __future__ import annotations

import json
from argparse import Namespace

import pytest
from manager_orch_cases import directives, installable, owner_go

from atlas.campaign import deploy, manager_image, manager_orch
from atlas.campaign.host_layout import Layout


class Shell:
    """Fake SSH: records each script; the timer is `timer` after an enable, else inactive."""

    def __init__(self, timer="active"):
        self.scripts, self.timer = [], timer

    async def __call__(self, argv, stdin):
        self.scripts.append(argv[-1])
        return 0, f"{self.timer}\n" if "add-wants" in argv[-1] else "inactive\n"


def host(tmp_path, environment="production", **values):
    path = tmp_path / "secrets" / f"atlas-{environment}.json"
    path.parent.mkdir(exist_ok=True)
    raw = {
        "manager_kind": "host",
        "environment": environment,
        "host": "203.0.113.5",
        "ssh_user": "atlas",
        "ssh_alias": "atlas-server",
        "account": "atlas",
        "orchestrator": {"orchestrator_url": "https://m"},
        **values,
    }
    path.write_text(json.dumps(raw))
    return path


def saved(path):
    return json.loads(path.read_text())


async def test_enable_is_a_dry_run_without_the_owners_go(tmp_path):
    path, shell = host(tmp_path), Shell()
    dry = await manager_image.image_enable(path, shell=shell)
    assert dry == {"applied": False, "timer": "atlas-image.timer", "at": "05:45"}
    with pytest.raises(deploy.DeployError, match="--owner-go"):
        await manager_image.image_enable(path, apply=True, shell=shell)
    assert shell.scripts == [] and "image_daily" not in saved(path)


async def test_enable_checks_the_run_then_adds_the_user_timer(tmp_path):
    path, shell = host(tmp_path), Shell()
    go = owner_go(tmp_path)
    await manager_image.image_enable(path, apply=True, owner_go=go, shell=shell)
    script = shell.scripts[0].splitlines()
    assert "atlas campaign image-daily --root $HOME/atlas/production > /dev/null" in script[2]
    assert script[3] == "rm -f $HOME/.config/systemd/user/atlas-image.timer.d/at.conf"
    assert script[-3:] == [
        "systemctl --user add-wants timers.target atlas-image.timer",
        "systemctl --user start atlas-image.timer",
        "systemctl --user is-active atlas-image.timer",
    ]
    recorded = saved(path)["image_daily"]
    assert recorded["on"] is True and recorded["at_utc"] == "05:45"
    assert recorded["owner_go_sha256"]


async def test_another_start_time_is_a_drop_in(tmp_path):
    path, shell = host(tmp_path), Shell()
    await manager_image.image_enable(
        path, apply=True, owner_go=owner_go(tmp_path), at="06:10", shell=shell
    )
    script = shell.scripts[0]
    assert "mkdir -p $HOME/.config/systemd/user/atlas-image.timer.d" in script
    assert "OnCalendar=\\nOnCalendar=*-*-* 06:10:00 UTC\\n' > $HOME/" in script
    with pytest.raises(deploy.DeployError, match="HH:MM"):
        await manager_image.image_enable(path, at="6:10", shell=shell)


async def test_a_timer_that_does_not_start_is_an_error(tmp_path):
    path = host(tmp_path)
    with pytest.raises(deploy.DeployError, match="not active"):
        await manager_image.image_enable(
            path, apply=True, owner_go=owner_go(tmp_path), shell=Shell("failed")
        )
    assert "image_daily" not in saved(path)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"environment": "staging"}, "only on production's host manager"),
        ({"manager_kind": "droplet", "manager_id": "1"}, "only on production's host manager"),
        ({"orchestrator": None}, "orchestrator-enable"),
    ],
)
async def test_enable_refuses_other_managers(tmp_path, values, message):
    path = host(tmp_path, **values)
    with pytest.raises(deploy.DeployError, match=message):
        await manager_image.image_enable(path, shell=Shell())


async def test_disable_takes_the_timer_out(tmp_path):
    path, shell = host(tmp_path), Shell()
    dry = await manager_image.image_disable(path, shell=shell)
    assert dry == {"applied": False, "timer": "atlas-image.timer"} and shell.scripts == []
    await manager_image.image_disable(path, apply=True, shell=shell)
    wants = "rm -f $HOME/.config/systemd/user/timers.target.wants/atlas-image.timer"
    assert wants in shell.scripts[0]
    assert saved(path)["image_daily"]["on"] is False


async def test_disable_that_finds_the_timer_running_is_an_error(tmp_path):
    class Running(Shell):
        async def __call__(self, argv, stdin):
            return 0, "active\n"

    with pytest.raises(deploy.DeployError, match="did not stop"):
        await manager_image.image_disable(host(tmp_path), apply=True, shell=Running())


def test_the_units_are_installed_off_and_never_restart_a_failed_run():
    units = {n: d.decode() for n, d, _ in Layout("production").unit_files()}
    timer, service = units["atlas-image.timer"], units["atlas-image.service"]
    assert "OnCalendar=*-*-* 05:45:00 UTC" in directives(timer)
    assert not installable(timer) and not installable(service)
    assert (
        "ExecStart=%h/atlas/production/venv/bin/atlas campaign image-daily --apply "
        "--root %h/atlas/production --database-file %h/.config/atlas/production/database.toml"
    ) in service
    assert "RestartPreventExitStatus=2 3" in service and "TimeoutStartSec=6h" in service


async def test_the_manager_cli_runs_both_steps(tmp_path, monkeypatch):
    calls = []

    async def fake(path, **values):
        calls.append(sorted(values))
        return {}

    monkeypatch.setattr(manager_image, "image_enable", fake)
    monkeypatch.setattr(manager_image, "image_disable", fake)
    for action in manager_image.ACTIONS:
        args = Namespace(
            manager_action=action, inventory=tmp_path, apply=False, owner_go=None, at="05:45"
        )
        assert await manager_orch.run(args, None) == {}
    assert calls == [["apply", "at", "owner_go"], ["apply"]]
    assert set(manager_image.ACTIONS) <= set(manager_orch.ACTIONS)
