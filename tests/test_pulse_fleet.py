"""PULSE-1b: the pulse fleet. Reconcile only creates and arms, never deletes; retiring is in
`test_pulse_fleet_retire.py` (fake cloud and fake SSH; nothing leaves the machine)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace

from campaign_fakes import PROD_PROJECT, Fault
from pulse_fleet_cases import (
    CREDENTIALS,
    DEV,
    NODE,
    TOKEN,
    WHEEL,
    bundle,
    ids_named,
    image_id,
    make_root,
    reconcile,
    records,
    services,
    snapshot,
    world,
)

from atlas.campaign import pulse_fleet
from atlas.campaign import scope as scope_module
from atlas.campaign.contracts import PULSE_TAG, DropletRecord
from atlas.campaign.pulse_fleet import NODE_COMMAND


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
        ([f"pi-pulse-{r}-geonode", f"pi-pulse-{r}-shifter"], r, "s-1vcpu-1gb-amd", image_id(r))
        for r in ("fra1", "lon1")
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


async def test_a_create_that_was_refused_records_why_and_is_tried_again(tmp_path, caplog):
    root = make_root(tmp_path)
    fake = world()
    message = "You specified an invalid image for Droplet creation."
    body = {"id": "unprocessable_entity", "message": message}
    fake.faults.append(Fault("POST", "/v2/droplets", 422, body=body))
    with caplog.at_level(logging.WARNING, logger="atlas.campaign.pulse_fleet"):
        report = await reconcile(fake, root)
    assert report["failed"] == [{"name": NODE, "status": "create_failed"}]
    assert not ids_named(fake, NODE)
    failed = records(root)[NODE]  # the cause is in the ledger and the journal (PULSE-FLEET-FIX-1)
    assert failed.error_class == "unprocessable_entity"
    assert failed.error_detail["message"] == f"POST /v2/droplets failed (422): {message}"
    assert failed.error_detail["http"] == ["POST /v2/droplets 422"]
    logged = f"lon1, 1 droplets: unprocessable_entity: {failed.error_detail['message']}"
    assert caplog.messages == [f"pulse create failed: {logged}"]
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
