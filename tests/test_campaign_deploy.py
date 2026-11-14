"""CAMP-1g: the manager deploy, its firewalls, secrets and allow-me (fakes only)."""

from __future__ import annotations

import io
import json
import re
import stat
import subprocess
import tarfile
import tomllib
from datetime import timedelta

import pytest
from campaign_deploy_fakes import (
    ASSETS,
    CID,
    CREDENTIALS,
    MAC_IP,
    MAC_PUBLIC,
    MANAGER_IP,
    NOW,
    OTHER_IP,
    WORKER_PUBLIC,
    Fake,
    Shell,
    fast_assign,
    inventory,
    make_job,
    run_deploy,
    writes,
)
from campaign_fakes import CAMPAIGN_PROJECT, TOKEN, Fault, running

from atlas.campaign import deploy, ops_api
from atlas.campaign.contracts import MANAGER_TAG, WORKER_TAG
from atlas.campaign.do_api import ApiError, CreateError
from atlas.campaign.hostkeys import known_hosts_option


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
        ("POST", "/v2/account/keys"),
        ("POST", "/v2/droplets"),
        ("POST", "/v2/projects/N/resources"),
        ("POST", "/v2/firewalls"),
        ("POST", "/v2/account/keys"),
        ("POST", "/v2/tags"),
        ("POST", "/v2/firewalls"),
    ]
    [create] = [b for _, p, b in fake.bodies if p == "/v2/droplets"]
    assert create["tags"] == [MANAGER_TAG] and re.fullmatch(
        r"atlas-camp-manager-20270101-[0-9a-f]{4}", create["names"][0]
    )
    assert (create["region"], create["size"], create["image"]) == (
        "lon1",
        "s-8vcpu-16gb",
        "ubuntu-24-04-x64",
    )
    assert create["with_droplet_agent"] is False and create["monitoring"] is False
    mac_key = next(
        k for k in fake.resources["ssh_key"].values() if k["name"].startswith("atlas-camp-mac-")
    )
    assert create["ssh_keys"] == [str(mac_key["id"])]
    saved = inventory(job)
    assert saved["manager_id"] in fake.projects[CAMPAIGN_PROJECT]
    assert (saved["host"], saved["user"], saved["campaigns_root"]) == (
        MANAGER_IP,
        "root",
        "/opt/atlas-campaign/campaigns",
    )
    assert saved["identity"] == saved["identity_file"] == str(job.mac_key)
    assert saved["known_hosts"].endswith("campaign-manager.known_hosts")
    assert saved["worker_key_name"] == "atlas-camp-worker-20270101"
    assert saved["wheel_sha256"] == result["wheel_sha256"] and saved["framework_version"] == "0.9.5"
    assert stat.S_IMODE(job.inventory.stat().st_mode) == 0o600
    assert not re.search(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b", json.dumps(result))
    worker_key = fake.resources["ssh_key"][saved["worker_key_id"]]
    assert worker_key["public_key"] == WORKER_PUBLIC
    commands = shell.commands()
    assert commands[0] == "true" and commands[1].endswith("manager-provision.sh")
    assert [c.split("&&")[1].strip() for c in commands[2:]] == [
        "cat > /opt/atlas-campaign/.secrets/do-token.partial",
        "cat > /opt/atlas-campaign/.secrets/credentials/shifter.toml.partial",
    ]
    argv = shell.calls[0][0]
    assert "BatchMode=yes" in argv and argv[argv.index("-i") + 1] == str(job.mac_key)
    assert known_hosts_option(saved["known_hosts"]) in argv and argv[-2] == f"root@{MANAGER_IP}"


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


async def test_the_archive_spaces_settings_travel_only_on_stdin(job):
    from atlas.archive.spaces import push_settings

    shell = Shell()
    settings = b'[spaces]\nsecret_access_key = "spaces-secret"\n'
    await run_deploy(Fake(), job, shell)
    await push_settings(job.inventory, settings, shell)
    assert shell.commands()[-1].split("&&")[1].strip() == (
        "cat > /opt/atlas-campaign/.secrets/spaces.toml.partial"
    )
    assert all("spaces-secret" not in " ".join(argv) for argv, _ in shell.calls)
    assert [stdin for _, stdin in shell.calls if b"spaces-secret" in stdin] == [settings]


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
    for line in (
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "PermitRootLogin prohibit-password",
        "AuthenticationMethods publickey",
        'Unattended-Upgrade::Automatic-Reboot "false";',
    ):
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
    manager = rules["atlas-camp-manager-fw"]
    assert manager["inbound_rules"] == [{**ssh, "sources": {"addresses": [f"{MAC_IP}/32"]}}]
    assert manager["droplet_ids"] == [int(saved["manager_id"])] and manager["tags"] == []
    worker = rules["atlas-camp-worker-fw"]
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
        writes(fake)
    )
    assert len([d for d in fake.droplets.values() if MANAGER_TAG in d["tags"]]) == 1
    for key in (
        "manager_id",
        "manager_firewall_id",
        "worker_firewall_id",
        "worker_key_id",
        "worker_key_name",
    ):
        assert again[key] == first[key]


async def test_a_missing_worker_tag_is_created_before_its_firewall(job):
    """CAMP-1h: the worker firewall targets atlas-camp-worker, which no droplet carried yet, and
    DigitalOcean answers 422 to a firewall naming a tag that does not exist."""
    fake = Fake()
    fake.seed_production(40)
    production = {i: list(d["tags"]) for i, d in fake.droplets.items()}
    assert WORKER_TAG not in fake.tags
    await run_deploy(fake, job)
    assert [(m, p, b) for m, p, b in fake.bodies if "/tags" in p] == [
        ("POST", "/v2/tags", {"name": WORKER_TAG})
    ]
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
    [worker_key] = [
        k for k in fake.resources["ssh_key"].values() if k["name"].startswith("atlas-camp-worker-")
    ]
    assert again["worker_key_name"] == worker_key["name"] == "atlas-camp-worker-20270101"
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
            return 0, f"active atlas-campaign-tick@{CID}.timer\n"

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
    assert "is-active" in shell.calls[0][0][-1] and shell.commands()[-1].split("&&")[
        1
    ].strip().startswith("cat > ")
    for unit in (
        f"atlas-campaign-tick@{CID}.service",
        f"atlas-campaign-collector@{CID}.service",
    ):
        with pytest.raises(deploy.DeployError, match="units are running"):
            await run_deploy(fake, job, UnitShell(watchdogs | {unit}))


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


@pytest.mark.parametrize(
    "shell, message",
    [
        (Shell(verdict="weak: sshd passwords"), "provision failed"),
        (Shell(fail=("do-token",)), "secret do-token"),
    ],
)
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
    path.write_text(
        "".join(deploy.toml_table(n, v).decode() for n, v in CREDENTIALS.items())
        + '[unused]\nkey = "x"\n'
    )
    files = deploy.credential_files(path, ("shifter", "geonode"))
    assert sorted(files) == ["geonode", "shifter"]
    for network, data in files.items():
        assert tomllib.loads(data.decode()) == {network: CREDENTIALS[network]}
    with pytest.raises(deploy.DeployError, match="no table for soax"):
        deploy.credential_files(path, ("shifter", "soax"))
    for name, values in (("Bad Name", {"a": "b"}), ("x", {}), ("x", {"a": 1}), ("x", {"a b": "c"})):
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


def test_a_table_that_does_not_round_trip_is_refused(monkeypatch):
    monkeypatch.setattr(deploy, "_toml_string", lambda value: '"changed"')
    with pytest.raises(deploy.DeployError, match="round-trip"):
        deploy.toml_table("x", {"a": "b"})
