"""CAMP-1g: campaign plan, manager deploy, enable, allow-me and teardown (fakes only)."""

from __future__ import annotations

import asyncio
import configparser
import io
import json
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import tomllib
from datetime import timedelta
from pathlib import Path

import pytest
from campaign_deploy_fakes import (
    ASSETS,
    CID,
    CREDENTIALS,
    IMAGE,
    MAC_IP,
    MAC_PUBLIC,
    MANAGER_IP,
    NOW,
    OTHER_IP,
    PLAN_CREDENTIALS,
    ROOT,
    WORKER_PUBLIC,
    Fake,
    Shell,
    fast_assign,
    inventory,
    make_job,
    other_team,
    parse,
    run_deploy,
    writes,
)
from campaign_fakes import CAMPAIGN_PROJECT, TOKEN, FakeDigitalOcean, Fault, running

from proxy_index.campaign import cli as campaign_cli
from proxy_index.campaign import deploy, ops_api, planspec
from proxy_index.campaign.contracts import (
    MANAGER_TAG,
    WORKER_TAG,
    CampaignSpec,
    spec_path,
)
from proxy_index.campaign.do_api import ApiError, CreateError
from proxy_index.campaign.planspec import PlanError, PlanInputs
from proxy_index.models import RequestContext
from proxy_index.onboarding import manifest
from proxy_index.onboarding.files import digest
from proxy_index.providers import load_provider
from proxy_index.regions import REGION_IDS


@pytest.fixture(autouse=True)
def quick_assign(monkeypatch):
    fast_assign(monkeypatch)


@pytest.fixture
def job(tmp_path):
    return make_job(tmp_path)


async def test_deploy_creates_a_hardened_manager_in_order(job):
    fake, shell = Fake(), Shell()
    result = await run_deploy(fake, job, shell)
    assert writes(fake) == [
        ("POST", "/v2/account/keys"), ("POST", "/v2/droplets"),
        ("POST", "/v2/projects/N/resources"), ("POST", "/v2/firewalls"),
        ("POST", "/v2/account/keys"), ("POST", "/v2/tags"), ("POST", "/v2/firewalls")]
    [create] = [b for _, p, b in fake.bodies if p == "/v2/droplets"]
    assert create["tags"] == [MANAGER_TAG] and re.fullmatch(
        r"pi-camp-manager-20270101-[0-9a-f]{4}", create["names"][0])
    assert (create["region"], create["size"], create["image"]) == (
        "lon1", "s-8vcpu-16gb", "ubuntu-24-04-x64")
    assert create["with_droplet_agent"] is False and create["monitoring"] is False
    mac_key = next(k for k in fake.resources["ssh_key"].values() if k["name"].startswith(
        "pi-camp-mac-"))
    assert create["ssh_keys"] == [str(mac_key["id"])]
    saved = inventory(job)
    assert saved["manager_id"] in fake.projects[CAMPAIGN_PROJECT]
    assert (saved["host"], saved["user"], saved["campaigns_root"]) == (
        MANAGER_IP, "root", "/opt/proxy-index-campaign/campaigns")
    assert saved["identity"] == saved["identity_file"] == str(job.mac_key)
    assert saved["known_hosts"].endswith("campaign-manager.known_hosts")
    assert saved["worker_key_name"] == "pi-camp-worker-20270101"
    assert saved["wheel_sha256"] == result["wheel_sha256"] and saved["framework_version"] == "0.9.5"
    assert stat.S_IMODE(job.inventory.stat().st_mode) == 0o600
    assert not re.search(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b", json.dumps(result))
    worker_key = fake.resources["ssh_key"][saved["worker_key_id"]]
    assert worker_key["public_key"] == WORKER_PUBLIC
    commands = shell.commands()
    assert commands[0] == "true" and commands[1].endswith("manager-provision.sh")
    assert [c.split("&&")[1].strip() for c in commands[2:]] == [
        "cat > /opt/proxy-index-campaign/.secrets/do-token.partial",
        "cat > /opt/proxy-index-campaign/.secrets/credentials/shifter.toml.partial"]
    argv = shell.calls[0][0]
    assert "BatchMode=yes" in argv and argv[argv.index("-i") + 1] == str(job.mac_key)
    assert f"UserKnownHostsFile={saved['known_hosts']}" in argv and argv[-2] == f"root@{MANAGER_IP}"


async def test_the_token_travels_only_on_stdin(job, tmp_path, capsys, caplog):
    shell = Shell()
    result = await run_deploy(Fake(), job, shell)
    assert all(TOKEN not in " ".join(argv) for argv, _ in shell.calls)
    assert [stdin for _, stdin in shell.calls if TOKEN.encode() in stdin] == [TOKEN.encode()]
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path
    assert TOKEN not in json.dumps(result) + capsys.readouterr().out + caplog.text
    secret = "customer-a"
    assert all(secret not in " ".join(argv) for argv, _ in shell.calls)
    assert secret not in job.inventory.read_text()


async def test_the_provision_payload(job):
    shell = Shell()
    await run_deploy(Fake(), job, shell)
    archive = tarfile.open(fileobj=io.BytesIO(shell.calls[1][1]))
    names = archive.getnames()
    assert names[:4] == ["manager-provision.sh", "requirements.txt", job.wheel.name, "mac.pub"]
    assert sorted(names[4:]) == sorted(f"units/{u}" for u in deploy.UNIT_FILES)
    assert archive.extractfile("mac.pub").read().decode().strip() == MAC_PUBLIC


def test_the_provision_script_hardens_ssh():
    script = (ASSETS / "manager-provision.sh").read_text()
    for line in ("PasswordAuthentication no", "KbdInteractiveAuthentication no",
                 "PermitRootLogin prohibit-password", "AuthenticationMethods publickey",
                 'Unattended-Upgrade::Automatic-Reboot "false";'):
        assert line in script
    assert "/root/.ssh/authorized_keys" in script and "passwd -l root" in script
    assert "--require-hashes" in script and "purge -y -q droplet-agent" in script
    assert subprocess.run(["bash", "-n", str(ASSETS / "manager-provision.sh")]).returncode == 0
    assert not re.search(r"BEGIN [A-Z ]*PRIVATE|password\s*=", script, re.IGNORECASE)


async def test_the_firewall_rules_are_exact(job):
    fake = Fake()
    await run_deploy(fake, job)
    saved = inventory(job)
    rules = {f["name"]: f for f in fake.resources["firewall"].values()}
    ssh = {"protocol": "tcp", "ports": "22"}
    manager = rules["pi-camp-manager-fw"]
    assert manager["inbound_rules"] == [{**ssh, "sources": {"addresses": [f"{MAC_IP}/32"]}}]
    assert manager["droplet_ids"] == [int(saved["manager_id"])] and manager["tags"] == []
    worker = rules["pi-camp-worker-fw"]
    assert worker["inbound_rules"] == [{**ssh, "sources": {"addresses": [f"{MANAGER_IP}/32"]}}]
    assert worker["tags"] == [WORKER_TAG] and worker["droplet_ids"] == []
    assert [r["protocol"] for r in worker["outbound_rules"]] == ["tcp", "udp", "icmp"]


async def test_a_second_deploy_updates_without_a_second_manager(job):
    fake, shell = Fake(), Shell()
    await run_deploy(fake, job)
    first = inventory(job)
    await run_deploy(fake, job, shell)
    again = inventory(job)
    assert [m for m, p in writes(fake)].count("POST") == 7 + 0 and ("PUT", "/v2/firewalls/N") in (
        writes(fake))
    assert len([d for d in fake.droplets.values() if MANAGER_TAG in d["tags"]]) == 1
    for key in ("manager_id", "manager_firewall_id", "worker_firewall_id", "worker_key_id",
                "worker_key_name"):
        assert again[key] == first[key]


async def test_a_missing_worker_tag_is_created_before_its_firewall(job):
    """CAMP-1h: the worker firewall targets pi-camp-worker, which no droplet carried yet, and
    DigitalOcean answers 422 to a firewall naming a tag that does not exist."""
    fake = Fake()
    fake.seed_production(40)
    production = {i: list(d["tags"]) for i, d in fake.droplets.items()}
    assert WORKER_TAG not in fake.tags
    await run_deploy(fake, job)
    assert [(m, p, b) for m, p, b in fake.bodies if "/tags" in p] == [
        ("POST", "/v2/tags", {"name": WORKER_TAG})]
    order = writes(fake)
    assert order[-2:] == [("POST", "/v2/tags"), ("POST", "/v2/firewalls")]
    assert inventory(job)["worker_firewall_id"] in fake.resources["firewall"]
    assert {i: fake.droplets[i]["tags"] for i in production} == production
    assert not fake.unlisted and not fake.deleted


async def test_a_deploy_stopped_before_the_worker_firewall_continues_on_a_rerun(job):
    """CAMP-1h: a rerun after the first run stopped at the worker tag finishes the deploy with
    the same manager, manager firewall and worker key. m1: the rerun runs on a later day and
    records the reused key's registered name, which `plan` looks the key up by."""
    fake = Fake()
    fake.faults.append(Fault("POST", "/v2/tags", 422))
    with pytest.raises(ApiError):
        await run_deploy(fake, job)
    partial = inventory(job)
    assert "worker_firewall_id" not in partial and partial["manager_firewall_id"]
    await run_deploy(fake, job, now=NOW + timedelta(days=1))
    again = inventory(job)
    for key in ("manager_id", "manager_firewall_id"):
        assert again[key] == partial[key]
    assert again["worker_firewall_id"] in fake.resources["firewall"]
    assert len(fake.resources["firewall"]) == 2 and WORKER_TAG in fake.tags
    assert len([d for d in fake.droplets.values() if MANAGER_TAG in d["tags"]]) == 1
    [worker_key] = [k for k in fake.resources["ssh_key"].values()
                    if k["name"].startswith("pi-camp-worker-")]
    assert again["worker_key_name"] == worker_key["name"] == "pi-camp-worker-20270101"
    assert str(worker_key["id"]) == again["worker_key_id"]


async def test_a_deploy_never_runs_under_a_campaign_and_keeps_earlier_credentials(job):
    from dataclasses import replace

    fake = Fake()
    await run_deploy(fake, job)
    deploy.save(job.inventory, inventory(job), campaigns=[CID])
    before = job.inventory.read_bytes()

    class Running(Shell):
        async def __call__(self, argv, stdin):
            self.calls.append((argv, stdin))
            return 0, f"active proxy-index-campaign-tick@{CID}.timer\n"

    shell = Running()
    with pytest.raises(deploy.DeployError, match="units are running"):  # m4
        await run_deploy(fake, job, shell)
    assert job.inventory.read_bytes() == before and len(shell.calls) == 1
    assert "is-active" in shell.calls[0][0][-1]
    await run_deploy(fake, replace(job, credentials={"geonode": b"[geonode]\n"}))
    assert inventory(job)["credentials"] == ["geonode", "shifter"]


class UnitShell(Shell):
    """Answers the running-units check per unit, as `systemctl is-active` would."""

    def __init__(self, active):
        super().__init__()
        self.active = active

    async def __call__(self, argv, stdin):
        command = argv[-1]
        if "is-active" not in command:
            return await super().__call__(argv, stdin)
        self.calls.append((argv, stdin))
        checked = command.split(" in ", 1)[1].split(";", 1)[0].split()
        return 0, "".join(f"active {u}\n" for u in checked if u in self.active)


async def test_a_finished_campaign_with_active_watchdogs_does_not_block_a_deploy(job):
    """n1: the watchdog timers stay active after the last hour; only tick and collector count."""
    fake = Fake()
    await run_deploy(fake, job)
    deploy.save(job.inventory, inventory(job), campaigns=[CID])
    watchdogs = {u for u in deploy.unit_names(CID) if "watchdog" in u}
    shell = UnitShell(watchdogs)
    await run_deploy(fake, job, shell)
    assert "is-active" in shell.calls[0][0][-1] and shell.commands()[-1].split("&&")[1].strip(
        ).startswith("cat > ")
    for unit in (f"proxy-index-campaign-tick@{CID}.service",
                 f"proxy-index-campaign-collector@{CID}.service"):
        with pytest.raises(deploy.DeployError, match="units are running"):
            await run_deploy(fake, job, UnitShell(watchdogs | {unit}))


async def test_disable_stops_one_campaigns_units_and_records_it(deployed):
    shell = Shell()
    with pytest.raises(deploy.DeployError, match="never uploaded"):
        await deploy.disable(deployed, "20270101t230000z-ffffffffffff", shell=shell)
    assert shell.calls == []
    result = await deploy.disable(deployed, CID, shell=shell, now=NOW)
    assert shell.commands() == ["systemctl disable --now " + " ".join(deploy.unit_names(CID))]
    assert result["units"] == deploy.unit_names(CID)
    assert json.loads(deployed.read_text())["disabled"] == {CID: NOW.isoformat()}


async def test_an_uncertain_create_adopts_the_manager_by_name(job):
    fake = Fake()
    fake.create_then_fail = 1
    await run_deploy(fake, job)
    assert inventory(job)["manager_id"] in fake.droplets


async def test_a_failed_create_stops_before_anything_else(job):
    fake = Fake()
    fake.faults.append(Fault("POST", "/v2/droplets", 422))
    with pytest.raises(CreateError):
        await run_deploy(fake, job)
    assert "manager_id" not in inventory(job) and not fake.resources["firewall"]


async def test_a_manager_never_confirmed_in_the_project_is_removed_and_forgotten(job):
    fake = Fake()
    fake.assign_delay = 100
    with pytest.raises(ops_api.OpsError, match="not confirmed"):
        await run_deploy(fake, job)
    saved = inventory(job)
    [gone] = saved["unassigned_managers"]
    assert saved["manager_id"] is None and ("droplet", gone) in fake.deleted
    assert not fake.resources["firewall"]
    fake.assign_delay = 0
    await run_deploy(fake, job)
    assert inventory(job)["manager_id"] not in (None, gone)


@pytest.mark.parametrize("shell, message", [
    (Shell(verdict="weak: sshd passwords"), "provision failed"),
    (Shell(fail=("do-token",)), "secret do-token"),
])
async def test_a_weak_or_failed_manager_step_fails_closed(job, shell, message):
    with pytest.raises(deploy.DeployError, match=message):
        await run_deploy(Fake(), job, shell)
    assert "worker_key_id" not in inventory(job)


async def test_ssh_that_never_answers_fails(job):
    class Down(Shell):
        async def __call__(self, argv, stdin):
            return 255, ""

    with pytest.raises(deploy.DeployError, match="never became ready"):
        await run_deploy(Fake(), job, Down())


def test_the_credential_split_round_trips_one_table_per_file(tmp_path):
    path = tmp_path / "credentials.toml"
    path.write_text("".join(deploy.toml_table(n, v).decode() for n, v in CREDENTIALS.items())
                    + "[unused]\nkey = \"x\"\n")
    files = deploy.credential_files(path, ("shifter", "geonode"))
    assert sorted(files) == ["geonode", "shifter"]
    for network, data in files.items():
        assert tomllib.loads(data.decode()) == {network: CREDENTIALS[network]}
    with pytest.raises(deploy.DeployError, match="no table for soax"):
        deploy.credential_files(path, ("shifter", "soax"))
    for name, values in (("Bad Name", {"a": "b"}), ("x", {}), ("x", {"a": 1}),
                         ("x", {"a b": "c"})):
        with pytest.raises(deploy.DeployError):
            deploy.toml_table(name, values)
    assert deploy.toml_table("x", {"a": "\x00\x7f"}) == b'[x]\na = "\\u0000\\u007f"\n'


def test_the_mac_address_is_read_from_the_echo_target():
    calls = []

    def run(argv, **_):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=f"{MAC_IP}\n")

    assert deploy.mac_ipv4(run) == MAC_IP and calls[0][-1] == deploy.IP_ECHO
    failed = lambda argv, **_: subprocess.CompletedProcess(argv, 6, stdout="")  # noqa: E731
    with pytest.raises(deploy.DeployError):
        deploy.mac_ipv4(failed)
    for bad in ("127.0.0.1", "0.0.0.0", "224.0.0.1", "::1", "nonsense"):
        with pytest.raises(deploy.DeployError):
            deploy.checked_ipv4(bad)


def units():
    parsed = {}
    for name in deploy.UNIT_FILES:
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        parser.read_string((ASSETS / name).read_text())
        parsed[name] = parser
    return parsed


def test_the_unit_files_render():
    found = units()
    tick = found["proxy-index-campaign-tick@.service"]
    timer = found["proxy-index-campaign-tick@.timer"]
    assert dict(timer["Timer"]) == {"OnCalendar": "*-*-* *:58:30", "AccuracySec": "1s",
                                    "Persistent": "true"}
    assert (tick["Service"]["Type"], tick["Service"]["Restart"]) == ("oneshot", "on-failure")
    assert tick["Service"]["TimeoutStartSec"] == "15min"
    collector = found["proxy-index-campaign-collector@.service"]["Service"]
    assert (collector["Restart"], collector["RestartPreventExitStatus"]) == ("always", "0")
    for name in ("tick@.service", "collector@.service"):
        assert found[f"proxy-index-campaign-{name}"]["Service"]["LimitNOFILE"] == "65536"
    for check, calendar in (("armed", "*-*-* *:00:30"), ("fired", "*-*-* *:02:00")):
        assert found[f"proxy-index-campaign-watchdog-{check}@.timer"]["Timer"][
            "OnCalendar"] == calendar
        exec_start = found[f"proxy-index-campaign-watchdog-{check}@.service"]["Service"][
            "ExecStart"]
        assert exec_start.endswith(f"--check {check}")
    for parser in found.values():
        if parser.has_section("Service"):
            argv = shlex.split(parser["Service"]["ExecStart"])
            assert argv[:2] == ["/opt/proxy-index-campaign/venv/bin/proxy-index", "campaign"]
            assert argv[argv.index("--root") + 1] == "/opt/proxy-index-campaign/campaigns/%i"


def test_the_unit_commands_exist_in_the_cli():
    pytest.importorskip("proxy_index.campaign.tick", reason="CAMP-1d (tick, collector, "
                        "watchdog) is not merged into this base yet")
    for parser_unit in units().values():
        if parser_unit.has_section("Service"):
            argv = shlex.split(parser_unit["Service"]["ExecStart"].replace("%i", CID))
            parse(argv[1:])


def test_systemd_analyze_verifies_the_units(tmp_path):
    tool = shutil.which("systemd-analyze")
    if tool is None:
        pytest.skip("systemd-analyze is not available on this host (Linux only)")
    for name in deploy.UNIT_FILES:
        (tmp_path / name.replace("@.", f"@{CID}.")).write_text((ASSETS / name).read_text())
    found = subprocess.run([tool, "verify", *sorted(map(str, tmp_path.iterdir()))],
                           capture_output=True, text=True)
    assert "Unknown key" not in found.stderr and "Invalid" not in found.stderr


def test_unit_names_for_a_campaign():
    assert deploy.unit_names(CID) == [
        f"proxy-index-campaign-tick@{CID}.timer", f"proxy-index-campaign-collector@{CID}.service",
        f"proxy-index-campaign-watchdog-armed@{CID}.timer",
        f"proxy-index-campaign-watchdog-fired@{CID}.timer"]
    with pytest.raises(ValueError):
        deploy.unit_names("../etc")


@pytest.fixture
def deployed(job):
    deploy.save(job.inventory, {}, manager_id="500001", host=MANAGER_IP, user="root",
                identity="k", identity_file="k", known_hosts="kh", project_id=CAMPAIGN_PROJECT,
                manager_since=NOW.isoformat(), manager_firewall_id="fw-1", campaigns=[CID],
                wheel_sha256="ab" * 32, account="leftclick", specs={CID: {
                    "starts_at": "2027-01-01T23:00:00+00:00", "synthetic": False}})
    return job.inventory


async def test_enable_refuses_without_the_owners_go(deployed, tmp_path):
    shell = Shell()
    empty = tmp_path / "empty.md"
    empty.write_text(" \n")
    for go in (None, tmp_path / "missing.md", empty):
        with pytest.raises(deploy.DeployError, match="--owner-go"):
            await deploy.enable(deployed, CID, go, shell=shell, now=NOW)
    answer = tmp_path / "CAMP-1-go.md"
    answer.write_text("Owner: go.\nStart: 2027-01-01T23:00Z\nAccount: leftclick\n")
    with pytest.raises(deploy.DeployError, match="never uploaded"):
        await deploy.enable(deployed, "20270101t230000z-ffffffffffff", answer, shell=shell)
    assert shell.calls == [] and "enabled" not in json.loads(deployed.read_text())
    stale = tmp_path / "CAMP-1-plan-approval.md"
    for text in ("Owner: the plan is approved.\n", "start at 23:00 UTC, round HH:00:00\n",
                 "T22:23:00Z and 2027-01-01T23:00Z\n", "Start: 2027-01-01T23:00Z, maybe\n"):
        stale.write_text(text)  # m3, n2: only the exact start line counts
        with pytest.raises(deploy.DeployError, match="'Start: 2027-01-01T23:00Z'"):
            await deploy.enable(deployed, CID, stale, shell=shell, now=NOW)
    assert shell.calls == []
    result = await deploy.enable(deployed, CID, answer, shell=shell, now=NOW)
    assert shell.commands() == ["systemctl enable --now " + " ".join(deploy.unit_names(CID))]
    recorded = json.loads(deployed.read_text())["enabled"]
    assert recorded["owner_go"] == str(answer.resolve()) and len(recorded["owner_go_sha256"]) == 64
    assert result["units"] == deploy.unit_names(CID) and recorded["synthetic"] is False


async def test_a_synthetic_campaign_needs_a_go_but_no_start_hour(deployed, tmp_path):
    saved = json.loads(deployed.read_text())
    deploy.save(deployed, saved, specs={CID: {"starts_at": "2027-01-01T23:00:00+00:00",
                                              "synthetic": True}})
    approval = tmp_path / "approval.md"
    approval.write_text("Owner: plan approved, including the scheduler proof.\n")
    result = await deploy.enable(deployed, CID, approval, shell=Shell(), now=NOW)
    assert result["synthetic"] is True


def test_the_enable_cli_refuses_without_owner_go(deployed, capsys):
    args = parse(
        ["campaign", "manager", "enable", CID, "--inventory", str(deployed)])
    assert campaign_cli.dispatch(args) == 3
    assert "--owner-go" in capsys.readouterr().err


def planned_root(tmp_path, manager_id="500001", wheel="ab" * 32):
    spec = CampaignSpec(
        CID, NOW.isoformat(), "leftclick", CAMPAIGN_PROJECT, "2027-01-01T23:00:00+00:00", 48,
        ("lon1",), ("shifter",), {"shifter": "networks/shifter/residential/m/campaign.json"},
        {"lon1": "c-2"}, {"lon1": "9001"}, IMAGE, wheel, "0.9.5", "77", manager_id)
    root = tmp_path / "plan" / CID
    (root / "networks/shifter/residential/m").mkdir(parents=True)
    (root / "networks/shifter/residential/m/campaign.json").write_text("{}")
    (root / "networks/shifter/residential/m/provider.toml").write_text("provider = 1\n")
    spec_path(root).write_text(spec.to_json())
    return root


async def test_upload_spec_sends_the_root_and_records_the_campaign(deployed, tmp_path):
    saved = json.loads(deployed.read_text())
    deploy.save(deployed, saved, campaigns=[])
    shell, root = Shell(), planned_root(tmp_path)
    assert await deploy.upload_spec(deployed, root, shell=shell) == {
        "campaign_id": CID, "campaigns": [CID]}
    [(argv, stdin)] = shell.calls
    assert argv[-1].startswith("umask 077 && test ! -e /opt/proxy-index-campaign/campaigns/" + CID)
    assert sorted(tarfile.open(fileobj=io.BytesIO(stdin)).getnames()) == [
        "campaign-spec.json", "networks/shifter/residential/m/campaign.json",
        "networks/shifter/residential/m/provider.toml"]
    saved = json.loads(deployed.read_text())
    assert saved["campaign_id"] == CID and saved["specs"][CID] == {
        "starts_at": "2027-01-01T23:00:00+00:00", "synthetic": False}
    deploy.save(deployed, saved, account="proxy-index")
    with pytest.raises(deploy.DeployError, match="account"):
        await deploy.upload_spec(deployed, planned_root(tmp_path / "c"), shell=shell)
    deploy.save(deployed, saved, account="leftclick")
    for other in (planned_root(tmp_path / "a", manager_id="9"),
                  planned_root(tmp_path / "b", wheel="cd" * 32)):
        with pytest.raises(deploy.DeployError, match="deployed manager, wheel and account"):
            await deploy.upload_spec(deployed, other, shell=shell)
    with pytest.raises(deploy.DeployError, match="exists or failed"):
        await deploy.upload_spec(deployed, root, shell=Shell(fail=("test ! -e",)))


async def test_allow_me_replaces_the_only_ssh_source(job):
    fake = Fake()
    await run_deploy(fake, job)
    async with running(fake) as cloud:
        result = await deploy.allow_me(cloud, job.inventory, OTHER_IP)
    firewall = fake.resources["firewall"][result["manager_firewall_id"]]
    assert firewall["inbound_rules"][0]["sources"] == {"addresses": [f"{OTHER_IP}/32"]}
    assert inventory(job)["mac_ipv4"] == OTHER_IP
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError):
            await deploy.allow_me(cloud, job.inventory, "not-an-address")


@pytest.fixture
def plan_world(tmp_path):
    credentials = tmp_path / "credentials.toml"
    credentials.write_text(deploy.toml_table("shifter", PLAN_CREDENTIALS).decode())
    credentials.chmod(0o600)
    manager_inventory = tmp_path / "campaign-manager.json"
    manager_inventory.write_text(json.dumps({
        "manager_id": "500001", "wheel_sha256": "ab" * 32, "framework_version": "0.9.5",
        "worker_key_name": "pi-camp-worker-20270101", "account": "leftclick",
        "project_id": CAMPAIGN_PROJECT, "credentials": ["shifter"]}))
    images = tmp_path / "campaign-images.json"
    images.write_text(json.dumps({"builds": [{"snapshot_id": "9001", "snapshot_name": IMAGE}]}))
    fake = FakeDigitalOcean()
    fake.sizes = [{"slug": "c-2", "available": True, "regions": list(REGION_IDS)}]
    fake.images["9001"] = {"id": 9001, "name": IMAGE, "regions": list(REGION_IDS)}
    fake.add_resource("ssh_key", 77, name="pi-camp-worker-20270101", public_key=WORKER_PUBLIC)
    inputs = PlanInputs("leftclick", "2027-01-01T23:00:00+00:00", ("shifter",), IMAGE,
                        tmp_path / "plans", credentials, manager_inventory, images,
                        configs=ROOT / "configs")
    return fake, inputs


async def run_plan(fake, inputs):
    async with running(fake) as cloud:
        return await planspec.plan(cloud, inputs, now=NOW)


def test_plan_writes_v3_manifests_and_a_valid_spec(plan_world):
    fake, inputs = plan_world
    result = asyncio.run(run_plan(fake, inputs))
    root = Path(result["root"])
    spec = CampaignSpec.from_json(spec_path(root).read_text())
    assert (spec.project_id, spec.ssh_key_id, spec.manager_droplet_id) == (
        CAMPAIGN_PROJECT, "77", "500001")
    assert spec.regions == tuple(REGION_IDS) and set(spec.image_by_region.values()) == {"9001"}
    assert set(spec.size_by_region.values()) == {"c-2"} and spec.concurrency == 2000
    assert spec.starts_at == "2027-01-01T23:00:00+00:00" and not spec.synthetic
    assert re.fullmatch(r"20270101t230000z-[0-9a-f]{12}", spec.campaign_id)
    path = root / spec.manifests["shifter"]
    loaded = manifest.load(path)
    assert loaded["schema_version"] == 3 and loaded["settings"]["concurrency"] == 2000
    assert spec.manifests["shifter"].startswith("networks/shifter/residential/")
    assert (path.parent / "provider.toml").read_bytes() == (
        ROOT / "configs/shifter-onboarding.toml").read_bytes()
    assert "acct_9" not in "".join(p.read_text() for p in root.rglob("*") if p.is_file())
    assert not [m for m, *_ in fake.requests if m != "GET"]


def test_plan_binds_the_strict_census_config_the_old_servers_ran(plan_world):
    """CAMP-1i: without strict targeting Shifter answers a country it has no exits for (AS,
    HM, NF, NU, UM, AQ) from a broader pool, so the census never saturates (O1: ~2M
    requests per country, ~99% unique) instead of stopping as no_success at ~3.5k."""
    fake, inputs = plan_world
    root = Path(asyncio.run(run_plan(fake, inputs))["root"])
    spec = CampaignSpec.from_json(spec_path(root).read_text())
    bound = (root / spec.manifests["shifter"]).parent / "provider.toml"
    assert manifest.load(bound.parent / "campaign.json")["provider_config_sha256"] == (
        digest(bound))
    # The census parameters of the 2026-10-05 servers' provider.toml (sha256 5f07819a...).
    assert tomllib.loads(bound.read_text())["parameters"] == {"strict": True}
    proxy = load_provider(bound, parameters={"country": "as"},
                          credentials_file=inputs.credentials)
    username = proxy.resolve(RequestContext("run", 0, 0)).username
    assert username == "customer-acct_9-country-as-strict-true"


def test_plan_refuses_a_census_config_without_strict_targeting(plan_world, tmp_path):
    from dataclasses import replace

    fake, inputs = plan_world
    configs = tmp_path / "configs"
    configs.mkdir()
    shutil.copy(ROOT / "configs/shifter.toml", configs / "shifter.toml")
    with pytest.raises(PlanError, match="network shifter: .*strict = true"):
        asyncio.run(run_plan(fake, replace(inputs, configs=configs)))
    (configs / "shifter-onboarding.toml").write_text(
        (ROOT / "configs/shifter-onboarding.toml").read_text().replace(
            "strict = true", "strict = false"))
    with pytest.raises(PlanError, match="network shifter: .*strict = true"):
        asyncio.run(run_plan(fake, replace(inputs, configs=configs)))
    assert not inputs.output.exists()


def test_the_census_config_of_a_network_without_a_strict_flag_is_its_run_config():
    configs = ROOT / "configs"
    assert planspec.census_config(configs, "shifter") == configs / "shifter-onboarding.toml"
    assert planspec.census_config(configs, "soax") == configs / "soax.toml"


async def test_plan_now_hour_and_options(plan_world):
    fake, inputs = plan_world
    from dataclasses import replace

    result = await run_plan(fake, replace(inputs, start="now-hour", regions=("syd1", "lon1"),
                                          concurrency=2500, synthetic=True))
    spec = CampaignSpec.from_json(spec_path(Path(result["root"])).read_text())
    assert spec.starts_at == "2027-01-01T21:00:00+00:00" and spec.regions == ("lon1", "syd1")
    assert spec.concurrency == 2500 and spec.synthetic


async def test_plan_records_the_worker_size_it_checked(plan_world):
    """CAMP-1j: `--size` (default c-2) must be offered in every region and is the spec's size."""
    from dataclasses import replace

    fake, inputs = plan_world
    fake.sizes.append({"slug": "c-4", "available": True, "regions": list(REGION_IDS)})
    fake.sizes.append({"slug": "s-4vcpu-8gb", "available": True, "regions": ["lon1"]})
    result = await run_plan(fake, replace(inputs, size="c-4", concurrency=2500))
    spec = CampaignSpec.from_json(spec_path(Path(result["root"])).read_text())
    assert spec.size_by_region == dict.fromkeys(REGION_IDS, "c-4") and spec.concurrency == 2500
    with pytest.raises(PlanError, match="size s-4vcpu-8gb in fra1"):
        await run_plan(fake, replace(inputs, size="s-4vcpu-8gb"))
    with pytest.raises(PlanError, match="size c-32 in lon1"):
        await run_plan(fake, replace(inputs, size="c-32"))
    for bad in ("g-2vcpu-8gb", "gpu-h100x1-80gb", "m-2vcpu-16gb", "c-2;x", ""):
        with pytest.raises(PlanError, match="--size"):
            await run_plan(fake, replace(inputs, size=bad))
    assert len(list(inputs.output.iterdir())) == 1


@pytest.mark.parametrize("change, message", [
    ("table", "network shifter"), ("size", "size c-2 in syd1"), ("image", "image in syd1"),
    ("key", "ssh key"), ("recorded image", "not one recorded build"),
    ("manager", "deploy the manager"), ("region", "--regions"), ("offset", "UTC offset"),
    ("config", "network shifter"), ("concurrency", "concurrency"), ("past", "future"),
    ("manager credentials", "no credentials for shifter"),
])
async def test_plan_rejects_a_missing_input(plan_world, change, message):
    from dataclasses import replace

    fake, inputs = plan_world
    if change == "table":
        inputs.credentials.write_text(deploy.toml_table("geonode", {"a": "b"}).decode())
    elif change == "size":
        fake.sizes[0]["regions"].remove("syd1")
    elif change == "image":
        fake.images["9001"]["regions"].remove("syd1")
    elif change == "key":
        fake.resources["ssh_key"].clear()
    elif change == "recorded image":
        inputs = replace(inputs, image="pi-camp-worker-other")
    elif change == "manager":
        inputs.inventory.write_text("{}")
    elif change == "region":
        inputs = replace(inputs, regions=("mars1",))
    elif change == "offset":
        inputs = replace(inputs, start="2027-01-01T23:00:00")
    elif change == "config":
        inputs = replace(inputs, configs=inputs.output)
    elif change == "manager credentials":
        recorded = json.loads(inputs.inventory.read_text())
        inputs.inventory.write_text(json.dumps(recorded | {"credentials": planspec.ALL16}))
    elif change == "concurrency":
        inputs = replace(inputs, concurrency=0)
    else:
        inputs = replace(inputs, start="2027-01-01T20:00:00+00:00")
    with pytest.raises((PlanError, ValueError), match=message):
        await run_plan(fake, inputs)
    assert not inputs.output.exists() or not any(inputs.output.iterdir())


async def test_plan_refuses_another_account_than_the_managers(plan_world):
    """B1: the spec's account and project must be the deployed manager's."""
    from dataclasses import replace

    fake, inputs = plan_world
    with pytest.raises(PlanError, match="account"):
        await run_plan(fake, replace(inputs, account="proxy-index"))
    other_team(fake)
    with pytest.raises(PlanError, match="account"):
        await run_plan(fake, inputs)
    assert not inputs.output.exists()


def test_parse_networks():
    assert planspec.parse_networks("all16") == planspec.ALL16 and len(planspec.ALL16) == 16
    assert planspec.parse_networks("shifter,soax") == ("shifter", "soax")
    assert planspec.parse_networks("all16,shifter") == (*planspec.ALL16, "shifter")
    for bad in ("", "a,a", "Bad", "all16,soax"):
        with pytest.raises(PlanError):
            planspec.parse_networks(bad)


def test_the_plan_cli_reports_bad_input(capsys):
    args = parse(
        ["campaign", "plan", "--account", "leftclick", "--start", "now-hour", "--networks",
         "a,a", "--image", IMAGE, "--output", "out"])
    assert campaign_cli.dispatch(args) == 3
    assert "Not planned" in capsys.readouterr().err


class NoCloud:
    """Stands in for the client the CLI builds; records the account it was built for."""

    accounts: list = []

    def __init__(self, account):
        self.accounts.append(account)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


@pytest.mark.parametrize("argv, called", [
    (["upload-spec", "root"], "upload_spec"),
    (["enable", CID, "--owner-go", "go.md"], "enable"),
    (["disable", CID], "disable"),
    (["allow-me", "--mac-ip", MAC_IP], "allow_me"),
    (["allow-me"], "allow_me"),
    (["teardown", "--after-collect", "r.json"], "teardown"),
    (["deploy", "--account", "leftclick", "--wheel", "w", "--requirements", "r",
      "--networks", "shifter", "--mac-ip", MAC_IP], "deploy"),
])
def test_the_manager_cli_routes_each_step(monkeypatch, capsys, tmp_path, argv, called):
    seen, recorded = [], tmp_path / "campaign-manager.json"
    recorded.write_text(json.dumps({"account": "proxy-index"}))

    def stub(name, result):
        async def step(*args, **_):
            seen.append((name, args))
            return result
        return step

    for name in ("upload_spec", "enable", "disable", "allow_me", "teardown", "deploy"):
        monkeypatch.setattr(deploy, name, stub(name, {"clean": name != "teardown"}))
    monkeypatch.setattr(deploy, "mac_ipv4", lambda: OTHER_IP)
    monkeypatch.setattr(deploy, "credential_files", lambda path, nets: {n: b"" for n in nets})
    monkeypatch.setattr("proxy_index.campaign.image.ensure_mac_key", lambda path: path)
    monkeypatch.setattr(campaign_cli, "_cloud", NoCloud)
    NoCloud.accounts = []
    args = parse(["campaign", "manager", *argv, "--inventory", str(recorded)])
    assert campaign_cli.dispatch(args) == (3 if called == "teardown" else 0)
    assert [name for name, _ in seen] == [called]
    if called in ("allow_me", "teardown"):  # B1: the inventory's account, never a flag
        assert NoCloud.accounts == ["proxy-index"]
    if called == "allow_me":
        assert seen[0][1][-1] == (MAC_IP if "--mac-ip" in argv else OTHER_IP)
    if called == "deploy":
        job = seen[0][1][1]
        assert job.mac_ipv4 == MAC_IP and list(job.credentials) == ["shifter"]
        assert NoCloud.accounts == ["leftclick"]
    assert json.loads(capsys.readouterr().out) == {"clean": called != "teardown"}


def test_the_manager_cli_needs_a_recorded_manager_for_teardown(tmp_path, capsys):
    args = parse(["campaign", "manager", "teardown", "--after-collect", "r.json",
                  "--inventory", str(tmp_path / "none.json")])
    assert campaign_cli.dispatch(args) == 3
    assert "no manager is recorded" in capsys.readouterr().err


def test_a_table_that_does_not_round_trip_is_refused(monkeypatch):
    monkeypatch.setattr(deploy, "_toml_string", lambda value: '"changed"')
    with pytest.raises(deploy.DeployError, match="round-trip"):
        deploy.toml_table("x", {"a": "b"})


def test_plan_refuses_a_config_for_another_provider_or_an_existing_folder(
        plan_world, tmp_path, monkeypatch):
    from dataclasses import replace

    fake, inputs = plan_world
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "shifter.toml").write_text('schema_version = 1\nprovider = "geonode"\n')
    with pytest.raises(PlanError, match="another provider"):
        asyncio.run(run_plan(fake, replace(inputs, configs=configs)))
    monkeypatch.setattr(planspec.secrets, "token_hex", lambda n: "0" * 2 * n)
    (inputs.output / "20270101t230000z-000000000000").mkdir(parents=True)
    with pytest.raises(PlanError, match="already exists"):
        asyncio.run(run_plan(fake, inputs))


def test_the_plan_cli_prints_the_summary(monkeypatch, capsys):
    class Cloud:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    async def fake_plan(cloud, inputs):
        return {"campaign_id": CID, "networks": list(inputs.networks),
                "regions": list(inputs.regions)}

    monkeypatch.setattr(campaign_cli, "_cloud", lambda account: Cloud())
    monkeypatch.setattr(planspec, "plan", fake_plan)
    args_list = ["campaign", "plan", "--account", "leftclick", "--start", "now-hour",
                 "--networks", "shifter", "--regions", "lon1", "--image", IMAGE,
                 "--output", "out"]
    args = parse(args_list)
    assert campaign_cli.dispatch(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "campaign_id": CID, "networks": ["shifter"], "regions": ["lon1"]}
    seen = []
    monkeypatch.setattr(planspec, "plan", lambda cloud, inputs: seen.append(inputs.size)
                        or fake_plan(cloud, inputs))
    assert campaign_cli.dispatch(args) == 0
    assert campaign_cli.dispatch(parse([*args_list, "--size", "c-4"])) == 0
    assert seen == ["c-2", "c-4"]
