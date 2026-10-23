"""CAMP-1f: cloud operations, the worker image build and the Mac watch agent (fakes only)."""

from __future__ import annotations

import io
import json
import plistlib
import re
import shutil
import stat
import subprocess
import tarfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    MANAGER_ID,
    TOKEN,
    FakeDigitalOcean,
    Fault,
    no_sleep,
    running,
)

from proxy_index import cli as main_cli
from proxy_index.campaign import cli as campaign_cli
from proxy_index.campaign import guard, image, ops_api, scope
from proxy_index.campaign.contracts import BUILDER_TAG, MANAGER_TAG, WORKER_TAG
from proxy_index.campaign.image import ImageJob, build
from proxy_index.regions import REGION_IDS

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "proxy_index" / "campaign" / "assets"
NOW = datetime(2027, 1, 1, 21, 55, tzinfo=UTC)
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTests pi-camp-mac"
IPV4 = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
SECRET = re.compile(r"BEGIN [A-Z ]*PRIVATE|dop_v1_|password\s*=|/Users/", re.IGNORECASE)


class Shell:
    """Fake SSH: records argv and stdin; answers `true` and the provision run."""

    def __init__(self, verdict="clean", ready=True):
        self.calls, self.verdict, self.ready = [], verdict, ready

    async def __call__(self, argv, stdin):
        self.calls.append((argv, stdin))
        if argv[-1] == "true":
            return (0 if self.ready else 255), ""
        return (0 if self.verdict == "clean" else 1), f"apt output\n{self.verdict}\n"


@pytest.fixture
def job(tmp_path):
    wheel = tmp_path / "proxy_index-0.9.5-py3-none-any.whl"
    wheel.write_bytes(b"wheel bytes")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("aiohttp==3.14.3 --hash=sha256:00\n")
    key = tmp_path / "keys" / "proxy-index-campaign"
    key.parent.mkdir()
    key.write_text("private part, never read by the build\n")
    key.with_name(key.name + ".pub").write_text(PUBLIC_KEY + "\n")
    return ImageJob(wheel, requirements, key, tmp_path / "secrets" / "campaign-images.json")


@pytest.fixture(autouse=True)
def quick_assign(monkeypatch):
    """Project assignment retries without real 5 s pauses."""
    original = ops_api.assign_and_verify_project

    async def assign(*args, **kwargs):
        return await original(*args, retry=scope.Retry(sleep=no_sleep), **kwargs)

    monkeypatch.setattr(ops_api, "assign_and_verify_project", assign)


def writes(fake):
    """Mutating requests in order, with ids collapsed."""
    return [(m, re.sub(r"/[0-9a-f-]{6,}", "/N", p)) for m, p, _, _ in fake.requests
            if m != "GET"]


def inventory(job):
    [entry] = json.loads(job.inventory.read_text())["builds"]
    return entry


async def run_build(fake, job, shell=None):
    async with running(fake) as cloud:
        return await build(cloud, job, shell=shell or Shell(), sleep=no_sleep, now=NOW)


async def test_build_runs_every_step_in_order_and_leaves_only_the_snapshot(job):
    fake, shell = FakeDigitalOcean(), Shell()
    result = await run_build(fake, job, shell)
    others = [r for r in REGION_IDS if r != "lon1"]
    assert writes(fake) == [
        ("POST", "/v2/account/keys"), ("POST", "/v2/droplets"),
        ("POST", "/v2/projects/N/resources"), ("POST", "/v2/droplets/N/actions"),
        ("POST", "/v2/droplets/N/actions"), *[("POST", "/v2/images/N/actions")] * 7,
        ("DELETE", "/v2/droplets/N"), ("DELETE", "/v2/account/keys/N")]
    actions = [b for _, p, b in fake.bodies if "/actions" in p]
    assert [b["type"] for b in actions[:2]] == ["shutdown", "snapshot"]
    assert actions[1]["name"] == "pi-camp-worker-0.9.5-" + result["wheel_sha256"][:12] + (
        "-20270101")
    assert [b["region"] for b in actions[2:]] == others  # exactly the 7 other regions
    assert result["state"] == "built" and result["regions"] == sorted(REGION_IDS)
    assert result["leftovers"] == [] and result["mac_key_created"] is True
    assert not fake.droplets and not fake.resources["ssh_key"] and len(fake.images) == 1
    assert inventory(job) == {**result, "started_at": NOW.isoformat()}
    assert stat.S_IMODE(job.inventory.stat().st_mode) == 0o600
    [(probe, _), (argv, stdin)] = shell.calls
    assert probe[-1] == "true" and "StrictHostKeyChecking=accept-new" in argv
    assert argv[-1].endswith("bash /root/pi-camp-build/worker-provision.sh")
    names = tarfile.open(fileobj=io.BytesIO(stdin)).getnames()
    assert names == ["worker-provision.sh", "requirements.txt", job.wheel.name]
    audit = [json.loads(line) for line in job.inventory.with_name(
        "campaign-ops-audit.jsonl").read_text().splitlines()]
    assert [(a["kind"], a["result"]) for a in audit if a["result"] == "deleted"] == [
        ("droplet", "deleted"), ("ssh_key", "deleted")]


async def test_an_existing_mac_key_is_reused_and_kept(job):
    fake = FakeDigitalOcean()
    fake.add_resource("ssh_key", 41, name="pi-camp-mac-20261230", public_key=PUBLIC_KEY.rsplit(
        " ", 1)[0] + " other-comment")
    result = await run_build(fake, job)
    assert result["mac_key_id"] == "41" and result["mac_key_created"] is False
    assert "41" in fake.resources["ssh_key"] and not fake.droplets


def never_active(fake):
    original = fake.add

    def add(*args, **kwargs):
        droplet_id = original(*args, **kwargs)
        fake.droplets[droplet_id]["status"] = "new"
        return droplet_id

    fake.add = add


STAGES = {
    "create": lambda fake: fake.faults.append(Fault("POST", "/v2/droplets", 422)),
    "assign": lambda fake: fake.faults.append(
        Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 422, times=3)),
    "active": never_active,
    "shutdown": lambda fake: fake.failing_actions.add("shutdown"),
    "snapshot": lambda fake: fake.failing_actions.add("snapshot"),
    "transfer": lambda fake: fake.failing_actions.add("transfer"),
}


@pytest.mark.parametrize("stage", [*STAGES, "ssh", "hygiene"])
async def test_a_failure_at_any_step_still_removes_the_builder_through_the_guard(job, stage):
    fake = FakeDigitalOcean()
    fake.action_polls = 1
    STAGES.get(stage, lambda fake: None)(fake)
    shell = Shell(verdict="dirty: host keys" if stage == "hygiene" else "clean",
                  ready=stage != "ssh")
    with pytest.raises(Exception) as raised:
        await run_build(fake, job, shell)
    entry = inventory(job)
    assert entry["state"] == "failed" and type(raised.value).__name__ in entry["error"]
    assert not [d for d in fake.droplets.values() if BUILDER_TAG in d["tags"]]
    assert not fake.resources["ssh_key"] and entry["leftovers"] == []
    if stage not in ("create",):
        audit = job.inventory.with_name("campaign-ops-audit.jsonl").read_text()
        assert '"kind": "droplet"' in audit
    snapshots = [b for _, p, b in fake.bodies if b.get("type") == "snapshot"]
    if stage in ("ssh", "hygiene", "active", "assign", "shutdown"):
        assert not snapshots  # a dirty or unreached builder is never snapshotted


async def test_an_uncertain_create_is_adopted_and_the_build_continues(job):
    fake = FakeDigitalOcean()
    fake.create_then_fail = 1
    result = await run_build(fake, job)
    assert result["state"] == "built" and result["builder_id"] and not fake.droplets


async def test_an_unknown_create_outcome_with_nothing_to_adopt_is_reported(job):
    fake = FakeDigitalOcean()
    fake.faults.append(Fault("POST", "/v2/droplets", 500, times=1))
    with pytest.raises(Exception, match="create stopped"):
        await run_build(fake, job)
    assert inventory(job)["state"] == "failed" and not fake.resources["ssh_key"]


async def test_a_builder_the_guard_cannot_remove_is_reported_as_left_over(job):
    fake = FakeDigitalOcean()
    original = fake._create

    async def create(request):
        response = await original(request)
        for droplet_id in fake.droplets:
            fake.faults.append(Fault("DELETE", f"/v2/droplets/{droplet_id}", 403))
        return response

    fake._create = create
    with pytest.raises(ops_api.OpsError, match="left"):
        await run_build(fake, job)
    entry = inventory(job)
    assert entry["state"] == "built" and entry["leftovers"][0].startswith("droplet ")
    assert not fake.resources["ssh_key"]


async def test_snapshot_and_transfer_helpers_check_what_they_find():
    fake = FakeDigitalOcean()
    fake.images["12"] = {"id": 12, "name": "pi-camp-worker-0.9.5-0123456789ab-20270101",
                         "regions": ["lon1", "fra1"], "droplet": ""}
    async with running(fake) as cloud:
        [builder] = await cloud.create(["pi-camp-builder-20270101-ab12"], region="lon1",
                                       size="s-1vcpu-1gb", image="x", tags=[BUILDER_TAG],
                                       ssh_keys=["9"])
        fake.images["13"] = {"id": 13, "name": "pi-camp-worker-1-0123456789ab-20270101",
                             "regions": [], "droplet": builder.id}
        with pytest.raises(ops_api.OpsError, match="expected one snapshot"):
            await ops_api.snapshot(cloud, builder.id, "pi-camp-worker-1-0123456789ab-20270101",
                                   no_sleep)
        assert await ops_api.transfer(cloud, "12", ["lon1", "fra1", "ams3"], no_sleep) == [
            "ams3", "fra1", "lon1"]
        fake.failing_actions.add("transfer")
        with pytest.raises(ops_api.OpsError):
            await ops_api.transfer(cloud, "12", ["syd1"], no_sleep)
        fake.failing_actions.clear()
        fake.images["12"]["regions"] = ["lon1"]
        fake.images["12"]["name"] = "pi-camp-worker-0.9.5-0123456789ab-20270101"
        fake.droplets.pop(builder.id)
        with pytest.raises(ops_api.OpsError, match="gone"):
            await ops_api.wait_droplet(cloud, builder.id, "active", no_sleep)
    assert [b["region"] for _, p, b in fake.bodies if p.startswith("/v2/images")] == [
        "ams3", "syd1"]


async def test_transfer_reports_regions_the_snapshot_never_reached():
    fake = FakeDigitalOcean()
    fake.images["12"] = {"id": 12, "name": "pi-camp-worker-0.9.5-0123456789ab-20270101",
                         "regions": ["lon1"], "droplet": ""}
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
        [builder] = await cloud.create(["pi-camp-builder-20270101-ab12"], region="lon1",
                                       size="s-1vcpu-1gb", image="x", tags=[BUILDER_TAG],
                                       ssh_keys=["9"])
        action = await cloud.droplet_action(builder.id, "shutdown")
        with pytest.raises(ops_api.OpsError, match="timed out"):
            await ops_api.wait_action(cloud, action, no_sleep, limit=10)


def test_firewall_rules_are_exact():
    everywhere = {"addresses": ["0.0.0.0/0", "::/0"]}
    outbound = [{"protocol": "tcp", "ports": "all", "destinations": everywhere},
                {"protocol": "udp", "ports": "all", "destinations": everywhere},
                {"protocol": "icmp", "destinations": everywhere}]
    assert ops_api.worker_firewall("198.51.100.7") == {
        "name": "pi-camp-worker-fw", "outbound_rules": outbound, "tags": [WORKER_TAG],
        "droplet_ids": [], "inbound_rules": [
            {"protocol": "tcp", "ports": "22", "sources": {"addresses": ["198.51.100.7/32"]}}]}
    assert ops_api.manager_firewall("203.0.113.5", MANAGER_ID) == {
        "name": "pi-camp-manager-fw", "outbound_rules": outbound, "tags": [],
        "droplet_ids": [int(MANAGER_ID)], "inbound_rules": [
            {"protocol": "tcp", "ports": "22", "sources": {"addresses": ["203.0.113.5/32"]}}]}


async def test_firewalls_are_created_then_replaced_on_campaign_droplets_only():
    fake = FakeDigitalOcean()
    manager = fake.add("pi-camp-manager-20270101-ab12", [MANAGER_TAG], droplet_id=MANAGER_ID)
    async with running(fake) as cloud:
        firewall_id = await ops_api.apply_firewall(
            cloud, ops_api.manager_firewall("203.0.113.5", manager))
        again = await ops_api.apply_firewall(
            cloud, ops_api.manager_firewall("203.0.113.6", manager), firewall_id)
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
    assert not SECRET.search(text) and not IPV4.search(text)


def test_launchd_plist_renders_without_tokens_or_addresses(tmp_path):
    command = guard.watch_command(tmp_path / "inv.json", tmp_path / "STATUS.md",
                                  tmp_path / "traffic")
    rendered = guard.render(command, tmp_path)
    agent = plistlib.loads(rendered)
    assert agent["Label"] == guard.LABEL and agent["ProgramArguments"] == command
    assert command[1:3] == ["campaign", "watch"] and command[0].endswith("proxy-index")
    assert agent["KeepAlive"] == {"SuccessfulExit": False} and agent["RunAtLoad"] is True
    assert agent["StandardErrorPath"].startswith(str(tmp_path / "Library" / "Logs"))
    text = rendered.decode()
    assert TOKEN not in text and not IPV4.search(text) and "dop_v1_" not in text


def test_guard_installs_reports_and_removes_the_agent(tmp_path):
    calls = []

    def run(argv):
        calls.append(argv)
        loaded = argv[1] == "print" and len(calls) < 4
        out = "\tstate = running\n\tlast exit code = 0\n\t\tnested = x\n" if loaded else ""
        return subprocess.CompletedProcess(argv, 0 if argv[1] != "print" or loaded else 113,
                                           out, "")

    path = guard.install(["/bin/echo"], tmp_path, run)
    assert path == guard.plist_path(tmp_path) and stat.S_IMODE(path.stat().st_mode) == 0o644
    assert stat.S_IMODE(guard.logs_dir(tmp_path).stat().st_mode) == 0o700
    assert [c[1] for c in calls] == ["bootout", "bootstrap"] and calls[1][3] == str(path)
    assert guard.status(tmp_path, run) == {"installed": True, "loaded": True,
                                           "state": "running", "last_exit_code": "0"}
    assert guard.remove(tmp_path, run) is True and not path.exists()
    assert guard.status(tmp_path, run)["loaded"] is False
    assert guard.remove(tmp_path, run) is False
    with pytest.raises(RuntimeError):
        guard.install(["/bin/echo"], tmp_path, lambda argv: subprocess.CompletedProcess(
            argv, 5, "", ""))


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is needed to build the wheel")
def test_the_wheel_carries_the_assets(tmp_path):
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path), "--quiet"], cwd=ROOT,
                   check=True, timeout=240)
    [wheel] = tmp_path.glob("*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    assert {"proxy_index/campaign/assets/worker-provision.sh",
            "proxy_index/campaign/assets/launchd-watch.plist"} <= set(names)


def test_mac_key_is_created_once_without_a_host_name(tmp_path):
    key = image.ensure_mac_key(tmp_path / "ssh" / "proxy-index-campaign")
    public = key.with_name(key.name + ".pub").read_text()
    assert public.startswith("ssh-ed25519 ") and public.split()[2] == "pi-camp-mac"
    before = key.read_bytes()
    assert image.ensure_mac_key(key) == key and key.read_bytes() == before


def test_image_and_guard_commands_are_wired(monkeypatch, capsys, job, tmp_path):
    seen = {}

    async def fake_build(cloud, built_job):
        seen.update(cloud=cloud, job=built_job)
        return {"state": "built"}

    monkeypatch.setattr(image, "build", fake_build)
    monkeypatch.setattr(campaign_cli, "_cloud", lambda account: FakeCloud())
    assert main_cli.main(["campaign", "image", "build", "--account", "leftclick", "--wheel",
                          str(job.wheel), "--requirements", str(job.requirements), "--mac-key",
                          str(job.mac_key)]) == 0
    assert json.loads(capsys.readouterr().out) == {"state": "built"}
    assert seen["job"].inventory == Path(".secrets/campaign-images.json")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(guard, "install", lambda command, home: home / "agent.plist")
    monkeypatch.setattr(guard, "remove", lambda home: True)
    monkeypatch.setattr(guard, "status", lambda home: {"loaded": False})
    with pytest.raises(SystemExit):
        main_cli.main(["campaign", "guard", "install"])
    for action, expected in (("install", {"installed": str(tmp_path / "agent.plist")}),
                             ("remove", {"removed": True}), ("status", {"loaded": False})):
        assert main_cli.main(["campaign", "guard", action, "--status-file", "S.md",
                              "--traffic-dir", "traffic"]) == 0
        assert json.loads(capsys.readouterr().out) == expected


class FakeCloud:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None


async def test_local_runners_and_key_matching():
    assert await image.run_shell(["cat"], b"clean\n") == (0, "clean\n")
    assert guard._run(["true"]).returncode == 0
    fake = FakeDigitalOcean()
    fake.add_resource("ssh_key", 7, name="pi-camp-worker-20270101", public_key="ssh-ed25519 X")
    async with running(fake) as cloud:
        assert await ops_api.ensure_ssh_key(cloud, "pi-camp-mac-20270101", PUBLIC_KEY) == (
            str(max(int(i) for i in fake.resources["ssh_key"])), True)
    [(_, _, body)] = fake.bodies
    assert body == {"name": "pi-camp-mac-20270101", "public_key": " ".join(PUBLIC_KEY.split()[:2])}
