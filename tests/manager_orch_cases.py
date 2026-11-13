"""Shared fakes for the MANAGER-ORCH-1 manager-step tests: a fake SSH shell that answers like the
manager, the enable inputs and a deployed manager in the fake DigitalOcean (no live calls)."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile

from campaign_deploy_fakes import CID, Fake, inventory, run_deploy
from campaign_fakes import running

from atlas.campaign import deploy, manager_orch
from atlas.campaign.contracts import PULSE_TAG

WANTED = manager_orch.WANTED_SIZE
CERT_PEM = "-----BEGIN CERTIFICATE-----\nMIIBfakeCERTbody0123456789+/=\n-----END CERTIFICATE-----\n"
CHECK_OK = {"ok": True, "issues": [], "networks": ["shifter"], "droplets_by_hour": {"5": 1}}


def directives(unit):
    """A unit file's section and key lines, without its comments."""
    text = unit.decode() if isinstance(unit, bytes) else unit
    return [line for line in text.splitlines() if line.strip() and not line.startswith("#")]


def installable(unit):
    """True when anything could enable the unit: an [Install] section or an install key."""
    keys = ("WantedBy=", "RequiredBy=", "UpheldBy=", "Alias=", "Also=")
    return any(line == "[Install]" or line.startswith(keys) for line in directives(unit))


def starts_hourly(script):
    """Lines that could turn the hourly test on; reading its state (`is-active`) is not one."""
    words = ("enable", "start", "restart", "add-wants", "link", "reenable", "preset")
    return [
        line
        for line in script.splitlines()
        if "proxy-index-daily" in line
        and "is-active" not in line
        and any(f"systemctl {word}" in line for word in words)
    ]


def staged(stdin):
    """{member name: bytes} of the enable script's stdin tar."""
    with tarfile.open(fileobj=io.BytesIO(stdin)) as archive:
        return {m.name: archive.extractfile(m).read() for m in archive}


class Shell:
    """Fake SSH: records argv and stdin; answers the enable script like the manager (the check
    line, each received formula's SHA-256, the certificate, `healthy`) and the hourly ones."""

    def __init__(self, healthy=True, check=CHECK_OK, tamper=False, timer="active", busy=False):
        self.calls, self.healthy, self.check = [], healthy, check
        self.tamper, self.timer, self.busy = tamper, timer, busy

    async def __call__(self, argv, stdin):
        self.calls.append((argv, stdin))
        command, check = argv[-1], f"daily-check {json.dumps(self.check, separators=(',', ':'))}"
        if "systemctl restart" in command:
            if self.busy:  # the hourly timer is on, or an hour is running
                return 5, "daily-busy\n"
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


async def recorded_size(fake, job):
    async with running(fake) as cloud:
        await manager_orch.resize(cloud, job.inventory, apply=True)


async def enabled(job, tmp_path, fake=None):
    fake = fake or await deployed(job)
    await recorded_size(fake, job)
    async with running(fake) as cloud:
        await manager_orch.enable(
            cloud, job.inventory, go_inputs(tmp_path), apply=True, shell=Shell()
        )
