"""PULSE-1b: retiring a pulse node goes only through `scope.retire_pulse_node`, and the
`campaign pulse-fleet` commands (fake cloud; nothing leaves the machine)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from campaign_fakes import PROD_PROJECT, Fault, running
from pulse_fleet_cases import DEV, NODE, FakeClock, ids_named, make_root, reconcile, records, world

from atlas.campaign import pulse_fleet
from atlas.campaign.contracts import PULSE_TAG, Droplet, DropletRecord
from atlas.campaign.pulse_fleet import FleetError
from atlas.campaign.scope import OwnershipError
from atlas.cli import main


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
    from atlas.campaign import cli

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
