"""PULSE-1b: the pulse fleet. Reconcile only creates and arms; retire goes only through
`scope.retire_pulse_node` (fake cloud and fake SSH; nothing leaves the machine)."""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from campaign_fakes import PROD_PROJECT, FakeDigitalOcean, Fault, running

from proxy_index.campaign import pulse_fleet
from proxy_index.campaign import scope as scope_module
from proxy_index.campaign.contracts import PULSE_TAG, Droplet, DropletRecord
from proxy_index.campaign.pulse_fleet import NODE_COMMAND, FleetError
from proxy_index.campaign.scope import OwnershipError
from proxy_index.cli import main

DEV = "33333333-3333-4333-8333-333333333333"
TOKEN = "node-token-for-tests-only"
CREDENTIALS = b"password = 'credential-for-tests-only'\n"
WHEEL = "proxy_index-1.0-py3-none-any.whl"
NODE = "pi-pulse-lon1-shifter"


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
        "account": "proxy-index",
        "regions": list(regions),
        "image_by_region": {r: f"img-{r}" for r in regions},
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


async def test_reconcile_creates_assigns_and_arms_one_node_per_network_and_region(tmp_path):
    root = make_root(tmp_path, networks=("geonode", "shifter"), regions=("fra1", "lon1"))
    fake, (remote, issuer, clock) = world(), services()
    creates = []

    def spy(cloud):
        original = cloud.create

        async def create(names, **kwargs):
            creates.append((names, kwargs))
            return await original(names, **kwargs)

        cloud.create = create

    report = await reconcile(fake, root, remote, issuer, clock, prepare=spy)
    names = {f"pi-pulse-{r}-{n}" for n in ("geonode", "shifter") for r in ("fra1", "lon1")}
    assert set(report["created"]) == names
    assert not any(report[k] for k in ("present", "unknown", "failed", "destroy_refused"))
    pulse = {i: d for i, d in fake.droplets.items() if d["name"] in names}
    assert len(pulse) == 4 and all(d["tags"] == [PULSE_TAG] for d in pulse.values())
    assert set(pulse) <= fake.projects[DEV] and not fake.deleted
    assert [(sorted(n), k["region"], k["size"], k["image"]) for n, k in creates] == [
        (["pi-pulse-fra1-geonode", "pi-pulse-fra1-shifter"], "fra1", "s-1vcpu-1gb-amd", "img-fra1"),
        (["pi-pulse-lon1-geonode", "pi-pulse-lon1-shifter"], "lon1", "s-1vcpu-1gb-amd", "img-lon1"),
    ]
    assert all(k["tags"] == [PULSE_TAG] and k["ssh_keys"] == ["9"] for _, k in creates)
    assert sorted(n for n, _, _ in remote.pushed) == sorted(names)
    assert all(command == NODE_COMMAND for *_, command in remote.pushed)
    pushed = {name: bundle(payload) for name, payload, _ in remote.pushed}
    files = pushed["pi-pulse-fra1-geonode"]
    assert sorted(files) == [
        "pulse/node.json",
        "pulse/provider.toml",
        "secrets/credentials.toml",
        "secrets/node-token",
        f"wheel/{WHEEL}",
    ]
    assert {mode for mode, _ in files.values()} == {0o600}
    assert json.loads(files["pulse/node.json"][1]) == {
        "orchestrator": "http://127.0.0.1:18080",
        "provider": "geonode",
        "product": "residential",
        "region": "fra1",
    }
    assert files["secrets/node-token"][1] == f"{TOKEN}\n".encode()
    assert files["secrets/credentials.toml"][1] == CREDENTIALS
    assert sorted(issuer.calls) == sorted(
        (str(root / "db.toml"), n, "residential", r)
        for n in ("geonode", "shifter")
        for r in ("fra1", "lon1")
    )
    ledger = (root / "nodes.json").read_text()
    assert TOKEN not in ledger and "203.0.113" not in ledger and "credential" not in ledger
    assert {r.status for r in records(root).values()} == {"armed"}


async def test_a_second_reconcile_creates_and_pushes_nothing(tmp_path):
    root = make_root(tmp_path, regions=("fra1", "lon1"))
    fake, (remote, issuer, clock) = world(), services()
    await reconcile(fake, root, remote, issuer, clock)
    before, pushes, issued = snapshot(fake), len(remote.pushed), len(issuer.calls)
    creates = len([r for r in fake.requests if r[:2] == ("POST", "/v2/droplets")])
    report = await reconcile(fake, root, remote, issuer, clock)
    assert report["present"] == ["pi-pulse-fra1-shifter", NODE]
    assert not any(report[k] for k in ("created", "unknown", "failed", "destroy_refused"))
    assert snapshot(fake) == before and (len(remote.pushed), len(issuer.calls)) == (pushes, issued)
    assert len([r for r in fake.requests if r[:2] == ("POST", "/v2/droplets")]) == creates


async def test_reconcile_never_deletes_or_moves_unknown_or_untagged_droplets(tmp_path):
    root = make_root(tmp_path, regions=("fra1", "lon1"))
    fake = world()
    stranger = fake.add(NODE, [PULSE_TAG], project=DEV)  # holds a wanted name: not ours
    other = fake.add("pi-pulse-nyc3-shifter", [PULSE_TAG], project=PROD_PROJECT)
    untagged = fake.add("pi-pulse-fra1-shifter", ["prod"])
    before = {i: json.dumps(d, sort_keys=True) for i, d in fake.droplets.items()}
    report = await reconcile(fake, root)
    assert sorted(u["id"] for u in report["unknown"]) == sorted([stranger, other])
    assert report["created"] == ["pi-pulse-fra1-shifter"]  # its own, beside the untagged one
    assert ids_named(fake, NODE) == [stranger] and NODE not in records(root)
    assert all(json.dumps(fake.droplets[i], sort_keys=True) == d for i, d in before.items())
    assert other in fake.projects[PROD_PROJECT] and untagged in fake.projects[PROD_PROJECT]
    assert not fake.deleted and not [r for r in fake.requests if r[0] == "DELETE"]


async def test_a_node_no_longer_in_fleet_json_is_reported_and_left_alone(tmp_path):
    root = make_root(tmp_path, regions=("fra1", "lon1"))
    fake = world()
    await reconcile(fake, root)
    raw = json.loads((root / "fleet.json").read_text())
    raw["regions"] = ["lon1"]
    (root / "fleet.json").write_text(json.dumps(raw))
    [kept] = ids_named(fake, "pi-pulse-fra1-shifter")
    report = await reconcile(fake, root)
    assert report["unknown"] == [{"id": kept, "name": "pi-pulse-fra1-shifter"}]
    assert report["present"] == [NODE] and kept in fake.droplets and not fake.deleted
    assert records(root)["pi-pulse-fra1-shifter"].status == "armed"


async def test_a_node_no_longer_wanted_is_never_armed_again(tmp_path):
    root = make_root(tmp_path, regions=("fra1", "lon1"))
    fake, (remote, issuer, clock) = world(), services()
    remote.ready = False
    await reconcile(fake, root, remote, issuer, clock)
    raw = json.loads((root / "fleet.json").read_text())
    (root / "fleet.json").write_text(json.dumps({**raw, "regions": ["lon1"]}))
    remote.ready = True
    report = await reconcile(fake, root, remote, issuer, clock)
    assert [name for name, _, _ in remote.pushed] == [NODE]
    assert [call[3] for call in issuer.calls] == ["lon1"]
    assert records(root)["pi-pulse-fra1-shifter"].status == "boot_failed"
    assert [item["name"] for item in report["unknown"]] == ["pi-pulse-fra1-shifter"]


async def test_arming_runs_a_bounded_number_of_nodes_at_once(tmp_path, monkeypatch):
    regions = ("ams3", "fra1", "lon1")
    root = make_root(tmp_path, networks=("geonode", "shifter"), regions=regions)
    fake, (remote, issuer, clock) = world(), services()
    monkeypatch.setattr(pulse_fleet, "ARM_AT_ONCE", 2)
    original, inside, most = pulse_fleet.Fleet._arm_one, [0], [0]

    async def counted(self, record):
        inside[0] += 1
        most[0] = max(most[0], inside[0])
        await asyncio.sleep(0)
        try:
            await original(self, record)
        finally:
            inside[0] -= 1

    monkeypatch.setattr(pulse_fleet.Fleet, "_arm_one", counted)
    report = await reconcile(fake, root, remote, issuer, clock)
    assert len(report["created"]) == 6 and len(remote.pushed) == 6 and most[0] == 2


async def test_a_create_whose_answer_was_lost_is_adopted_not_doubled(tmp_path):
    root = make_root(tmp_path)
    fake = world()
    fake.create_then_fail = 1
    report = await reconcile(fake, root)
    [rid] = ids_named(fake, NODE)
    assert report["created"] == [NODE] and records(root)[NODE].droplet_id == rid
    assert records(root)[NODE].status == "armed" and rid in fake.projects[DEV]


async def test_a_create_that_was_refused_is_tried_again_next_time(tmp_path):
    root = make_root(tmp_path)
    fake = world()
    fake.faults.append(Fault("POST", "/v2/droplets", 422))
    report = await reconcile(fake, root)
    assert report["failed"] == [{"name": NODE, "status": "create_failed"}]
    assert not ids_named(fake, NODE)
    report = await reconcile(fake, root)
    assert report["created"] == [NODE] and records(root)[NODE].attempts == 2


async def test_a_restarted_process_adopts_the_droplet_of_its_requested_record(tmp_path):
    root = make_root(tmp_path)
    fake = world()
    rid = fake.add(NODE, [PULSE_TAG])  # created before the crash, still in the default project
    pending = DropletRecord("shifter", "lon1", 0, NODE, attempts=1, requested_at="2027-01-01")
    pulse_fleet.save_nodes(root, {NODE: pending})
    report = await reconcile(fake, root)
    assert report["present"] == [NODE] and not report["created"] and not report["unknown"]
    assert ids_named(fake, NODE) == [rid] and rid in fake.projects[DEV]
    assert records(root)[NODE].droplet_id == rid and records(root)[NODE].status == "armed"


async def test_a_requested_record_whose_droplet_never_came_is_created_again(tmp_path):
    root = make_root(tmp_path)
    fake = world()
    pending = DropletRecord("shifter", "lon1", 0, NODE, attempts=1)
    pulse_fleet.save_nodes(root, {NODE: pending})
    report = await reconcile(fake, root)
    assert report["created"] == [NODE] and records(root)[NODE].attempts == 2


async def test_a_node_that_is_gone_is_created_again(tmp_path):
    root = make_root(tmp_path)
    fake = world()
    await reconcile(fake, root)
    [old] = ids_named(fake, NODE)
    fake.droplets.pop(old)
    report = await reconcile(fake, root)
    [new] = ids_named(fake, NODE)
    assert report["created"] == [NODE] and new != old
    assert records(root)[NODE].droplet_id == new and records(root)[NODE].attempts == 2


async def test_a_recorded_node_without_the_tag_is_reported_and_left_alone(tmp_path):
    root = make_root(tmp_path)
    fake = world()
    await reconcile(fake, root)
    [rid] = ids_named(fake, NODE)
    fake.droplets[rid]["tags"] = ["prod"]
    report = await reconcile(fake, root)
    assert report["failed"] == [{"name": NODE, "error": "not listed under the tag"}]
    assert not report["present"] and not report["created"] and ids_named(fake, NODE) == [rid]
    assert not fake.deleted


async def test_boot_and_start_failures_are_retried_on_later_runs(tmp_path):
    root = make_root(tmp_path)
    fake, (remote, issuer, clock) = world(), services()
    remote.ready = False
    report = await reconcile(fake, root, remote, issuer, clock)
    assert report["failed"] == [{"name": NODE, "status": "boot_failed"}]
    remote.ready, remote.fail = True, True
    report = await reconcile(fake, root, remote, issuer, clock)
    assert report["failed"] == [{"name": NODE, "status": "start_failed"}]
    record = records(root)[NODE]
    assert record.error_class == "RuntimeError" and "203.0.113" not in json.dumps(
        record.error_detail
    )
    remote.fail = False
    report = await reconcile(fake, root, remote, issuer, clock)
    assert report["present"] == [NODE] and len(ids_named(fake, NODE)) == 1
    assert len(remote.pushed) == 1 and len(issuer.calls) == 2


async def test_a_droplet_that_never_becomes_active_fails_its_boot(tmp_path):
    root = make_root(tmp_path)
    fake, (remote, issuer, clock) = world(), services()

    def booting(cloud):
        original = cloud.get

        async def get(droplet_id):
            found = await original(droplet_id)
            return found and replace(found, status="new")

        cloud.get = get

    report = await reconcile(fake, root, remote, issuer, clock, prepare=booting)
    assert report["failed"] == [{"name": NODE, "status": "boot_failed"}]
    assert clock.t >= 1000 + pulse_fleet.READY_LIMIT_SECONDS - pulse_fleet.READY_POLL_SECONDS
    assert not remote.pushed and not issuer.calls


async def test_an_unconfirmed_assignment_ends_destroy_refused_and_is_never_armed(tmp_path):
    root = make_root(tmp_path)
    fake, (remote, issuer, clock) = world(), services()
    fake.faults.append(Fault("POST", f"/v2/projects/{DEV}/resources", 500, times=10**6))
    report = await reconcile(fake, root, remote, issuer, clock)
    [rid] = ids_named(fake, NODE)
    assert report["destroy_refused"] == [{"name": NODE, "id": rid}]
    assert not remote.pushed and not issuer.calls and not fake.deleted
    record = records(root)[NODE]
    assert record.status == "destroy_refused" and record.assign_failed_at
    report = await reconcile(fake, root, remote, issuer, clock)  # left for the owner
    assert report["destroy_refused"] == [{"name": NODE, "id": rid}] and not remote.pushed
    assert not report["created"] and not fake.deleted


async def test_a_late_dev_resources_membership_never_deletes_the_node(tmp_path, monkeypatch):
    """The assignment landed but showed only after the retries: the fail-closed path must
    still leave the node for the owner, never delete it, and report it truthfully."""
    root = make_root(tmp_path)
    fake, (remote, issuer, clock) = world(), services()
    fake.faults.append(Fault("POST", f"/v2/projects/{DEV}/resources", 500, times=10**6))
    original = scope_module.remove_unassigned

    async def late(cloud, scope, records, audit_path, causes, *args, **kwargs):
        for rid in causes:
            fake.move(rid, DEV)
        return await original(cloud, scope, records, audit_path, causes, *args, **kwargs)

    monkeypatch.setattr(scope_module, "remove_unassigned", late)
    report = await reconcile(fake, root, remote, issuer, clock)
    rid = records(root)[NODE].droplet_id
    assert not fake.deleted and ids_named(fake, NODE) == [rid]
    assert not [r for r in fake.requests if r[0] == "DELETE"]
    assert report["destroy_refused"] == [{"name": NODE, "id": rid}]
    record = records(root)[NODE]
    assert record.status == "destroy_refused" and record.assign_failed_at
    assert record.destroyed_at is None and not remote.pushed and not issuer.calls


async def retire(fake, root, name=NODE, prepare=None):
    async with running(fake, per_minute=10**6) as cloud:
        if prepare:
            prepare(cloud)
        return await pulse_fleet.retire(root, name, cloud, FakeClock())


async def test_retire_removes_one_recorded_node_and_checks_it_gone(tmp_path):
    root = make_root(tmp_path, regions=("fra1", "lon1"))
    fake = world()
    await reconcile(fake, root)
    [rid] = ids_named(fake, NODE)
    assert await retire(fake, root) == {"name": NODE, "id": rid, "result": "deleted", "gone": True}
    assert fake.deleted == [("droplet", rid)] and ids_named(fake, "pi-pulse-fra1-shifter")
    assert records(root)[NODE].status == "destroyed" and records(root)[NODE].destroyed_at
    audit = [json.loads(line) for line in (root / "audit.jsonl").read_text().splitlines()]
    assert {line["role"] for line in audit} == {"pulse"}
    report = await reconcile(fake, root)  # still wanted: it comes back
    assert report["created"] == [NODE] and records(root)[NODE].attempts == 2


async def test_retire_refuses_a_node_outside_dev_resources(tmp_path):
    root = make_root(tmp_path)
    fake = world()
    await reconcile(fake, root)
    [rid] = ids_named(fake, NODE)
    fake.move(rid, PROD_PROJECT)
    with pytest.raises(OwnershipError):
        await retire(fake, root)
    assert rid in fake.droplets and not fake.deleted
    assert records(root)[NODE].status == "destroy_refused"


async def test_retire_refuses_a_name_it_never_recorded(tmp_path):
    root = make_root(tmp_path)
    fake = world()
    stranger = fake.add(NODE, [PULSE_TAG], project=DEV)
    with pytest.raises(FleetError, match="not a recorded pulse node"):
        await retire(fake, root)
    assert stranger in fake.droplets and not fake.requests


async def test_a_failed_delete_leaves_the_node_destroy_failed(tmp_path):
    root = make_root(tmp_path)
    fake = world()
    await reconcile(fake, root)
    [rid] = ids_named(fake, NODE)
    fake.faults.append(Fault("DELETE", f"/v2/droplets/{rid}", 500, times=10**6))
    with pytest.raises(Exception) as failed:
        await retire(fake, root)
    assert not isinstance(failed.value, OwnershipError) and rid in fake.droplets
    assert records(root)[NODE].status == "destroy_failed"


async def test_a_node_still_shown_after_its_delete_is_not_counted_gone(tmp_path):
    root = make_root(tmp_path)
    fake = world()
    await reconcile(fake, root)
    [rid] = ids_named(fake, NODE)
    ghost = Droplet(rid, NODE, "lon1", "s-1vcpu-1gb-amd", "active", datetime.now(UTC), (PULSE_TAG,))

    def lingering(cloud):
        original = cloud.get

        async def get(droplet_id):
            return await original(droplet_id) or (ghost if fake.deleted else None)

        cloud.get = get

    result = await retire(fake, root, prepare=lingering)
    assert result == {"name": NODE, "id": rid, "result": "deleted", "gone": False}
    assert records(root)[NODE].status == "destroy_failed"
    assert records(root)[NODE].destroyed_at is None


@pytest.mark.parametrize(
    "change, what",
    [
        ({"account": "someone"}, "account"),
        ({"size": "s-2vcpu-2gb"}, "size"),
        ({"regions": []}, "none"),
        ({"networks": {}}, "none"),
        ({"regions": ["LON1"], "image_by_region": {"LON1": "1"}}, "region"),
        ({"image_by_region": {}}, "image_by_region"),
        ({"orchestrator_url": "ftp://example"}, "orchestrator_url"),
        ({"orchestrator_url": "http://x/ y"}, "orchestrator_url"),
        ({"wheel": "proxy_index.tar.gz"}, "wheel"),
        ({"dbip_base_url": "http://cdn.invalid/dbip/"}, "dbip_base_url"),
        ({"dbip_base_url": "https://cdn.invalid/db ip/"}, "dbip_base_url"),
        ({"networks": {"Bad Name": {}}}, "network name"),
        (
            {
                "networks": {
                    "shifter": {
                        "product": "res idential",
                        "provider_config": "p",
                        "credentials": "c",
                    }
                }
            },
            "shifter",
        ),
    ],
)
def test_fleet_json_is_validated(tmp_path, change, what):
    root = make_root(tmp_path)
    raw = json.loads((root / "fleet.json").read_text())
    (root / "fleet.json").write_text(json.dumps({**raw, **change}))
    with pytest.raises(FleetError, match=what):
        pulse_fleet.load_config(root)


def test_the_config_names_one_node_per_network_and_region(tmp_path):
    root = make_root(tmp_path, networks=("shifter",), regions=("lon1",))
    config = pulse_fleet.load_config(root)
    assert config.wanted() == {NODE: ("shifter", "lon1")} and config.size == "s-1vcpu-1gb-amd"
    assert config.networks["shifter"].provider == "shifter"


def test_cli_status_lists_the_recorded_nodes(tmp_path, capsys):
    root = make_root(tmp_path)
    record = DropletRecord("shifter", "lon1", 0, NODE, "700001", status="armed")
    pulse_fleet.save_nodes(root, {NODE: record})
    assert main(["campaign", "pulse-fleet", "status", "--root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out) == {NODE: {"id": "700001", "status": "armed"}}


def test_cli_retire_needs_a_name(tmp_path, capsys):
    root = make_root(tmp_path)
    assert main(["campaign", "pulse-fleet", "retire", "--root", str(root)]) == 2
    assert "FleetError" in capsys.readouterr().err


class Closing:
    closed = False

    async def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "action, report, code",
    [
        ("reconcile", {"created": [NODE], "failed": [], "destroy_refused": []}, 0),
        ("reconcile", {"created": [], "failed": [{"name": NODE}], "destroy_refused": []}, 1),
        ("reconcile", {"created": [], "failed": [], "destroy_refused": [{"name": NODE}]}, 1),
        ("retire", {"name": NODE, "gone": True}, 0),
        ("retire", {"name": NODE, "gone": False}, 1),
    ],
)
def test_cli_reconcile_and_retire(tmp_path, monkeypatch, capsys, action, report, code):
    from proxy_index.campaign import cli

    root, cloud, seen = make_root(tmp_path), Closing(), []
    monkeypatch.setattr(cli, "_cloud", lambda account: seen.append(account) or cloud)

    async def fake_reconcile(at, got, remote, issuer):
        seen.append((at, got is cloud, type(remote).__name__, issuer.__name__))
        return report

    async def fake_retire(at, name, got):
        seen.append((at, name, got is cloud))
        return report

    monkeypatch.setattr(pulse_fleet, "reconcile", fake_reconcile)
    monkeypatch.setattr(pulse_fleet, "retire", fake_retire)
    argv = ["campaign", "pulse-fleet", action, "--root", str(root), "--name", NODE]
    assert main(argv) == code and cloud.closed
    assert json.loads(capsys.readouterr().out) == report
    called = (
        (root, True, "OpenSSHRemote", "issue_token")
        if action == "reconcile"
        else (root, NODE, True)
    )
    assert seen == ["proxy-index", called]


async def test_with_a_dbip_base_url_the_node_is_told_where_releases_are(tmp_path):
    root = make_root(tmp_path, dbip_base_url="https://cdn.invalid/dbip/")
    remote = Remote()
    await reconcile(world(), root, remote)
    ((_, payload, _),) = remote.pushed
    node = json.loads(bundle(payload)["pulse/node.json"][1])
    assert node["dbip_base_url"] == "https://cdn.invalid/dbip/"


def arm_script(tmp_path, node, databases=(), current=None):
    """Runs NODE_SCRIPT after its wheel install on a fake worker root; returns the systemd units
    written and the systemctl steps."""
    root = tmp_path / "worker"
    (root / "pulse").mkdir(parents=True)
    (root / "db-ip").mkdir()
    for name in databases:
        (root / "db-ip" / name).write_bytes(b"mmdb")
    if current:
        (root / "db-ip" / "current").symlink_to(current)
    (root / "pulse" / "node.json").write_text(json.dumps(node))
    units, steps = tmp_path / "systemd", []
    units.mkdir()
    shim = SimpleNamespace(
        Path=lambda p: units / Path(p).name if p.startswith("/etc/systemd/") else Path(p)
    )
    run = SimpleNamespace(run=lambda command, check: steps.append(command[1:]))
    tail = pulse_fleet.NODE_SCRIPT.split(pulse_fleet.INSTALL_WHEEL)[1]
    exec(tail, {"json": json, "pathlib": shim, "subprocess": run, "root": root, "secrets": root})
    return root, {p.name: p.read_text() for p in units.iterdir()}, steps


NODE_JSON = {"orchestrator": "http://o", "provider": "p", "product": "q", "region": "lon1"}


def test_arming_links_the_image_database_and_installs_the_daily_update(tmp_path):
    base = "https://cdn.invalid/dbip/"
    root, units, steps = arm_script(
        tmp_path, NODE_JSON | {"dbip_base_url": base}, ["dbip-full-2026-11-02.mmdb"]
    )
    assert (root / "db-ip" / "current").resolve().name == "dbip-full-2026-11-02.mmdb"
    assert f"--mmdb {root}/db-ip/current" in units["pi-pulse-node.service"]
    update = f"bin/proxy-index dbip update --dest {root}/db-ip --base-url {base}"
    assert update in units["pi-dbip-update.service"]
    assert "OnCalendar=*-*-* 05:41:00 UTC" in units["pi-dbip-update.timer"]
    assert steps[-1] == ["enable", "--now", "pi-dbip-update.timer"]


def test_arming_keeps_an_updated_current_and_needs_no_timer_without_a_base_url(tmp_path):
    names = ["dbip-full-2026-11-02.mmdb", "dbip-full-2026-12-01.mmdb"]
    root, units, steps = arm_script(tmp_path, NODE_JSON, names, current=names[1])
    assert (root / "db-ip" / "current").resolve().name == names[1]
    assert set(units) == {"pi-pulse-node.service"} and len(steps) == 3
    _, units, _ = arm_script(tmp_path / "bare", NODE_JSON)
    assert "--mmdb" not in units["pi-pulse-node.service"]
