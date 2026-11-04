"""MANAGER-ORCH-1: the manager's size check, the orchestrator units and firewall rule (fakes
only, no live calls), HTTPS serving and the calculation as a subprocess."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import ssl
import sys
import tarfile
import time
from pathlib import Path

import aiohttp
import pytest
import trustme
from aiohttp import web
from campaign_deploy_fakes import (
    CID,
    MANAGER_IP,
    OTHER_IP,
    ROOT,
    Fake,
    fast_assign,
    inventory,
    make_job,
    parse,
    run_deploy,
    writes,
)
from campaign_deploy_fakes import Shell as DeployShell
from campaign_fakes import running

from proxy_index.campaign import cli as campaign_cli
from proxy_index.campaign import deploy, manager_daily, manager_orch, manager_rules, ops_api
from proxy_index.campaign.contracts import PULSE_TAG
from proxy_index.orchestrator import app as orchestrator_app
from proxy_index.orchestrator import cli as orchestrator_cli
from proxy_index.orchestrator import jobs
from proxy_index.orchestrator.db import Database, put_setting
from proxy_index.orchestrator.scheduler import Scheduler
from proxy_index.storage import seed

pytest_plugins = ["test_storage"]
WANTED = manager_orch.WANTED_SIZE
CERT_PEM = "-----BEGIN CERTIFICATE-----\nMIIBfakeCERTbody0123456789+/=\n-----END CERTIFICATE-----\n"


@pytest.fixture(autouse=True)
def quick_assign(monkeypatch):
    fast_assign(monkeypatch)


@pytest.fixture
def job(tmp_path):
    return make_job(tmp_path, account="proxy-index")


CHECK_OK = {"ok": True, "issues": [], "networks": ["shifter"], "droplets_by_hour": {"5": 1}}


def directives(unit):
    """A unit file's section and key lines, without its comments."""
    text = unit.decode() if isinstance(unit, bytes) else unit
    return [line for line in text.splitlines() if line.strip() and not line.startswith("#")]


def installable(unit):
    """True when anything could enable the unit: an [Install] section or an install key."""
    keys = ("WantedBy=", "RequiredBy=", "UpheldBy=", "Alias=", "Also=")
    return any(line == "[Install]" or line.startswith(keys) for line in directives(unit))


def staged(stdin):
    """{member name: bytes} of the enable script's stdin tar."""
    with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
        return {m.name: archive.extractfile(m).read() for m in archive}


class Shell:
    """Fake SSH: records argv and stdin; answers the enable script like the manager (the check
    line, each received formula's SHA-256, the certificate, `healthy`) and the hourly ones."""

    def __init__(self, healthy=True, check=CHECK_OK, tamper=False, timer="active"):
        self.calls, self.healthy, self.check = [], healthy, check
        self.tamper, self.timer = tamper, timer

    async def __call__(self, argv, stdin):
        self.calls.append((argv, stdin))
        command, check = argv[-1], f"daily-check {json.dumps(self.check, separators=(',', ':'))}"
        if "systemctl restart" in command:
            if not self.check["ok"]:
                return 4, check + "\n"
            sums = [
                f"formula {hashlib.sha256(data + (b'x' if self.tamper else b'')).hexdigest()} "
                + name.removeprefix("formulas/")
                for name, data in staged(stdin).items()
                if name.startswith("formulas/")
            ]
            tail = "healthy\n" if self.healthy else ""
            return 0, "\n".join([check, *sums, "Created symlink", CERT_PEM]) + tail
        if "add-wants" in command:
            return 0, f"{check}\n{self.timer}\n"
        if "is-active" in command:
            return 0, "inactive\n"
        return 0, ""

    def scripts(self):
        return [argv[-1] for argv, _ in self.calls]


def formula_dir(tmp_path):
    folder = tmp_path / "formulas"
    folder.mkdir(exist_ok=True)
    for network, schedule in {"shifter": {"GB": 5}, "geonode": {"DE": 6}}.items():
        (folder / f"{network}.json").write_text(
            json.dumps({"network": network, "schedule": schedule})
        )
    return folder


def go_inputs(tmp_path, **changes):
    """Inputs for an applied enable: the owner's go, the formulas and the template."""
    values = {"owner_go": owner_go(tmp_path), "formulas": formula_dir(tmp_path)}
    return manager_orch.EnableInputs(**(values | {"daily_template": CID} | changes))


async def deployed(job, size=WANTED, pulse_tag=True):
    """A deployed manager in the fake, live at `size`, with its size recorded as at creation."""
    fake = Fake()
    fake.sizes.append({"slug": WANTED, "available": True, "regions": ["lon1"]})
    await run_deploy(fake, job)
    deploy.save(job.inventory, inventory(job), campaigns=[CID])  # the daily template, uploaded
    fake.droplets[str(inventory(job)["manager_id"])]["size_slug"] = size
    if pulse_tag:
        fake.tags.add(PULSE_TAG)
    return fake


def manager_firewall(fake, job):
    return fake.resources["firewall"][inventory(job)["manager_firewall_id"]]


def owner_go(tmp_path):
    path = tmp_path / "owner-go.md"
    path.write_text("Go: switch the orchestrator to the manager.\n")
    return path


# --- units --------------------------------------------------------------------------------------


def test_the_units_are_rendered_from_the_assets():
    found = {name: data.decode() for name, data, mode in manager_orch.units() if mode == 0o644}
    assert sorted(found) == sorted(manager_orch.UNIT_FILES)
    service = found[manager_orch.ORCHESTRATOR_UNIT]
    assert "MemoryMax=12G" in service
    assert "Environment=PROXY_INDEX_CALCULATE_MEMORY_MAX=8G" in service
    assert f"EnvironmentFile={manager_orch.ENV_FILE}" in service
    assert f"Environment=DBIP_MMDB={manager_orch.DBIP_DIR}/dbip-full.mmdb" in service
    assert f"WorkingDirectory={deploy.BASE}" in service
    start = next(line for line in service.splitlines() if line.startswith("ExecStart="))
    assert start.split() == [
        f"ExecStart={deploy.BASE}/venv/bin/proxy-index",
        "orchestrator",
        "--host",
        "0.0.0.0",
        "--port",
        str(manager_orch.NODE_API_PORT),
        "--database-file",
        manager_orch.DATABASE_FILE,
        "--tls-cert",
        manager_orch.CERT,
        "--tls-key",
        manager_orch.KEY,
    ]
    hourly = found[manager_daily.DAILY_TIMER]
    assert "OnCalendar=*-*-* *:56:00 UTC" in hourly
    assert not installable(hourly)  # nothing can enable it
    daily = found[manager_daily.DAILY_SERVICE]
    assert not installable(daily) and "Type=oneshot" in daily
    assert installable(service)  # the orchestrator itself is enabled by orchestrator-enable
    assert '--unit=proxy-index-daily-$$(date -u -d "+10 min" +%%Y%%m%%dT%%H)' in daily
    assert (
        f"daily hour --formulas {manager_daily.FORMULAS} --template {manager_daily.TEMPLATE} "
        f"--database-file {manager_orch.DATABASE_FILE} --mmdb {manager_orch.DBIP_DIR}/"
        "dbip-full.mmdb --start next-hour'"
    ) in daily
    analyze = found["proxy-index-db-analyze.service"]
    assert "Type=oneshot" in analyze
    assert f"db-server analyze --database-file {manager_orch.DATABASE_FILE}" in analyze
    timer = found[manager_orch.ANALYZE_TIMER]
    assert "OnCalendar=*-*-* 03:47:00 UTC" in timer and "Persistent=true" in timer


def test_the_firewall_keeps_its_rules_and_adds_the_node_api_rule():
    recorded = {"manager_id": "42"}
    assert manager_orch.manager_rules(recorded, OTHER_IP) == ops_api.manager_firewall(
        OTHER_IP, "42"
    )
    opened = manager_orch.manager_rules(recorded | {"node_api_port": 8443}, OTHER_IP)
    assert opened["inbound_rules"] == [
        ops_api.ssh_from(OTHER_IP),
        {"protocol": "tcp", "ports": "8443", "sources": {"tags": [PULSE_TAG]}},
    ]
    assert opened["outbound_rules"] == ops_api.outbound_all()


def test_the_enable_script_reads_the_database_file_in_place():
    script = manager_orch.enable_script(MANAGER_IP, CID)
    assert f"subjectAltName=IP:{MANAGER_IP}" in script
    assert f"https://{MANAGER_IP}:8443/health" in script and "--connect-to ::127.0.0.1:" in script
    assert script.splitlines()[-2:] == [f"cat {manager_orch.CERT}", "echo healthy"]
    uses = [line for line in script.splitlines() if manager_orch.DATABASE_FILE in line]
    assert all(line.startswith("test ") for line in uses) and len(uses) == 2  # never read
    with pytest.raises(ValueError):
        manager_orch.enable_script("203.0.113.10; rm -rf /", CID)
    lines = script.splitlines()
    check = next(i for i, line in enumerate(lines) if "daily check" in line)
    swap = next(i for i, line in enumerate(lines) if "mv -Tf" in line)
    units = next(i for i, line in enumerate(lines) if "/etc/systemd/system/" in line)
    assert check < swap < units  # nothing is installed before the daily check passes
    assert f'--formulas "$stage/formulas" --template "{manager_daily.CAMPAIGNS}/{CID}"' in script
    assert manager_daily.DAILY_TIMER not in script and "add-wants" not in script


def test_the_hourly_timer_is_never_started_by_enable_or_deploy():
    assert manager_daily.DAILY_TIMER not in manager_orch.STARTED
    assert not any("daily" in name for name in deploy.UNIT_FILES)  # deploy installs no daily unit


# --- resize -------------------------------------------------------------------------------------


async def test_resize_records_the_live_size_and_its_dry_run_changes_nothing(job):
    fake = await deployed(job)
    before = inventory(job)
    assert before["manager_size"] == deploy.MANAGER_SIZE
    async with running(fake) as cloud:
        dry = await manager_orch.resize(cloud, job.inventory)
        assert dry == {
            "manager_size": WANTED,
            "recorded_size": deploy.MANAGER_SIZE,
            "applied": False,
            "action": "record",
        }
        assert inventory(job) == before
        writes_before = writes(fake)
        done = await manager_orch.resize(cloud, job.inventory, apply=True)
        assert done["action"] == "record" and inventory(job)["manager_size"] == WANTED
        again = await manager_orch.resize(cloud, job.inventory, apply=True)
    assert again["action"] == "none" and writes(fake) == writes_before  # no cloud write


@pytest.mark.parametrize("size", ["s-99vcpu-1gb", "c-2-unknown"])
async def test_resize_refuses_an_unknown_size(job, size):
    fake = await deployed(job)
    before = inventory(job)
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match=f"size {size} is not offered in lon1"):
            await manager_orch.resize(cloud, job.inventory, size, apply=True)
    assert inventory(job) == before


async def test_resize_never_resizes_a_manager_of_another_size(job):
    fake = await deployed(job, size="s-1vcpu-2gb")
    before = inventory(job)
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match="the manager is s-1vcpu-2gb, not"):
            await manager_orch.resize(cloud, job.inventory, apply=True)
        del fake.droplets[str(before["manager_id"])]
        with pytest.raises(deploy.DeployError, match="the recorded manager is gone"):
            await manager_orch.resize(cloud, job.inventory, apply=True)
    assert inventory(job) == before


async def test_steps_need_a_recorded_manager(tmp_path):
    async with running(Fake()) as cloud:
        for step in (manager_orch.resize, manager_orch.enable, manager_orch.disable):
            with pytest.raises(deploy.DeployError, match="no manager is recorded"):
                await step(cloud, tmp_path / "none.json")


# --- enable / disable ---------------------------------------------------------------------------


async def recorded_size(fake, job):
    async with running(fake) as cloud:
        await manager_orch.resize(cloud, job.inventory, apply=True)


async def test_enable_dry_run_changes_nothing(job, tmp_path):
    fake = await deployed(job)
    await recorded_size(fake, job)
    before, writes_before, shell = inventory(job), writes(fake), Shell()
    inputs = manager_orch.EnableInputs(formulas=formula_dir(tmp_path), daily_template=CID)
    async with running(fake) as cloud:
        result = await manager_orch.enable(cloud, job.inventory, inputs, shell=shell)
    assert result["applied"] is False and result["node_api_port"] == 8443
    assert result["firewall_inbound"][-1]["sources"] == {"tags": [PULSE_TAG]}
    assert sorted(result["units"]) == sorted(manager_orch.UNIT_FILES)
    assert result["daily"] == {"template": CID, "formulas": 2}
    assert result["started"] == list(manager_orch.STARTED)
    assert inventory(job) == before and writes(fake) == writes_before and shell.calls == []


async def test_enable_needs_the_size_the_owner_go_and_its_daily_inputs(job, tmp_path):
    fake = await deployed(job)
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match="record the manager's size first"):
            await manager_orch.enable(cloud, job.inventory, go_inputs(tmp_path), apply=True)
    await recorded_size(fake, job)
    empty = tmp_path / "empty.md"
    empty.write_text("\n")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "x.json").write_text('{"network": "x", "schedule": {"GB": 25}}')
    refusals = [
        ({"owner_go": None}, "--owner-go"),
        ({"owner_go": empty}, "--owner-go"),
        ({"owner_go": tmp_path / "missing.md"}, "--owner-go"),
        ({"daily_template": None}, "name the daily template once"),
        ({"daily_template": "not-an-id"}, "must be a campaign id"),
        ({"daily_template": "20270101t230000z-ffffffffffff"}, "never uploaded"),
        ({"formulas": bad}, "needs a test hour"),
        ({"formulas": tmp_path / "none"}, "no formula"),
    ]
    before, writes_before = inventory(job), writes(fake)
    async with running(fake) as cloud:
        for changes, message in refusals:
            with pytest.raises(deploy.DeployError, match=message):
                await manager_orch.enable(
                    cloud, job.inventory, go_inputs(tmp_path, **changes), apply=True
                )
    assert inventory(job) == before and writes(fake) == writes_before


async def test_enable_opens_the_port_installs_and_starts_the_units(job, tmp_path):
    fake = await deployed(job)
    await recorded_size(fake, job)
    shell, inputs = Shell(), go_inputs(tmp_path)
    async with running(fake) as cloud:
        result = await manager_orch.enable(cloud, job.inventory, inputs, apply=True, shell=shell)
    rules = manager_firewall(fake, job)
    assert rules["inbound_rules"][-1] == manager_rules.node_api_rule()
    assert rules["droplet_ids"] == [int(inventory(job)["manager_id"])]
    ((argv, stdin),) = shell.calls
    assert argv[-1] == manager_orch.enable_script(MANAGER_IP, CID)
    sent = staged(stdin)
    assert sent == {f"units/{name}": data for name, data, _ in manager_orch.units()} | {
        f"formulas/{p.name}": p.read_bytes() for p in inputs.formulas.glob("*.json")
    }
    saved = inventory(job)
    cert = job.inventory.with_name("campaign-manager.orchestrator.crt")
    assert cert.read_text() == CERT_PEM and result["cert"] == cert.name
    assert saved["node_api_port"] == 8443 and saved["daily_template"] == CID
    record = saved["orchestrator"]
    assert record["orchestrator_url"] == f"https://{MANAGER_IP}:8443"
    assert record["cert_sha256"] == result["cert_sha256"] and record["owner_go_sha256"]
    assert record["formulas_sha256"] == manager_daily.digests(
        manager_daily.formula_files(inputs.formulas)
    )
    assert record["droplets_by_hour"] == CHECK_OK["droplets_by_hour"]
    assert "hourly" not in saved  # the hourly timer stays off
    assert MANAGER_IP not in json.dumps(result)  # the address stays in the inventory
    # A rerun (after a deploy) takes the recorded template; allow-me keeps the node API rule.
    async with running(fake) as cloud:
        rerun = go_inputs(tmp_path, daily_template=None)
        await manager_orch.enable(cloud, job.inventory, rerun, apply=True, shell=shell)
        await deploy.allow_me(cloud, job.inventory, OTHER_IP)
    assert manager_firewall(fake, job)["inbound_rules"] == [
        ops_api.ssh_from(OTHER_IP),
        manager_rules.node_api_rule(),
    ]


async def test_enable_and_deploy_leave_the_hourly_timer_inactive(job, tmp_path):
    fake = await deployed(job)
    await recorded_size(fake, job)
    shell = Shell()
    async with running(fake) as cloud:
        await manager_orch.enable(
            cloud, job.inventory, go_inputs(tmp_path), apply=True, shell=shell
        )
    deploy_shell = DeployShell()
    await run_deploy(fake, job, shell=deploy_shell)
    for script in shell.scripts() + deploy_shell.commands():
        assert manager_daily.DAILY_TIMER not in script and "add-wants" not in script
        assert "proxy-index-daily.service" not in script
    for line in shell.scripts()[0].splitlines():
        if line.startswith("systemctl "):
            assert "daily" not in line
    timer = dict((name, data) for name, data, _ in manager_orch.units())[manager_daily.DAILY_TIMER]
    assert not installable(timer)  # `systemctl enable`, a preset or a reboot cannot start it
    (provision,) = [stdin for argv, stdin in deploy_shell.calls if "provision" in argv[-1]]
    assert not [name for name in staged(provision) if "daily" in name]  # deploy ships none
    assert "hourly" not in inventory(job)


async def test_enable_stops_when_the_manager_disagrees(job, tmp_path):
    fake = await deployed(job)
    await recorded_size(fake, job)
    failing = {"ok": False, "issues": ["hour 7: the template cannot test geonode"]}
    cases = [
        (Shell(healthy=False), "did not answer /health"),
        (Shell(check=failing), "the daily check failed: hour 7: the template cannot test geonode"),
        (Shell(tamper=True), "the formulas on the manager differ"),
    ]
    async with running(fake) as cloud:
        for shell, message in cases:
            with pytest.raises(deploy.DeployError, match=message):
                await manager_orch.enable(
                    cloud, job.inventory, go_inputs(tmp_path), apply=True, shell=shell
                )
    saved = inventory(job)
    assert "orchestrator" not in saved and "daily_template" not in saved
    assert not job.inventory.with_name("campaign-manager.orchestrator.crt").exists()


async def test_a_missing_pulse_tag_is_named_and_nothing_is_recorded(job, tmp_path):
    fake = await deployed(job, pulse_tag=False)
    await recorded_size(fake, job)
    shell = Shell()
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match=f"does the tag {PULSE_TAG} exist"):
            await manager_orch.enable(
                cloud, job.inventory, go_inputs(tmp_path), apply=True, shell=shell
            )
    assert "node_api_port" not in inventory(job) and shell.calls == []
    assert len(manager_firewall(fake, job)["inbound_rules"]) == 1


async def test_disable_stops_the_units_and_the_hourly_timer_then_closes_the_port(job, tmp_path):
    fake = await deployed(job)
    await recorded_size(fake, job)
    async with running(fake) as cloud:
        await manager_orch.enable(
            cloud, job.inventory, go_inputs(tmp_path), apply=True, shell=Shell()
        )
        shell, before = Shell(), inventory(job)
        dry = await manager_orch.disable(cloud, job.inventory, shell=shell)
        assert dry["firewall_inbound"] == [ops_api.ssh_from(before["mac_ipv4"])]
        assert dry["stopped"][-1] == manager_daily.DAILY_TIMER
        assert inventory(job) == before and shell.calls == []
        await manager_orch.disable(cloud, job.inventory, apply=True, shell=shell)
    assert shell.scripts()[0].splitlines() == [
        "set -eu",
        "systemctl disable --now proxy-index-orchestrator.service proxy-index-db-analyze.timer",
        f"systemctl stop {manager_daily.DAILY_TIMER} || true",
        f"rm -f {manager_daily.WANTS}",
        "systemctl daemon-reload",
    ]
    assert manager_firewall(fake, job)["inbound_rules"] == [ops_api.ssh_from(before["mac_ipv4"])]
    saved = inventory(job)
    assert "node_api_port" not in saved and saved["orchestrator_disabled_at"]


async def test_deploy_keeps_the_node_api_rule(job):
    fake = await deployed(job)
    deploy.save(job.inventory, inventory(job), node_api_port=8443)
    await run_deploy(fake, job)
    assert manager_firewall(fake, job)["inbound_rules"][-1] == manager_rules.node_api_rule()


# --- the hourly timer (owner rule: on only with the owner's go) --------------------------------


async def enabled(job, tmp_path, fake=None):
    fake = fake or await deployed(job)
    await recorded_size(fake, job)
    async with running(fake) as cloud:
        await manager_orch.enable(
            cloud, job.inventory, go_inputs(tmp_path), apply=True, shell=Shell()
        )


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
            with pytest.raises(deploy.DeployError, match="only on the proxy-index account"):
                await step(cloud)
    clouds = []
    monkeypatch.setattr(campaign_cli, "_cloud", lambda account: clouds.append(account))
    for action in ("resize", "orchestrator-disable"):
        args = parse(
            ["campaign", "manager", action, "--apply", "--inventory", str(other.inventory)]
        )
        with pytest.raises(deploy.DeployError, match="only on the proxy-index account"):
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
    assert accounts == ["proxy-index"] * 3
    assert inventory(job)["manager_size"] == WANTED
    with pytest.raises(deploy.DeployError, match="no manager is recorded"):
        await manager_orch.run(
            parse(["campaign", "manager", "resize", "--inventory", str(job.inventory) + "x"]),
            cloud_for,
        )


# --- HTTPS --------------------------------------------------------------------------------------


async def test_the_orchestrator_serves_https_with_its_certificate(tmp_path):
    ca = trustme.CA()
    issued = ca.issue_cert("127.0.0.1")
    cert, key = tmp_path / "server.crt", tmp_path / "server.key"
    issued.cert_chain_pems[0].write_to_path(cert)
    issued.private_key_pem.write_to_path(key)
    context = orchestrator_app.server_tls(str(cert), str(key))
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    app = web.Application()

    async def health(_request):
        return web.json_response({"ok": True})

    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    trusted = ssl.create_default_context()
    ca.configure_trust(trusted)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://127.0.0.1:{port}/health", ssl=trusted) as reply:
                assert await reply.json() == {"ok": True}
            with pytest.raises(aiohttp.ClientConnectorCertificateError):
                await session.get(f"https://127.0.0.1:{port}/health")  # an untrusted client
    finally:
        await runner.cleanup()


def test_tls_needs_both_files():
    assert orchestrator_app.server_tls(None, None) is None
    with pytest.raises(ValueError, match="go together"):
        orchestrator_app.server_tls("cert.pem", None)


def test_the_orchestrator_cli_passes_the_certificate(monkeypatch):
    import argparse

    seen = {}
    monkeypatch.setattr(orchestrator_app, "serve", lambda *a, **k: seen.update(k))
    parser = argparse.ArgumentParser()
    orchestrator_cli.register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        ["orchestrator", "--database-file", "db.toml", "--tls-cert", "c", "--tls-key", "k"]
    )
    assert orchestrator_cli.dispatch(args) == 0 and seen["tls"] == ("c", "k")


# --- calculation subprocess ---------------------------------------------------------------------


def python(code):
    return [sys.executable, "-c", code]


def test_the_calculation_command():
    plain = jobs.calculation_argv("db.toml", "m.toml")
    tail = ["calculate", "--database-file", "db.toml", "--methodology", "m.toml", "--publish"]
    assert plain[-6:] == tail
    scoped = jobs.calculation_argv("db.toml", "m.toml", "8G")
    assert scoped[:7] == [
        "systemd-run",
        "--scope",
        "--quiet",
        "--unit=proxy-index-calculate",
        "-p",
        "MemoryMax=8G",
        "--",
    ]
    assert scoped[7:] == plain
    for bad in ("8 G", "0", "8G; reboot", "-1G"):
        with pytest.raises(ValueError, match="PROXY_INDEX_CALCULATE_MEMORY_MAX"):
            jobs.calculation_argv("db.toml", "m.toml", bad)


async def test_a_calculation_records_its_snapshot():
    payload = {"snapshot_id": "s9", "as_of": "2026-11-04T10:05:00+00:00", "readiness": {}}
    result = await jobs.run_calculation(python(f"print({json.dumps(json.dumps(payload))})"), 30)
    assert result == {"snapshot_id": "s9", "as_of": payload["as_of"], "exit_code": 0}


@pytest.mark.parametrize(
    "code, message",
    [
        ("import sys; print('x', file=sys.stderr); sys.exit(3)", "exit 3: x"),
        ("import sys; sys.exit(2)", "exit 2"),
        ("print('not json')", "exit 0 without the JSON result"),
        ("print('{}')", "exit 0 without the JSON result"),
    ],
)
async def test_a_failed_calculation_raises_with_its_exit_code(code, message):
    with pytest.raises(jobs.CalculationFailed, match=f"^{message}"):
        await jobs.run_calculation(python(code), 30)


async def test_a_calculation_past_its_timeout_is_killed_with_its_children(tmp_path):
    pids = tmp_path / "pids"
    code = (
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')\n"
        "time.sleep(60)\n"
    )
    began = time.monotonic()
    with pytest.raises(jobs.CalculationFailed, match="timed out after 1 s"):
        await jobs.run_calculation(python(code), 1)
    assert time.monotonic() - began < 10
    for pid in map(int, pids.read_text().split()):
        for _ in range(50):
            if not _alive(pid):
                break
            await asyncio.sleep(0.1)
        assert not _alive(pid)


async def test_a_cancelled_calculation_is_killed_and_the_cancel_propagates(tmp_path):
    pids = tmp_path / "pids"
    code = (
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')\n"
        "time.sleep(60)\n"
    )
    task = asyncio.ensure_future(jobs.run_calculation(python(code), 60))
    for _ in range(100):
        if pids.exists() and len(pids.read_text().split()) == 2:
            break
        await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for pid in map(int, pids.read_text().split()):
        for _ in range(50):
            if not _alive(pid):
                break
            await asyncio.sleep(0.1)
        assert not _alive(pid)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    with open(f"/proc/{pid}/stat") as stat:  # a reaped-later zombie is not running
        return stat.read().split(")")[-1].split()[0] != "Z"


async def test_the_scheduler_records_the_calculation_outcome(database, monkeypatch):
    commands = iter([python("import sys; sys.exit(4)"), python("import time; time.sleep(30)")])
    monkeypatch.setattr(jobs, "calculation_argv", lambda *a: next(commands))
    db = Database(database)
    try:
        async with db.connection() as conn:
            await put_setting(conn, "calculate.trigger", "hourly")
            await put_setting(conn, "calculate.timeout_seconds", 1)
        scheduler = Scheduler(db)
        scheduler.add("calculate", lambda: jobs.calculate(db, database, "m.toml"), every_s=60)
        await scheduler.run_now("calculate")
        await scheduler.run_now("calculate")
        async with db.connection() as conn:
            rows = await (
                await conn.execute("SELECT status,error FROM job_runs ORDER BY id")
            ).fetchall()
    finally:
        await db.close()
    assert [(r["status"], r["error"]) for r in rows] == [
        ("failed", "CalculationFailed: exit 4"),
        ("failed", "CalculationFailed: timed out after 1 s (killed)"),
    ]


async def test_the_real_calculate_command_runs_as_a_subprocess(database):
    from proxy_index.calculated.store import initialize

    seed(database)
    initialize(database)
    argv = jobs.calculation_argv(database, jobs.METHODOLOGY)
    result = await jobs.run_calculation(argv, 240)
    assert result["exit_code"] == 0 and result["snapshot_id"]
