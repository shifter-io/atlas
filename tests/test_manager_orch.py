"""MANAGER-ORCH-1: the manager's size check, the orchestrator units and firewall rule and the
orchestrator enable and disable steps (fakes only, no live calls; the fakes are in
manager_orch_cases.py, the hourly timer and the manager CLI steps in test_manager_orch_hourly.py,
HTTPS serving and the calculation subprocess in test_manager_orch_serve.py)."""

from __future__ import annotations

import json
import re

import pytest
from campaign_deploy_fakes import (
    CID,
    MANAGER_IP,
    OTHER_IP,
    Fake,
    fast_assign,
    inventory,
    make_job,
    run_deploy,
    writes,
)
from campaign_deploy_fakes import Shell as DeployShell
from campaign_fakes import Fault, running
from manager_orch_cases import (
    CERT_PEM,
    CHECK_OK,
    WANTED,
    Shell,
    deployed,
    formula_dir,
    go_inputs,
    installable,
    manager_firewall,
    recorded_size,
    staged,
    starts_hourly,
)

from proxy_index.campaign import deploy, manager_daily, manager_orch, manager_rules, ops_api
from proxy_index.campaign.contracts import PULSE_TAG

pytest_plugins = ["test_storage"]


@pytest.fixture(autouse=True)
def quick_assign(monkeypatch):
    fast_assign(monkeypatch)


@pytest.fixture
def job(tmp_path):
    return make_job(tmp_path, account="proxy-index")


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
        "--browser-port",
        str(manager_rules.BROWSER_PORT),
        "--client-ca",
        manager_orch.BROWSER_CA,
    ]
    hourly = found[manager_daily.DAILY_TIMER]
    assert "OnCalendar=*-*-* *:56:00 UTC" in hourly
    assert not installable(hourly)  # nothing can enable it
    daily = found[manager_daily.DAILY_SERVICE]
    assert not installable(daily) and "Type=oneshot" in daily
    assert "--property=CollectMode=inactive-or-failed" in daily  # failed hours do not pile up
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
    # BROWSER-MTLS-1: the browser port is open to every address (the minis' change), and an
    # address list once recorded for them (BP-FIREWALL) is no longer a source of anything.
    both = recorded | {"node_api_port": 8443, "browser_port": 8444}
    browser = manager_orch.manager_rules(both | {"browser_host_ipv4s": ["8.8.8.8"]}, OTHER_IP)
    assert browser["inbound_rules"] == [
        *opened["inbound_rules"],
        {"protocol": "tcp", "ports": "8444", "sources": {"addresses": ["0.0.0.0/0", "::/0"]}},
    ]
    staging = manager_orch.manager_rules(both | {"environment": "staging"}, OTHER_IP)
    assert staging["inbound_rules"] == browser["inbound_rules"]
    assert staging["name"] != browser["name"]


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
    assert starts_hourly(script) == [] and "add-wants" not in script
    timer = manager_daily.DAILY_TIMER
    assert starts_hourly(f"systemctl start {timer}\nsystemctl is-active {timer}") == [
        f"systemctl start {timer}"
    ]  # the check itself sees a start


def test_the_enable_script_makes_the_browser_ca_once_before_the_restart():
    lines = manager_orch.enable_script(MANAGER_IP, CID).splitlines()
    make = next(i for i, line in enumerate(lines) if "/CN=proxy-index-browser-ca" in line)
    assert lines[make - 1].startswith(f"if [ ! -s {manager_orch.BROWSER_CA_KEY} ]")
    assert "basicConstraints=critical,CA:TRUE" in lines[make]
    assert make < next(i for i, line in enumerate(lines) if "systemctl restart" in line)
    assert not any(line.startswith("rm -f") for line in lines)  # kept across reruns
    fresh = manager_orch.enable_script(MANAGER_IP, CID, new_ca=True).splitlines()
    assert f"rm -f {manager_orch.BROWSER_CA} {manager_orch.BROWSER_CA_KEY}" in fresh
    assert fresh.index(f"rm -f {manager_orch.BROWSER_CA} {manager_orch.BROWSER_CA_KEY}") < make


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


async def test_enable_dry_run_changes_nothing(job, tmp_path):
    fake = await deployed(job)
    await recorded_size(fake, job)
    before, writes_before, shell = inventory(job), writes(fake), Shell()
    inputs = manager_orch.EnableInputs(formulas=formula_dir(tmp_path), daily_template=CID)
    async with running(fake) as cloud:
        result = await manager_orch.enable(cloud, job.inventory, inputs, shell=shell)
    assert result["applied"] is False and result["node_api_port"] == 8443
    assert result["browser_port"] == 8444 and result["new_browser_ca"] is False
    assert result["firewall_inbound"][-2:] == [
        manager_rules.node_api_rule(),
        manager_rules.browser_rule(),
    ]
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
    browser = [manager_rules.node_api_rule(), manager_rules.browser_rule()]
    assert rules["inbound_rules"][-2:] == browser
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
    assert saved["browser_port"] == 8444
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
        manager_rules.browser_rule(),
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
        assert starts_hourly(script) == [] and "add-wants" not in script
    assert not any("proxy-index-daily" in script for script in deploy_shell.commands())
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
        (Shell(busy=True), "the hourly daily test is on or an hour is running"),
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


async def test_enable_never_swaps_the_formulas_under_the_hourly_test(job, tmp_path):
    fake = await deployed(job)
    await recorded_size(fake, job)
    deploy.save(job.inventory, inventory(job), hourly={"on": True})
    before, writes_before, shell = inventory(job), writes(fake), Shell()
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match="hourly-disable --apply first"):
            await manager_orch.enable(
                cloud, job.inventory, go_inputs(tmp_path), apply=True, shell=shell
            )
    assert inventory(job) == before and writes(fake) == writes_before and shell.calls == []
    lines = manager_orch.enable_script(MANAGER_IP, CID).splitlines()
    guard = next(i for i, line in enumerate(lines) if "--state=active" in line)
    assert manager_daily.DAILY_TIMER in lines[guard] and "'proxy-index-daily-*'" in lines[guard]
    assert guard < next(i for i, line in enumerate(lines) if "mv -Tf" in line)


async def test_a_missing_pulse_tag_is_created_empty_and_a_refusal_names_the_error(job, tmp_path):
    """MANAGER-TAG-1: enable creates the empty pi-pulse-node tag; a refused rule names why."""
    fake = await deployed(job, pulse_tag=False)
    await recorded_size(fake, job)
    path = f"/v2/firewalls/{inventory(job)['manager_firewall_id']}"
    fake.faults.append(Fault("PUT", path, 422))
    shell = Shell()
    async with running(fake) as cloud:
        refused = re.escape(f"DigitalOcean refused the firewall rules: PUT {path} failed (422)")
        with pytest.raises(deploy.DeployError, match=refused):
            await manager_orch.enable(
                cloud, job.inventory, go_inputs(tmp_path), apply=True, shell=shell
            )
    assert "node_api_port" not in inventory(job) and shell.calls == []
    assert "browser_port" not in inventory(job)
    assert len(manager_firewall(fake, job)["inbound_rules"]) == 1
    async with running(fake) as cloud:
        inputs = go_inputs(tmp_path)
        await manager_orch.enable(cloud, job.inventory, inputs, apply=True, shell=shell)
    assert manager_firewall(fake, job)["inbound_rules"][-2] == manager_rules.node_api_rule()
    assert PULSE_TAG in fake.tags  # created, and put on no droplet
    assert not any(PULSE_TAG in droplet["tags"] for droplet in fake.droplets.values())


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
    assert "browser_port" not in saved
    assert saved["hourly"] == {"on": False, "at": saved["orchestrator_disabled_at"]}


async def test_deploy_keeps_the_node_api_rule(job):
    fake = await deployed(job)
    deploy.save(job.inventory, inventory(job), node_api_port=8443)
    await run_deploy(fake, job)
    assert manager_firewall(fake, job)["inbound_rules"][-1] == manager_rules.node_api_rule()
