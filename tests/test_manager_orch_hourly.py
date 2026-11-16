"""MANAGER-ORCH-1: the hourly timer (on only with the owner's go), the account guard of every
manager step and the manager CLI steps (fakes only, no live calls; the fakes are in
manager_orch_cases.py, the size check, units and orchestrator steps in test_manager_orch.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
from campaign_deploy_fakes import CID, ROOT, fast_assign, inventory, make_job, parse
from campaign_fakes import running
from manager_orch_cases import (
    CHECK_OK,
    WANTED,
    Shell,
    deployed,
    enabled,
    formula_dir,
    go_inputs,
    owner_go,
)

from atlas.campaign import cli as campaign_cli
from atlas.campaign import deploy, manager_daily, manager_orch


@pytest.fixture(autouse=True)
def quick_assign(monkeypatch):
    fast_assign(monkeypatch)


@pytest.fixture
def job(tmp_path):
    return make_job(tmp_path, account="atlas")


# --- the hourly timer (owner rule: on only with the owner's go) --------------------------------


async def test_hourly_enable_needs_the_orchestrator_and_the_owner_go(job, tmp_path):
    fake = await deployed(job)
    with pytest.raises(deploy.DeployError, match="run orchestrator-enable --apply first"):
        await manager_daily.hourly_enable(job.inventory, apply=True, owner_go=owner_go(tmp_path))
    await enabled(job, tmp_path, fake)
    shell, before = Shell(), inventory(job)
    dry = await manager_daily.hourly_enable(job.inventory, shell=shell)
    assert dry == {"applied": False, "timer": manager_daily.DAILY_TIMER, "template": CID}
    with pytest.raises(deploy.DeployError, match="--owner-go"):
        await manager_daily.hourly_enable(job.inventory, apply=True, shell=shell)
    assert shell.calls == [] and inventory(job) == before


async def test_hourly_enable_checks_then_adds_the_timer_to_timers_target(job, tmp_path):
    await enabled(job, tmp_path)
    shell = Shell()
    result = await manager_daily.hourly_enable(
        job.inventory, apply=True, owner_go=owner_go(tmp_path), shell=shell
    )
    script = shell.scripts()[0].splitlines()
    timer = manager_daily.DAILY_TIMER
    assert script[-3:] == [
        f"systemctl add-wants timers.target {timer}",
        f"systemctl start {timer}",
        f"systemctl is-active {timer}",
    ]
    check = next(i for i, line in enumerate(script) if "daily check" in line)
    assert check < script.index(f"systemctl add-wants timers.target {timer}")
    assert result["droplets_by_hour"] == CHECK_OK["droplets_by_hour"]
    hourly = inventory(job)["hourly"]
    assert hourly["on"] is True and hourly["owner_go_sha256"]


async def test_hourly_enable_refuses_a_failed_check_or_an_inactive_timer(job, tmp_path):
    await enabled(job, tmp_path)
    failing = {"ok": False, "issues": ["no formula"]}
    for shell, message in (
        (Shell(check=failing), "the daily check failed: no formula"),
        (Shell(timer="failed"), "the hourly timer is not active"),
    ):
        with pytest.raises(deploy.DeployError, match=message):
            await manager_daily.hourly_enable(
                job.inventory, apply=True, owner_go=owner_go(tmp_path), shell=shell
            )
    assert "hourly" not in inventory(job)


async def test_hourly_disable_takes_the_timer_out_and_it_stays_off(job, tmp_path):
    await enabled(job, tmp_path)
    shell = Shell()
    assert (await manager_daily.hourly_disable(job.inventory, shell=shell))["applied"] is False
    assert shell.calls == []
    await manager_daily.hourly_disable(job.inventory, apply=True, shell=shell)
    script = shell.scripts()[0]
    assert f"rm -f {manager_daily.WANTS}" in script and "systemctl daemon-reload" in script
    assert inventory(job)["hourly"]["on"] is False

    async def still_active(argv, stdin):
        return 0, "active\n"

    with pytest.raises(deploy.DeployError, match="did not stop"):
        await manager_daily.hourly_disable(job.inventory, apply=True, shell=still_active)
    with pytest.raises(deploy.DeployError, match="no manager is recorded"):
        await manager_daily.hourly_disable(tmp_path / "none.json", apply=True)


def test_the_repository_formulas_install(tmp_path):
    found = manager_daily.formula_files(ROOT / "formulas")
    assert len(found) >= 18 and all(name.endswith(".json") for name in found)


async def test_an_inventory_of_another_account_is_refused_before_any_call(tmp_path, monkeypatch):
    other = make_job(tmp_path, account="leftclick")
    fake = await deployed(other)
    deploy.save(other.inventory, inventory(other), manager_size=WANTED, orchestrator={"x": 1})
    before, requests, shell = inventory(other), len(fake.requests), Shell()
    steps = [
        lambda cloud: manager_orch.resize(cloud, other.inventory, apply=True),
        lambda cloud: manager_orch.enable(cloud, other.inventory, go_inputs(tmp_path), apply=True),
        lambda cloud: manager_orch.disable(cloud, other.inventory, apply=True, shell=shell),
        lambda cloud: manager_daily.hourly_enable(
            other.inventory, apply=True, owner_go=owner_go(tmp_path), shell=shell
        ),
        lambda cloud: manager_daily.hourly_disable(other.inventory, apply=True, shell=shell),
    ]
    async with running(fake) as cloud:
        for step in steps:
            with pytest.raises(deploy.DeployError, match="only on the atlas account"):
                await step(cloud)
    clouds = []
    monkeypatch.setattr(campaign_cli, "_cloud", lambda account: clouds.append(account))
    for action in ("resize", "orchestrator-disable"):
        args = parse(
            ["campaign", "manager", action, "--apply", "--inventory", str(other.inventory)]
        )
        with pytest.raises(deploy.DeployError, match="only on the atlas account"):
            await campaign_cli._manager_step(args)
    assert len(fake.requests) == requests and shell.calls == [] and clouds == []
    assert inventory(other) == before


# --- CLI ----------------------------------------------------------------------------------------


def test_the_cli_parses_the_new_manager_steps(tmp_path):
    with pytest.raises(SystemExit):  # the inventory is always named
        parse(["campaign", "manager", "resize"])
    inv = ["--inventory", str(tmp_path / "pi.json")]
    args = parse(["campaign", "manager", "resize", *inv])
    assert (args.manager_action, args.size, args.apply) == ("resize", WANTED, False)
    go = tmp_path / "go.md"
    args = parse(
        ["campaign", "manager", "orchestrator-enable", "--owner-go", str(go), "--apply"]
        + ["--daily-template", CID, *inv]
    )
    assert (args.owner_go, args.apply, args.daily_template) == (go, True, CID)
    assert args.formulas == Path("formulas")
    with pytest.raises(SystemExit):  # SEC-MINI-KEYS-1: the manager makes no CA any more
        parse(["campaign", "manager", "orchestrator-enable", "--new-browser-ca", *inv])
    args = parse(["campaign", "manager", "orchestrator-disable", "--inventory", str(go)])
    assert (args.manager_action, args.inventory, args.apply) == ("orchestrator-disable", go, False)
    args = parse(["campaign", "manager", "hourly-enable", "--owner-go", str(go), *inv])
    assert (args.manager_action, args.owner_go, args.apply) == ("hourly-enable", go, False)
    args = parse(["campaign", "manager", "hourly-disable", "--apply", *inv])
    assert (args.manager_action, args.apply) == ("hourly-disable", True)


async def test_the_cli_runs_each_step_on_the_recorded_account(job, tmp_path, monkeypatch):
    fake = await deployed(job)
    accounts = []

    def cloud_for(account):
        accounts.append(account)
        return running(fake)

    monkeypatch.setattr(campaign_cli, "_cloud", cloud_for)
    folder = formula_dir(tmp_path)
    for argv in (
        ["resize", "--apply"],
        ["orchestrator-enable", "--formulas", str(folder), "--daily-template", CID],
        ["orchestrator-disable"],
        ["hourly-enable"],
        ["hourly-disable"],
    ):
        args = parse(["campaign", "manager", *argv, "--inventory", str(job.inventory)])
        if argv[0] == "hourly-enable":
            with pytest.raises(deploy.DeployError, match="orchestrator-enable --apply first"):
                await campaign_cli._manager_step(args)
            continue
        result = await campaign_cli._manager_step(args)
        assert result["applied"] is ("--apply" in argv)
    assert accounts == ["atlas"] * 3
    assert inventory(job)["manager_size"] == WANTED
    with pytest.raises(deploy.DeployError, match="no manager is recorded"):
        await manager_orch.run(
            parse(["campaign", "manager", "resize", "--inventory", str(job.inventory) + "x"]),
            cloud_for,
        )
