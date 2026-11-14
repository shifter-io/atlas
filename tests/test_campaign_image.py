"""CAMP-1f: the worker image build and the image and guard commands (fakes only)."""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import tarfile
from pathlib import Path

import pytest
from campaign_fakes import CAMPAIGN_PROJECT, FakeDigitalOcean, Fault, no_sleep, running
from campaign_image_cases import NOW, PUBLIC_KEY

from atlas import cli as main_cli
from atlas.campaign import cli as campaign_cli
from atlas.campaign import guard, image, ops_api, scope
from atlas.campaign.contracts import BUILDER_TAG
from atlas.campaign.image import ImageJob, build
from atlas.regions import REGION_IDS


class Shell:
    """Fake SSH: records argv and stdin; answers `true` and the provision run."""

    def __init__(self, verdict="clean", ready=True):
        self.calls, self.verdict, self.ready = [], verdict, ready

    async def __call__(self, argv, stdin):
        self.calls.append((argv, stdin.read_bytes() if isinstance(stdin, Path) else stdin))
        if argv[-1] == "true":
            return (0 if self.ready else 255), ""
        return (0 if self.verdict == "clean" else 1), f"apt output\n{self.verdict}\n"


@pytest.fixture
def job(tmp_path):
    wheel = tmp_path / "atlas-0.9.5-py3-none-any.whl"
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
    return [(m, re.sub(r"/[0-9a-f-]{6,}", "/N", p)) for m, p, _, _ in fake.requests if m != "GET"]


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
        ("POST", "/v2/account/keys"),
        ("POST", "/v2/droplets"),
        ("POST", "/v2/projects/N/resources"),
        ("POST", "/v2/droplets/N/actions"),
        ("POST", "/v2/droplets/N/actions"),
        *[("POST", "/v2/images/N/actions")] * 7,
        ("DELETE", "/v2/droplets/N"),
        ("DELETE", "/v2/account/keys/N"),
    ]
    actions = [b for _, p, b in fake.bodies if "/actions" in p]
    assert [b["type"] for b in actions[:2]] == ["shutdown", "snapshot"]
    assert actions[1]["name"] == "pi-camp-worker-0.9.5-" + result["wheel_sha256"][:12] + (
        "-20270101"
    )
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
    audit = [
        json.loads(line)
        for line in job.inventory.with_name("campaign-ops-audit.jsonl").read_text().splitlines()
    ]
    assert [(a["kind"], a["result"]) for a in audit if a["result"] == "deleted"] == [
        ("droplet", "deleted"),
        ("ssh_key", "deleted"),
    ]


class Reader:
    """A fake maxminddb reader of the given database type."""

    def __init__(self, database_type):
        self.database_type = database_type

    def metadata(self):
        return type(
            "Metadata", (), {"database_type": self.database_type, "build_epoch": 1_757_808_000}
        )()

    def close(self):
        pass


@pytest.fixture
def mmdb(tmp_path, monkeypatch):
    """A stand-in for the licensed DB-IP Location + ISP file (CTRY-2)."""
    path = tmp_path / "databases" / "dbip-full-2027-01-01.mmdb"
    path.parent.mkdir()
    path.write_bytes(b"database bytes")
    monkeypatch.setattr("maxminddb.open_database", lambda _path: Reader("DBIP-Location-ISP"))
    return path


async def test_the_image_carries_the_country_database_and_records_it(job, mmdb):
    """CTRY-2: the MMDB rides in the provision tar and its name and SHA-256 are recorded."""
    from dataclasses import replace

    shell = Shell()
    result = await run_build(FakeDigitalOcean(), replace(job, mmdb=mmdb), shell)
    stdin = shell.calls[-1][1]
    with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
        assert archive.getnames() == [
            "worker-provision.sh",
            "requirements.txt",
            job.wheel.name,
            mmdb.name,
        ]
        assert archive.extractfile(mmdb.name).read() == b"database bytes"
        assert archive.getmember(mmdb.name).mode == 0o600
    assert result["state"] == "built" and result["mmdb_file"] == mmdb.name
    assert result["mmdb_sha256"] == hashlib.sha256(b"database bytes").hexdigest()
    assert inventory(job)["mmdb_sha256"] == result["mmdb_sha256"]


@pytest.mark.parametrize(
    ("name", "kind", "message"),
    [
        ("dbip-full.mmdb", "GeoLite2-City", "not a DB-IP database"),
        ("dbip-full.csv", "DBIP-Location-ISP", "must name a .mmdb file"),
    ],
)
async def test_a_wrong_database_stops_the_build_before_any_cloud_write(
    job, tmp_path, monkeypatch, name, kind, message
):
    from dataclasses import replace

    path = tmp_path / name
    path.write_bytes(b"x")
    monkeypatch.setattr("maxminddb.open_database", lambda _path: Reader(kind))
    fake = FakeDigitalOcean()
    with pytest.raises(ValueError, match=message):
        await run_build(fake, replace(job, mmdb=path))
    assert writes(fake) == [] and not job.inventory.exists()


async def test_an_existing_mac_key_is_reused_and_kept(job):
    fake = FakeDigitalOcean()
    fake.add_resource(
        "ssh_key",
        41,
        name="pi-camp-mac-20261230",
        public_key=PUBLIC_KEY.rsplit(" ", 1)[0] + " other-comment",
    )
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
        Fault("POST", f"/v2/projects/{CAMPAIGN_PROJECT}/resources", 422, times=3)
    ),
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
    shell = Shell(
        verdict="dirty: host keys" if stage == "hygiene" else "clean", ready=stage != "ssh"
    )
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
    entry = inventory(job)
    assert entry["state"] == "failed" and not fake.resources["ssh_key"]
    assert entry["leftovers"] == [f"builder_unresolved: {entry['builder_name']}"]  # m3


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


def test_image_and_guard_commands_are_wired(monkeypatch, capsys, job, tmp_path):
    seen = {}

    async def fake_build(cloud, built_job):
        seen.update(cloud=cloud, job=built_job)
        return {"state": "built"}

    monkeypatch.setattr(image, "build", fake_build)
    monkeypatch.setattr(campaign_cli, "_cloud", lambda account, token_file=None: FakeCloud())
    assert (
        main_cli.main(
            [
                "campaign",
                "image",
                "build",
                "--account",
                "leftclick",
                "--wheel",
                str(job.wheel),
                "--requirements",
                str(job.requirements),
                "--mac-key",
                str(job.mac_key),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"state": "built"}
    assert seen["job"].inventory == Path(".secrets/campaign-images.json")
    assert seen["job"].mmdb is None
    assert (
        main_cli.main(
            [
                "campaign",
                "image",
                "build",
                "--account",
                "leftclick",
                "--wheel",
                str(job.wheel),
                "--requirements",
                str(job.requirements),
                "--mac-key",
                str(job.mac_key),
                "--mmdb",
                "db-ip/dbip-full.mmdb",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert seen["job"].mmdb == Path("db-ip/dbip-full.mmdb")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(guard, "install", lambda command, home: home / "agent.plist")
    monkeypatch.setattr(guard, "remove", lambda home: True)
    monkeypatch.setattr(guard, "status", lambda home: {"loaded": False})
    with pytest.raises(SystemExit):
        main_cli.main(["campaign", "guard", "install"])
    for action, expected in (
        ("install", {"installed": str(tmp_path / "agent.plist")}),
        ("remove", {"removed": True}),
        ("status", {"loaded": False}),
    ):
        assert (
            main_cli.main(
                ["campaign", "guard", action, "--status-file", "S.md", "--traffic-dir", "traffic"]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out) == expected


class FakeCloud:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None
