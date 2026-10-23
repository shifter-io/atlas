"""CAMP-1g: manager teardown and the account binding of every manager step (fakes only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from campaign_deploy_fakes import (
    CID,
    Fake,
    Shell,
    clean_report,
    fast_assign,
    make_job,
    owned_world,
    running,
)
from campaign_fakes import CAMPAIGN_PROJECT

from proxy_index.campaign import deploy
from proxy_index.campaign.contracts import MANAGER_TAG, WORKER_TAG, campaign_tag


@pytest.fixture(autouse=True)
def quick_assign(monkeypatch):
    fast_assign(monkeypatch)


@pytest.fixture
def job(tmp_path):
    return make_job(tmp_path)


async def test_teardown_releases_only_through_the_guard_in_order(job, tmp_path):
    fake, saved, images = await owned_world(job)
    production = {k for k, d in fake.droplets.items() if MANAGER_TAG not in d["tags"]}
    shell, removed = Shell(), []
    async with running(fake) as cloud:
        done = await deploy.teardown(cloud, job.inventory, [clean_report(tmp_path)],
                                     shell=shell, remove_agent=lambda: removed.append(1) or True)
    assert shell.commands() == [
        "systemctl disable --now " + " ".join(deploy.unit_names(CID)), deploy.SHRED]
    assert fake.deleted == [
        ("droplet", saved["manager_id"]), ("firewall", saved["manager_firewall_id"]),
        ("firewall", saved["worker_firewall_id"]), ("ssh_key", saved["worker_key_id"]),
        ("ssh_key", saved["mac_key_id"]), ("snapshot", "9001")]
    assert set(fake.droplets) == production and "9002" in fake.resources["snapshot"]
    assert done["clean"] is True and removed == [1]
    assert done["tag_counts"] == {"pi-camp-worker": 0, "pi-camp-manager": 0,
                                  "pi-camp-builder": 0, campaign_tag(CID): 0}
    audit = [json.loads(line) for line in deploy.audit_path(job.inventory).read_text()
             .splitlines()]
    assert [a["kind"] for a in audit if a["result"] == "deleted"] == [
        "droplet", "firewall", "firewall", "ssh_key", "ssh_key", "snapshot"]
    assert all(all(a["checks"].values()) for a in audit if a["result"] == "deleted")
    assert json.loads(job.inventory.read_text())["teardown"] == done
    assert ("DELETE", "/v2/droplets") not in [(m, p) for m, p, _, _ in fake.requests]


@pytest.mark.parametrize("reports, message", [
    ([], "clean collect report"),
    ([{"clean": False}], "clean collect report"),
    ([{"mismatches": ["a"]}], "clean collect report"),
    ([{"cid": "20270101t230000z-ffffffffffff"}], "clean collect report"),
])
async def test_teardown_refuses_without_a_clean_report(job, tmp_path, reports, message):
    fake, _, images = await owned_world(job)
    paths = [clean_report(tmp_path, change.pop("cid", CID), **change) for change in reports]
    shell = Shell()
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match=message):
            await deploy.teardown(cloud, job.inventory, paths, shell=shell)
    assert shell.calls == [] and not fake.deleted


async def test_teardown_refuses_while_a_worker_lives(job, tmp_path):
    fake, _, images = await owned_world(job)
    fake.add("pic-01234567-h00-lon1-shifter", [WORKER_TAG, "pi-camp-01234567"],
             project=CAMPAIGN_PROJECT)
    shell = Shell()
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match="still alive"):
            await deploy.teardown(cloud, job.inventory, [clean_report(tmp_path)],
                                  shell=shell)
    assert shell.calls == [] and not fake.deleted


async def test_teardown_never_releases_before_the_secrets_are_shredded(job, tmp_path):
    fake, _, images = await owned_world(job)

    class Unshredded(Shell):
        async def __call__(self, argv, stdin):
            self.calls.append((argv, stdin))
            return 0, ""

    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match="not shredded"):
            await deploy.teardown(cloud, job.inventory, [clean_report(tmp_path)],
                                  shell=Unshredded())
        with pytest.raises(deploy.DeployError, match="stop units"):
            await deploy.teardown(cloud, job.inventory, [clean_report(tmp_path)],
                                  shell=Shell(fail=("systemctl",)))
    assert not fake.deleted


async def test_a_refused_release_is_reported_and_the_rest_still_go(job, tmp_path):
    fake, saved, images = await owned_world(job)
    fake.resources["ssh_key"][saved["worker_key_id"]]["name"] = "production-key"
    async with running(fake) as cloud:
        done = await deploy.teardown(cloud, job.inventory, [clean_report(tmp_path)],
                                     shell=Shell(), remove_agent=lambda: False)
    results = {(r["kind"], r["id"]): r["result"] for r in done["released"]}
    assert results[("ssh_key", saved["worker_key_id"])] == "failed: OwnershipError"
    assert saved["worker_key_id"] in fake.resources["ssh_key"] and "9001" not in (
        fake.resources["snapshot"])
    assert done["clean"] is False


async def test_teardown_without_a_manager_or_campaign_units(job, tmp_path):
    with pytest.raises(deploy.DeployError, match="no manager"):
        async with running(Fake()) as cloud:
            await deploy.teardown(cloud, job.inventory, [])
    fake, saved, images = await owned_world(job)
    deploy.save(job.inventory, saved, campaigns=[])
    shell = Shell()
    async with running(fake) as cloud:
        images.unlink()
        done = await deploy.teardown(cloud, job.inventory, [clean_report(tmp_path)],
                                     shell=shell, remove_agent=lambda: True)
    assert shell.commands() == [deploy.SHRED] and done["clean"] is True



def test_teardown_removes_the_mac_agent_by_default(monkeypatch):
    from proxy_index.campaign import guard

    homes = []
    monkeypatch.setattr(guard, "remove", lambda home: homes.append(home) or True)
    assert deploy._remove_agent() is True and homes == [Path.home()]
