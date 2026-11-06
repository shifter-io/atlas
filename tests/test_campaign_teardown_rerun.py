"""CAMP-1g: manager teardown reruns, stray managers and teardowns that keep the worker assets
(fakes only)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from campaign_deploy_fakes import (
    CID,
    Shell,
    clean_report,
    inventory,
    owned_world,
    run_deploy,
    running,
)
from campaign_fakes import Fault

from proxy_index.campaign import deploy, teardown
from proxy_index.campaign.contracts import MANAGER_TAG

pytest_plugins = ["campaign_teardown_cases"]
pytestmark = pytest.mark.usefixtures("quick_assign")


async def test_a_rerun_after_a_failed_release_finishes_without_ssh(job, tmp_path):
    """B2: one failed delete is retried alone; the manager is not contacted again."""
    fake, saved, _ = await owned_world(job)
    worker_fw = saved["worker_firewall_id"]
    fake.faults.append(Fault("DELETE", f"/v2/firewalls/{worker_fw}", 422))
    report = clean_report(tmp_path)
    async with running(fake) as cloud:
        first = await teardown.teardown(
            cloud, job.inventory, [report], shell=Shell(), remove_agent=lambda: True
        )
    assert first["clean"] is False and worker_fw in fake.resources["firewall"]
    fake.deleted.clear()
    shell = Shell()
    async with running(fake) as cloud:
        again = await teardown.teardown(
            cloud, job.inventory, [report], shell=shell, remove_agent=lambda: True
        )
    assert shell.calls == [] and fake.deleted == [("firewall", worker_fw)]
    assert again["clean"] is True and len(again["released"]) == len(first["released"])
    assert all(r["result"] in ("deleted", "absent") for r in again["released"])


async def test_a_manager_already_gone_is_never_contacted(job, tmp_path):
    """B2: the droplet (and its disk with the secrets) is gone, so no SSH is attempted."""
    fake, saved, _ = await owned_world(job)
    fake.droplets.pop(saved["manager_id"])
    shell = Shell()
    async with running(fake) as cloud:
        done = await teardown.teardown(
            cloud, job.inventory, [clean_report(tmp_path)], shell=shell, remove_agent=lambda: True
        )
    assert shell.calls == [] and done["clean"] is True
    assert ("droplet", saved["manager_id"]) not in fake.deleted


async def test_a_surviving_manager_keeps_its_firewall_until_a_rerun(job, tmp_path):
    """B2: SSH is never opened on a manager that is still there."""
    fake, saved, _ = await owned_world(job)
    manager, manager_fw = saved["manager_id"], saved["manager_firewall_id"]
    fake.faults.append(Fault("DELETE", f"/v2/droplets/{manager}", 422))
    report = clean_report(tmp_path)
    async with running(fake) as cloud:
        first = await teardown.teardown(
            cloud, job.inventory, [report], shell=Shell(), remove_agent=lambda: True
        )
    results = {(r["kind"], r["id"]): r["result"] for r in first["released"]}
    assert results[("droplet", manager)] == "failed: ApiError"
    assert results[("firewall", manager_fw)].startswith("kept")
    assert manager_fw in fake.resources["firewall"] and first["clean"] is False
    shell = Shell()
    async with running(fake) as cloud:
        again = await teardown.teardown(
            cloud, job.inventory, [report], shell=shell, remove_agent=lambda: True
        )
    assert shell.calls == [] and again["clean"] is True
    assert manager not in fake.droplets and manager_fw not in fake.resources["firewall"]


async def test_an_unclean_report_is_refused_even_with_no_campaign_recorded(job, tmp_path):
    """m5: a given report must itself be clean."""
    fake, saved, _ = await owned_world(job)
    deploy.save(job.inventory, saved, campaigns=[])
    report = clean_report(tmp_path, "20270101t230000z-ffffffffffff", clean=False)
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match="is not clean"):
            await teardown.teardown(cloud, job.inventory, [report], shell=Shell())
    assert not fake.deleted


async def test_a_stray_unassigned_manager_keeps_the_teardown_unclean(job, tmp_path):
    """m1: a manager the guard could not remove at deploy time is listed, never forgotten."""
    fake, saved, _ = await owned_world(job)
    stray = fake.add("pi-camp-manager-20270101-beef", [MANAGER_TAG])
    deploy.save(job.inventory, saved, unassigned_managers=[stray, "999999"])
    async with running(fake) as cloud:
        done = await teardown.teardown(
            cloud, job.inventory, [clean_report(tmp_path)], shell=Shell(), remove_agent=lambda: True
        )
    assert done["unassigned_managers"] == [stray] and done["clean"] is False
    assert stray in fake.droplets


class NewManagerShell(Shell):
    """A new manager generates its own worker key (the old one was shredded with its disk)."""

    async def __call__(self, argv, stdin):
        code, out = await super().__call__(argv, stdin)
        return code, out.replace("IWorkerKeyForTests", "INewWorkerKeyForTests")


async def test_keeping_the_worker_assets_removes_only_the_manager_and_its_firewall(job, tmp_path):
    """CAMP-1m: the snapshot, worker firewall and keys stay; the manager is retired."""
    fake, saved, _ = await owned_world(job)
    known_hosts = tmp_path / "secrets" / "campaign-manager.known_hosts"
    known_hosts.write_text("old manager host key\n")
    async with running(fake) as cloud:
        done = await teardown.teardown(
            cloud,
            job.inventory,
            [clean_report(tmp_path)],
            keep_worker_assets=True,
            shell=Shell(),
            remove_agent=lambda: True,
        )
    assert fake.deleted == [
        ("droplet", saved["manager_id"]),
        ("firewall", saved["manager_firewall_id"]),
    ]
    assert done["clean"] is True and done["kept_worker_assets"] is True
    assert saved["worker_firewall_id"] in fake.resources["firewall"]
    assert {saved["worker_key_id"], saved["mac_key_id"]} <= set(fake.resources["ssh_key"])
    assert "9001" in fake.resources["snapshot"] and not known_hosts.exists()
    after = inventory(job)
    assert not any(key in after for key in teardown.RESET)
    assert after["kept_worker_key_ids"] == [saved["worker_key_id"]]
    assert (after["worker_firewall_id"], after["mac_key_id"]) == (
        saved["worker_firewall_id"],
        saved["mac_key_id"],
    )
    [retired] = after["retired_managers"]
    assert (retired["manager_id"], retired["campaigns"], retired["teardown"]) == (
        saved["manager_id"],
        [CID],
        done,
    )
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match="no manager is recorded"):
            await teardown.teardown(cloud, job.inventory, [], keep_worker_assets=True)


async def test_the_next_manager_reuses_the_worker_firewall_and_a_full_teardown_ends_all(
    job, tmp_path
):
    fake, saved, _ = await owned_world(job)
    report = clean_report(tmp_path)
    async with running(fake) as cloud:
        await teardown.teardown(
            cloud,
            job.inventory,
            [report],
            keep_worker_assets=True,
            shell=Shell(),
            remove_agent=lambda: True,
        )
    await run_deploy(fake, replace(job, size="s-2vcpu-4gb"), NewManagerShell())
    new = inventory(job)
    assert new["manager_id"] != saved["manager_id"] and new["manager_size"] == "s-2vcpu-4gb"
    assert new["worker_firewall_id"] == saved["worker_firewall_id"]
    assert new["worker_key_id"] != saved["worker_key_id"]
    assert new["worker_key_name"].startswith(saved["worker_key_name"] + "-")  # unique for plan
    assert new["mac_key_id"] == saved["mac_key_id"]
    assert "secrets_shredded" not in new  # m1: the new manager's secrets are shredded too
    fake.deleted.clear()
    shell = Shell()
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match="for no recorded campaign"):
            await teardown.teardown(cloud, job.inventory, [report], shell=shell)
        done = await teardown.teardown(
            cloud, job.inventory, [], shell=shell, remove_agent=lambda: True
        )
    assert shell.commands()[-1] == teardown.SHRED
    assert fake.deleted == [
        ("droplet", new["manager_id"]),
        ("firewall", new["manager_firewall_id"]),
        ("firewall", saved["worker_firewall_id"]),
        ("ssh_key", new["worker_key_id"]),
        ("ssh_key", saved["worker_key_id"]),
        ("ssh_key", saved["mac_key_id"]),
        ("snapshot", "9001"),
    ]
    assert done["clean"] is True


async def test_a_kept_teardown_that_fails_retires_nothing_until_a_clean_rerun(job, tmp_path):
    fake, saved, _ = await owned_world(job)
    fake.faults.append(Fault("DELETE", f"/v2/droplets/{saved['manager_id']}", 422))
    report = clean_report(tmp_path)
    async with running(fake) as cloud:
        first = await teardown.teardown(
            cloud,
            job.inventory,
            [report],
            keep_worker_assets=True,
            shell=Shell(),
            remove_agent=lambda: True,
        )
    assert first["clean"] is False and inventory(job)["manager_id"] == saved["manager_id"]
    async with running(fake) as cloud:
        again = await teardown.teardown(
            cloud,
            job.inventory,
            [report],
            keep_worker_assets=True,
            shell=Shell(),
            remove_agent=lambda: True,
        )
    assert again["clean"] is True and "manager_id" not in inventory(job)
    assert saved["worker_firewall_id"] in fake.resources["firewall"]
