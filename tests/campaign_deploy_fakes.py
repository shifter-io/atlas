"""Shared fakes for the CAMP-1g deploy and teardown tests (no live calls)."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from campaign_fakes import PROD_PROJECT, TOKEN, FakeDigitalOcean, no_sleep, running

from proxy_index.campaign import cli as campaign_cli
from proxy_index.campaign import deploy, ops_api, scope

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "proxy_index" / "campaign" / "assets"
NOW = datetime(2027, 1, 1, 21, 55, tzinfo=UTC)
MAC_IP, OTHER_IP, MANAGER_IP = "198.51.100.7", "198.51.100.8", "203.0.113.10"
MAC_PUBLIC = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMacKeyForTests pi-camp-mac"
WORKER_PUBLIC = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIWorkerKeyForTests"
CREDENTIALS = {"shifter": {"account": "customer-a\"b\\c", "password": "p\tw"},
               "geonode": {"username": "u", "password": "v"}}
PLAN_CREDENTIALS = {"account": "customer-acct_9", "password": "p\"w"}
CID = "20270101t230000z-0123456789ab"
IMAGE = "pi-camp-worker-0.9.5-0123456789ab-20270101"


class Shell:
    """Fake SSH to the manager: records argv and stdin; answers like the provisioned host."""

    def __init__(self, verdict="hardened", fail=()):
        self.calls, self.verdict, self.fail = [], verdict, fail

    async def __call__(self, argv, stdin):
        self.calls.append((argv, stdin))
        command = argv[-1]
        if any(word in command for word in self.fail):
            return 1, ""
        if command.endswith("manager-provision.sh"):
            code = 0 if self.verdict == "hardened" else 1
            return code, f"apt\nworker-key {WORKER_PUBLIC}\n{self.verdict}\n"
        if command.startswith("test -d "):  # the base directory exists
            return 0, "present\n"
        return 0, "shredded\n" if "shred -u" in command else ""

    def commands(self):
        return [argv[-1] for argv, _ in self.calls]


class Fake(FakeDigitalOcean):
    """Also records create bodies; offers the manager sizes in lon1."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sizes += [{"slug": slug, "available": True, "regions": ["lon1", "fra1"]}
                       for slug in ("s-8vcpu-16gb", "s-2vcpu-4gb")]

    async def _create(self, request):
        self.bodies.append(("POST", "/v2/droplets", await request.json()))
        return await super()._create(request)

OTHER_TEAM_PROJECT = "44444444-4444-4444-8444-444444444444"


def other_team(fake):
    """The fake answering as another team: its project Proxy Index has another id."""
    fake.project_names = {PROD_PROJECT: "Other Production", OTHER_TEAM_PROJECT: "Proxy Index"}
    fake.projects[OTHER_TEAM_PROJECT] = set()
    return fake


def fast_assign(monkeypatch):
    """Project assignment retries without real 5 s pauses."""
    original = ops_api.assign_and_verify_project

    async def assign(*args, **kwargs):
        return await original(*args, retry=scope.Retry(sleep=no_sleep), **kwargs)

    monkeypatch.setattr(ops_api, "assign_and_verify_project", assign)


def make_job(tmp_path, account="leftclick"):
    wheel = tmp_path / "dist" / "proxy_index-0.9.5-py3-none-any.whl"
    wheel.parent.mkdir(exist_ok=True)
    wheel.write_bytes(b"wheel bytes")
    requirements = tmp_path / "dist" / "requirements.txt"
    requirements.write_text("aiohttp==3.14.3 --hash=sha256:00\n")
    key = tmp_path / "keys" / "proxy-index-campaign"
    key.parent.mkdir(exist_ok=True)
    key.write_text("private part, never read by deploy\n")
    key.with_name(key.name + ".pub").write_text(MAC_PUBLIC + "\n")
    credentials = tmp_path / "credentials.toml"
    credentials.write_text("".join(
        deploy.toml_table(n, v).decode() for n, v in CREDENTIALS.items()))
    return deploy.DeployJob(account, wheel, requirements, key,
                            tmp_path / "secrets" / "campaign-manager.json",
                            deploy.credential_files(credentials, ("shifter",)), MAC_IP,
                            lambda: TOKEN)


async def run_deploy(fake, job, shell=None, now=NOW):
    async with running(fake) as cloud:
        return await deploy.deploy(cloud, job, shell=shell or Shell(), sleep=no_sleep, now=now)


def parse(argv):
    """The `proxy-index` arguments for the campaign group, without running them."""
    parser = argparse.ArgumentParser(prog="proxy-index")
    campaign_cli.register(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(argv)


def inventory(job):
    return json.loads(job.inventory.read_text())


def writes(fake):
    return [(m, re.sub(r"/[0-9a-f-]{6,}", "/N", p)) for m, p, _, _ in fake.requests
            if m != "GET"]



async def owned_world(job):
    """A deployed manager in the fake, plus a recorded snapshot and production droplets."""
    fake = Fake()
    fake.seed_production(40)
    await run_deploy(fake, job)
    saved = inventory(job)
    deploy.save(job.inventory, saved, campaigns=[CID])
    fake.add_resource("snapshot", 9001, name=IMAGE)
    fake.add_resource("snapshot", 9002, name="prod-backup")
    images = job.inventory.with_name("campaign-images.json")
    images.write_text(json.dumps({"builds": [{"snapshot_id": "9001", "snapshot_name": IMAGE},
                                             {"state": "failed"}]}))
    return fake, saved, images


def clean_report(tmp_path, cid=CID, **changes):
    path = tmp_path / f"collect-{cid}-{len(changes)}.json"
    path.write_text(json.dumps({"campaign_id": cid, "clean": True, "mismatches": [],
                                "missing": [], "extra": [], **changes}))
    return path

