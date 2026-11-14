"""CAMP-1f: cloud operations, the provision script and the Mac watch agent (fakes only)."""

from __future__ import annotations

import plistlib
import re
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    MANAGER_ID,
    TOKEN,
    FakeDigitalOcean,
    no_sleep,
    running,
)
from campaign_image_cases import NOW, PUBLIC_KEY

from atlas.campaign import guard, image, ops_api
from atlas.campaign.contracts import BUILDER_TAG, MANAGER_TAG, WORKER_TAG

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "atlas" / "campaign" / "assets"
SCOPE = ops_api.ops_scope(CAMPAIGN_PROJECT, NOW)
IPV4 = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
# Private keys, DigitalOcean tokens, passwords and home paths (split so the diff's own
# privacy scan does not match this line).
TOKEN_PREFIX, HOME_ROOT = "dop" + "_v1_", "/" + "Users/"
SECRET = re.compile(rf"BEGIN [A-Z ]*PRIVATE|{TOKEN_PREFIX}|password\s*=|{HOME_ROOT}", re.IGNORECASE)


async def test_snapshot_and_transfer_helpers_check_what_they_find():
    fake = FakeDigitalOcean()
    fake.images["12"] = {
        "id": 12,
        "name": "atlas-camp-worker-0.9.5-0123456789ab-20270101",
        "regions": ["lon1", "fra1"],
        "droplet": "",
    }
    async with running(fake) as cloud:
        [builder] = await cloud.create(
            ["atlas-camp-builder-20270101-ab12"],
            region="lon1",
            size="s-1vcpu-1gb",
            image="x",
            tags=[BUILDER_TAG],
            ssh_keys=["9"],
        )
        fake.images["13"] = {
            "id": 13,
            "name": "atlas-camp-worker-1-0123456789ab-20270101",
            "regions": [],
            "droplet": builder.id,
        }
        fake.move(builder.id)
        with pytest.raises(ops_api.OpsError, match="expected one snapshot"):
            await ops_api.snapshot(
                cloud, SCOPE, builder.id, "atlas-camp-worker-1-0123456789ab-20270101", no_sleep
            )
        assert await ops_api.transfer(cloud, "12", ["lon1", "fra1", "ams3"], no_sleep) == [
            "ams3",
            "fra1",
            "lon1",
        ]
        fake.failing_actions.add("transfer")
        with pytest.raises(ops_api.OpsError):
            await ops_api.transfer(cloud, "12", ["syd1"], no_sleep)
        fake.failing_actions.clear()
        fake.images["12"]["regions"] = ["lon1"]
        fake.images["12"]["name"] = "atlas-camp-worker-0.9.5-0123456789ab-20270101"
        fake.droplets.pop(builder.id)
        with pytest.raises(ops_api.OpsError, match="gone"):
            await ops_api.wait_droplet(cloud, builder.id, "active", no_sleep)
    assert [b["region"] for _, p, b in fake.bodies if p.startswith("/v2/images")] == [
        "ams3",
        "syd1",
    ]


async def test_transfer_reports_regions_the_snapshot_never_reached():
    fake = FakeDigitalOcean()
    fake.images["12"] = {
        "id": 12,
        "name": "atlas-camp-worker-0.9.5-0123456789ab-20270101",
        "regions": ["lon1"],
        "droplet": "",
    }
    original = fake._image_action

    async def lost(request):
        response = await original(request)
        fake.images["12"]["regions"].remove("fra1")
        return response

    fake._image_action = lost
    async with running(fake) as cloud:
        with pytest.raises(ops_api.OpsError, match="missing in"):
            await ops_api.transfer(cloud, "12", ["lon1", "fra1"], no_sleep)


async def test_polling_gives_up_after_its_limit():
    fake = FakeDigitalOcean()
    fake.action_polls = 99
    async with running(fake) as cloud:
        [builder] = await cloud.create(
            ["atlas-camp-builder-20270101-ab12"],
            region="lon1",
            size="s-1vcpu-1gb",
            image="x",
            tags=[BUILDER_TAG],
            ssh_keys=["9"],
        )
        fake.move(builder.id)
        action = await cloud.droplet_action(builder.id, "shutdown", CAMPAIGN_PROJECT)
        with pytest.raises(ops_api.OpsError, match="timed out"):
            await ops_api.wait_action(cloud, action, no_sleep, limit=10)


def test_firewall_rules_are_exact():
    everywhere = {"addresses": ["0.0.0.0/0", "::/0"]}
    outbound = [
        {"protocol": "tcp", "ports": "all", "destinations": everywhere},
        {"protocol": "udp", "ports": "all", "destinations": everywhere},
        {"protocol": "icmp", "destinations": everywhere},
    ]
    assert ops_api.worker_firewall("198.51.100.7") == {
        "name": "atlas-camp-worker-fw",
        "outbound_rules": outbound,
        "tags": [WORKER_TAG],
        "droplet_ids": [],
        "inbound_rules": [
            {"protocol": "tcp", "ports": "22", "sources": {"addresses": ["198.51.100.7/32"]}}
        ],
    }
    assert ops_api.manager_firewall("203.0.113.5", MANAGER_ID) == {
        "name": "atlas-camp-manager-fw",
        "outbound_rules": outbound,
        "tags": [],
        "droplet_ids": [int(MANAGER_ID)],
        "inbound_rules": [
            {"protocol": "tcp", "ports": "22", "sources": {"addresses": ["203.0.113.5/32"]}}
        ],
    }


async def test_firewalls_are_created_then_replaced_on_campaign_droplets_only():
    fake = FakeDigitalOcean()
    manager = fake.add(
        "atlas-camp-manager-20270101-ab12",
        [MANAGER_TAG],
        droplet_id=MANAGER_ID,
        project=CAMPAIGN_PROJECT,
    )
    async with running(fake) as cloud:
        firewall_id = await ops_api.apply_firewall(
            cloud, SCOPE, ops_api.manager_firewall("203.0.113.5", manager)
        )
        again = await ops_api.apply_firewall(
            cloud, SCOPE, ops_api.manager_firewall("203.0.113.6", manager), firewall_id
        )
    assert again == firewall_id
    stored = fake.resources["firewall"][firewall_id]
    assert stored["inbound_rules"][0]["sources"] == {"addresses": ["203.0.113.6/32"]}


def test_provision_script_is_valid_pinned_and_secret_free():
    script = ASSETS / "worker-provision.sh"
    text = script.read_text()
    subprocess.run(["bash", "-n", str(script)], check=True)
    assert 'UV_VERSION="0.12.10"' in text
    assert 'UV_SHA256="173d95a0c32d18c896c46ba6fafbf3cf9c14ab74b033f81b76c883ef492a976b"' in text
    assert "sha256sum -c" in text and "--require-hashes" in text
    assert "cloud-init clean" in text and "datasource_list: [ DigitalOcean ]" in text
    assert text.rstrip().endswith("hygiene") and "echo clean" in text
    # m1: our sshd drop-in is read before cloud-init's, and the effective values are checked.
    assert "/etc/ssh/sshd_config.d/01-pi-camp.conf" in text and "sshd -T" in text
    assert text.index("SSHD_EFFECTIVE=") < text.index("rm -f /etc/ssh/ssh_host_*")
    # m2: the build's login traces are emptied and checked.
    assert "/var/log/auth.log /var/log/wtmp" in text and "login trace" in text
    # CTRY-2: at most one database, root-owned and world-readable in the fixed directory.
    assert 'DB_DIR="$WORKER_ROOT/db-ip"' in text and "dirty: several databases" in text
    assert 'chown root:root "$DB_DIR"/*.mmdb' in text and 'chmod 0644 "$DB_DIR"/*.mmdb' in text
    assert text.index('mv "${databases[0]}"') < text.index('rm -rf "$HERE"')
    assert 'reasons+=("database")' in text
    assert not SECRET.search(text) and not IPV4.search(text)


def test_launchd_plist_renders_without_tokens_or_addresses(tmp_path):
    command = guard.watch_command(
        tmp_path / "inv.json", tmp_path / "STATUS.md", tmp_path / "traffic"
    )
    rendered = guard.render(command, tmp_path)
    agent = plistlib.loads(rendered)
    assert agent["Label"] == guard.LABEL and agent["ProgramArguments"] == command
    assert command[1:3] == ["campaign", "watch"] and command[0].endswith("atlas")
    assert agent["KeepAlive"] == {"SuccessfulExit": False} and agent["RunAtLoad"] is True
    assert agent["StandardErrorPath"].startswith(str(tmp_path / "Library" / "Logs"))
    text = rendered.decode()
    assert TOKEN not in text and not IPV4.search(text)
    assert not re.search(rf"BEGIN [A-Z ]*PRIVATE|{TOKEN_PREFIX}", text)  # the local path is fine


def test_guard_installs_reports_and_removes_the_agent(tmp_path):
    calls = []

    def run(argv):
        calls.append(argv)
        loaded = argv[1] == "print" and len(calls) < 4
        out = "\tstate = running\n\tlast exit code = 0\n\t\tnested = x\n" if loaded else ""
        return subprocess.CompletedProcess(
            argv, 0 if argv[1] != "print" or loaded else 113, out, ""
        )

    path = guard.install(["/bin/echo"], tmp_path, run)
    assert path == guard.plist_path(tmp_path) and stat.S_IMODE(path.stat().st_mode) == 0o644
    assert stat.S_IMODE(guard.logs_dir(tmp_path).stat().st_mode) == 0o700
    assert [c[1] for c in calls] == ["bootout", "bootstrap"] and calls[1][3] == str(path)
    assert guard.status(tmp_path, run) == {
        "installed": True,
        "loaded": True,
        "state": "running",
        "last_exit_code": "0",
    }
    assert guard.remove(tmp_path, run) is True and not path.exists()
    assert guard.status(tmp_path, run)["loaded"] is False
    assert guard.remove(tmp_path, run) is False
    with pytest.raises(RuntimeError):
        guard.install(
            ["/bin/echo"], tmp_path, lambda argv: subprocess.CompletedProcess(argv, 5, "", "")
        )


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is needed to build the wheel")
def test_the_wheel_carries_the_assets(tmp_path):
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path), "--quiet"],
        cwd=ROOT,
        check=True,
        timeout=240,
    )
    [wheel] = tmp_path.glob("*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    assert {
        "atlas/campaign/assets/worker-provision.sh",
        "atlas/campaign/assets/launchd-watch.plist",
    } <= set(names)


def test_mac_key_is_created_once_without_a_host_name(tmp_path):
    key = image.ensure_mac_key(tmp_path / "ssh" / "atlas-campaign")
    public = key.with_name(key.name + ".pub").read_text()
    assert public.startswith("ssh-ed25519 ") and public.split()[2] == "atlas-camp-mac"
    before = key.read_bytes()
    assert image.ensure_mac_key(key) == key and key.read_bytes() == before


async def test_local_runners_and_key_matching(tmp_path):
    assert await image.run_shell(["cat"], b"clean\n") == (0, "clean\n")
    streamed = tmp_path / "payload.tar"
    streamed.write_bytes(b"from a file\n")
    assert await image.run_shell(["cat"], streamed) == (0, "from a file\n")
    assert guard._run(["true"]).returncode == 0
    fake = FakeDigitalOcean()
    fake.add_resource("ssh_key", 7, name="atlas-camp-worker-20270101", public_key="ssh-ed25519 X")
    async with running(fake) as cloud:
        assert await ops_api.ensure_ssh_key(cloud, "atlas-camp-mac-20270101", PUBLIC_KEY) == (
            str(max(int(i) for i in fake.resources["ssh_key"])),
            "atlas-camp-mac-20270101",
            True,
        )
        assert await ops_api.ensure_ssh_key(cloud, "atlas-camp-worker-20270202", "ssh-ed25519 X") == (
            "7",
            "atlas-camp-worker-20270101",
            False,
        )
    [(_, _, body)] = fake.bodies
    assert body == {"name": "atlas-camp-mac-20270101", "public_key": " ".join(PUBLIC_KEY.split()[:2])}
