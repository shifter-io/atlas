"""Shared fakes and helpers of the pulse fleet tests (fake cloud and fake SSH; nothing leaves
the machine)."""

from __future__ import annotations

import io
import json
import tarfile

from campaign_fakes import FakeDigitalOcean, running

from atlas.campaign import pulse_fleet

DEV = "33333333-3333-4333-8333-333333333333"
TOKEN = "node-token-for-tests-only"
CREDENTIALS = b"password = 'credential-for-tests-only'\n"
WHEEL = "atlas-1.0-py3-none-any.whl"
NODE = "atlas-pulse-lon1-shifter"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class Remote:
    def __init__(self):
        self.ready, self.fail, self.pushed = True, False, []

    async def wait_ready(self, droplet, limit_seconds):
        return self.ready

    def ready_failure(self, droplet):
        return None if self.ready else "unreachable"

    async def push_start(self, droplet, payload, command):
        if self.fail:
            raise RuntimeError("push failed")
        self.pushed.append((droplet.name, payload, command))


class Issuer:
    def __init__(self):
        self.calls = []

    def __call__(self, database_file, *, provider, product, region):
        self.calls.append((database_file, provider, product, region))
        return {"token": TOKEN, "node_id": 7}


def image_id(region: str) -> str:
    """The region's snapshot id in the test fleet: digits, as DigitalOcean's create needs."""
    return "".join(str(ord(c)) for c in region)


def make_root(tmp_path, networks=("shifter",), regions=("lon1",), **extra):
    root = tmp_path / "fleet"
    root.mkdir()
    for name, data in (
        ("provider.toml", b"[provider]\n"),
        ("credentials.toml", CREDENTIALS),
        ("id", b"key"),
        ("db.toml", b"[database]\n"),
        (WHEEL, b"wheel bytes"),
    ):
        (root / name).write_bytes(data)
    raw = {
        "account": "atlas",
        "regions": list(regions),
        "image_by_region": {r: image_id(r) for r in regions},
        "ssh_key_id": 9,
        "identity": "id",
        "wheel": WHEEL,
        "orchestrator_url": "http://127.0.0.1:18080",
        "database_file": "db.toml",
        "networks": {
            n: {
                "product": "residential",
                "provider_config": "provider.toml",
                "credentials": "credentials.toml",
            }
            for n in networks
        },
        **extra,
    }
    (root / "fleet.json").write_text(json.dumps(raw))
    return root


def world(production=20):
    fake = FakeDigitalOcean()
    fake.projects[DEV] = set()
    fake.project_names[DEV] = "Dev Resources"
    fake.seed_production(production)
    return fake


def services():
    return Remote(), Issuer(), FakeClock()


async def reconcile(fake, root, remote=None, issuer=None, clock=None, prepare=None):
    remote, issuer, clock = remote or Remote(), issuer or Issuer(), clock or FakeClock()
    async with running(fake, per_minute=10**6) as cloud:
        if prepare:
            prepare(cloud)
        return await pulse_fleet.reconcile(root, cloud, remote, issuer, clock)


def ids_named(fake, name):
    return sorted(i for i, d in fake.droplets.items() if d["name"] == name)


def records(root):
    return pulse_fleet.load_nodes(root)


def snapshot(fake):
    return json.dumps(fake.droplets, sort_keys=True), json.dumps(
        {p: sorted(m) for p, m in fake.projects.items()}, sort_keys=True
    )


def bundle(payload):
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        return {m.name: (m.mode, archive.extractfile(m).read()) for m in archive.getmembers()}
