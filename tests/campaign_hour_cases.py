"""Shared helpers of the hour engine tests: fake clock and remote, specs, launched hours.

Everything runs against the in-process fake DigitalOcean and a fake `Remote`; no request or
SSH connection leaves the machine. Addresses are RFC 5737 documentation addresses.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from aiohttp import web
from campaign_fakes import CAMPAIGN_PROJECT, CID, MANAGER_ID, SPEC_TIME, FakeDigitalOcean, running
from test_onboarding_round import make as make_manifest
from test_onboarding_round import run_round

from atlas.campaign import fanout, harvest, launch
from atlas.campaign.contracts import CampaignSpec, Droplet, HourRecord

REGIONS = ("lon1", "fra1", "ams3", "nyc3", "tor1", "sgp1", "blr1", "syd1")
NETWORKS = tuple(f"net{i:02d}" for i in range(16))
SCHEDULED = "2027-01-01T22:00:00Z"
PRE_CREATE = datetime.fromisoformat("2027-01-01T21:58:30+00:00").timestamp()
HOUR = 3600.0


class FakeClock:
    def __init__(self, start: float):
        self.t = start

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += max(seconds, 0.0)
        await asyncio.sleep(0)


class FakeRemote:
    """Answers per droplet name; records every push."""

    def __init__(self, clock: FakeClock):
        self.clock, self.never_ready, self.push_failures = clock, set(), {}
        self.pushes, self.inspected = [], {}

    async def wait_ready(self, droplet, limit_seconds):
        if droplet.name in self.never_ready:
            self.clock.t += limit_seconds
            return False
        return True

    def ready_failure(self, droplet):
        return "unreachable" if droplet.name in self.never_ready else None

    async def push_start(self, droplet, payload, command):
        if self.push_failures.get(droplet.name, 0):
            self.push_failures[droplet.name] -= 1
            raise fanout.RemoteError("push and start failed (exit 1)")
        self.pushes.append((droplet.name, payload, command))

    async def inspect(self, droplet):
        if droplet.name not in self.inspected:
            raise fanout.RemoteError("inspect failed (exit 255)")
        return self.inspected[droplet.name]


def make_spec(networks=NETWORKS[:2], regions=REGIONS[:2], **changes) -> CampaignSpec:
    values = dict(
        campaign_id=CID,
        created_at=SPEC_TIME,
        account="leftclick",
        project_id=CAMPAIGN_PROJECT,
        starts_at=SCHEDULED,
        grace_hours=48,
        regions=tuple(regions),
        networks=tuple(networks),
        manifests={n: f"networks/{n}/residential/m-{n}/campaign.json" for n in networks},
        size_by_region=dict.fromkeys(regions, "c-2"),
        image_by_region={r: f"snap-{r}" for r in regions},
        image_name="atlas-camp-worker-x",
        wheel_sha256=hashlib.sha256(b"wheel bytes").hexdigest(),
        framework_version="1.0",
        ssh_key_id="4242",
        manager_droplet_id=MANAGER_ID,
    )
    return CampaignSpec(**(values | changes)).validate()


def make_root(tmp_path: Path, spec: CampaignSpec) -> Path:
    """The manager layout: campaign root, manifests, run configs, wheel and credentials."""
    base = tmp_path / "manager"
    root = base / "campaigns" / spec.campaign_id
    for network in spec.networks:
        manifest = root / spec.manifests[network]
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            json.dumps(
                {
                    "regions": {r: ["US", "GB"] for r in REGIONS},
                    "settings": {"concurrency": spec.concurrency},
                    **({"slot_minutes": spec.slot_minutes} if spec.slot_minutes != 60 else {}),
                }
            )
        )
        (manifest.parent / "provider.toml").write_text(f"provider = '{network}'\n")
        secrets = base / ".secrets" / "credentials"
        secrets.mkdir(parents=True, exist_ok=True)
        (secrets / f"{network}.toml").write_text(f"password = 'secret-{network}'\n")
    (base / "wheels").mkdir()
    (base / "wheels" / "atlas-1.0-py3-none-any.whl").write_bytes(b"wheel bytes")
    (base / "wheels" / "atlas-0.9-py3-none-any.whl").write_bytes(b"old wheel")
    return root


async def launched(tmp_path, fake, spec=None, *, start=PRE_CREATE, record=None, setup=None):
    spec = spec or make_spec()
    root = (
        make_root(tmp_path, spec)
        if not (tmp_path / "manager").exists()
        else (tmp_path / "manager" / "campaigns" / spec.campaign_id)
    )
    clock = FakeClock(start)
    remote = FakeRemote(clock)
    if setup:
        setup(remote)
    record = record or HourRecord(CID, 0, SCHEDULED)
    async with running(fake) as cloud:
        await launch.launch_hour(
            spec, record, root, cloud=cloud, remote_factory=lambda hour: remote, clock=clock
        )
    return record, root, remote, clock


def posts(fake, path):
    return [r for r in fake.requests if r[0] == "POST" and r[1] == path]


def by_name(record, name):
    return [d for d in record.droplets if d.name == name]


class ListFailsAfterCreate(FakeDigitalOcean):
    """Every hour-tag list fails (5xx after the client's retries) once droplets exist."""

    broken = False

    async def _create(self, request):
        response = await super()._create(request)
        self.broken = True
        return response

    async def _list(self, request):
        if self.broken:
            return web.json_response({"id": "server_error"}, status=500)
        return await super()._list(request)


DROPLET = Droplet(
    "700001",
    "atlas-01234567-h00-lon1-net00",
    "lon1",
    "c-2",
    "active",
    datetime.fromisoformat(SPEC_TIME),
    (),
    public_ipv4="203.0.113.7",
)


class FakeRunner:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    async def __call__(self, argv, stdin, seconds, sink, limit):
        self.calls.append((argv, stdin))
        code, out = self.answers.pop(0) if self.answers else (0, b"")
        if len(out) > limit:
            return 254
        sink.write(out)
        return code


def stub_systemctl(tmp_path: Path, state: str | None) -> dict:
    """A `systemctl` that prints `state` (or fails when it is None); returns the env."""
    bin_dir = tmp_path / "bin-systemctl"
    bin_dir.mkdir(exist_ok=True)
    body = f"printf '{state}\\n'" if state is not None else "exit 1"
    (bin_dir / "systemctl").write_text(f"#!/bin/sh\n{body}\n")
    (bin_dir / "systemctl").chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


AT_HH = datetime.fromisoformat("2027-01-01T22:00:00+00:00").timestamp()
RUNNING = {
    "service": {"ActiveState": "active", "SubState": "running"},
    "state": {"status": "running", "jobs": [{"started_at": "2027-01-01T22:00:01.500Z"}]},
}


def ended(code=0, status="completed"):
    return {
        "service": {"ActiveState": "active", "SubState": "exited", "ExecMainStatus": str(code)},
        "state": {"status": status, "jobs": []},
    }


class HarvestRemote(FakeRemote):
    """Pulls copy a real round directory produced by the 1a round runner."""

    def __init__(self, clock, source):
        super().__init__(clock)
        self.source, self.pull_failures, self.pulls, self.stops = source, 0, [], []

    async def pull(self, droplet, remote, local):
        """Applies the real `TERMINAL_CHECK` to the source, with the unit state `inspect` shows."""
        self.pulls.append(remote)
        if self.pull_failures:
            self.pull_failures -= 1
            raise fanout.RemoteError("pull failed (exit 1)")
        service = (self.inspected.get(droplet.name) or {}).get("service") or {
            "ActiveState": "inactive",
            "SubState": "dead",
        }  # systemd's answer for no unit
        unit = "\n".join(f"{key}={value}" for key, value in service.items())
        check = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", fanout.TERMINAL_CHECK, str(self.source)],
            env=stub_systemctl(self.source.parent, unit),
            check=False,
        )
        if check.returncode:
            raise fanout.RemoteError(f"pull failed (exit {check.returncode})")
        shutil.rmtree(local, ignore_errors=True)
        shutil.copytree(self.source, local)

    async def journal(self, droplet, local):
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"journal")

    async def stop(self, droplet):
        self.stops.append(droplet.name)


async def harvest_setup(tmp_path, *, samples=True, fake=None):
    """A launched hour (net00 in tor1) and a worker round directory with real data."""
    worker_manifest = make_manifest(tmp_path / "worker")
    await run_round(worker_manifest, 0, rows=2)
    source = worker_manifest.parent / "regions" / "tor1" / "rounds" / "00"
    (source / "worker-start.json").write_text('{"ntp_synchronized": true}')
    if samples:
        (source / "host-samples.jsonl").write_text(
            "\n".join(
                json.dumps(
                    {
                        "cpu_pct": v,
                        "core_max_pct": v + 5,
                        "steal_pct": 1.0,
                        "load1": 1.5,
                        "mem_used_pct": 30.0,
                        "rss_mb": 200.0,
                    }
                )
                for v in (40.0, 60.0, 90.0)
            )
            + "\n"
            + json.dumps({"time": AT_HH - 30, "cpu_pct": 1.0})
            + "\n"
        )  # idle lead-in (m6)
    spec = make_spec(NETWORKS[:1], ("tor1",))
    root = make_root(tmp_path, spec)
    (root / spec.manifests["net00"]).write_bytes(worker_manifest.read_bytes())
    fake = fake or FakeDigitalOcean()
    record, *_ = await launched(tmp_path, fake, spec)
    assert record.droplets[-1].status == "armed"
    clock = FakeClock(AT_HH + 60)
    return spec, root, fake, record, HarvestRemote(clock, source), clock


async def harvested(spec, record, root, fake, remote, clock):
    async with running(fake) as cloud:
        await harvest.harvest_hour(
            spec, record, root, cloud=cloud, remote_factory=lambda hour: remote, clock=clock
        )
    return record
