"""CAMP-1g: manager teardown and the account binding of every manager step (fakes only)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from campaign_deploy_fakes import (
    CID,
    OTHER_IP,
    Fake,
    Shell,
    clean_report,
    fast_assign,
    inventory,
    make_job,
    other_team,
    owned_world,
    run_deploy,
    running,
    writes,
)
from campaign_fakes import CAMPAIGN_PROJECT, Fault

from proxy_index.campaign import deploy
from proxy_index.campaign.contracts import MANAGER_TAG, WORKER_TAG, campaign_tag
from proxy_index.campaign.hostkeys import known_hosts_option

OTHER_CID = "20270101t230000z-ffffffffffff"


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
        done = await deploy.teardown(
            cloud,
            job.inventory,
            [clean_report(tmp_path)],
            shell=shell,
            remove_agent=lambda: removed.append(1) or True,
        )
    assert shell.commands() == [
        "systemctl disable --now " + " ".join(deploy.unit_names(CID)),
        deploy.PROBE,
        deploy.SHRED,
    ]
    assert fake.deleted == [
        ("droplet", saved["manager_id"]),
        ("firewall", saved["manager_firewall_id"]),
        ("firewall", saved["worker_firewall_id"]),
        ("ssh_key", saved["worker_key_id"]),
        ("ssh_key", saved["mac_key_id"]),
        ("snapshot", "9001"),
    ]
    assert set(fake.droplets) == production and "9002" in fake.resources["snapshot"]
    assert done["clean"] is True and removed == [1] and done["secrets"] == "shredded"
    assert done["tag_counts"] == {
        "pi-camp-worker": 0,
        "pi-camp-manager": 0,
        "pi-camp-builder": 0,
        campaign_tag(CID): 0,
    }
    audit = [json.loads(line) for line in deploy.audit_path(job.inventory).read_text().splitlines()]
    assert [a["kind"] for a in audit if a["result"] == "deleted"] == [
        "droplet",
        "firewall",
        "firewall",
        "ssh_key",
        "ssh_key",
        "snapshot",
    ]
    assert all(all(a["checks"].values()) for a in audit if a["result"] == "deleted")
    assert json.loads(job.inventory.read_text())["teardown"] == done
    assert ("DELETE", "/v2/droplets") not in [(m, p) for m, p, _, _ in fake.requests]


@pytest.mark.parametrize(
    "reports, message",
    [
        ([], "clean collect report"),
        ([{"clean": False}], "is not clean"),
        ([{"mismatches": ["a"]}], "is not clean"),
        ([{"cid": OTHER_CID}], "for no recorded campaign"),
    ],
)
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
    fake.add(
        "pic-01234567-h00-lon1-shifter", [WORKER_TAG, "pi-camp-01234567"], project=CAMPAIGN_PROJECT
    )
    shell = Shell()
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match="still alive"):
            await deploy.teardown(cloud, job.inventory, [clean_report(tmp_path)], shell=shell)
    assert shell.calls == [] and not fake.deleted


async def test_teardown_never_releases_before_the_secrets_are_shredded(job, tmp_path):
    fake, _, images = await owned_world(job)

    class Unshredded(Shell):
        async def __call__(self, argv, stdin):
            self.calls.append((argv, stdin))
            return 0, ""

    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match="not shredded"):
            await deploy.teardown(
                cloud, job.inventory, [clean_report(tmp_path)], shell=Unshredded()
            )
        with pytest.raises(deploy.DeployError, match="stop units"):
            await deploy.teardown(
                cloud, job.inventory, [clean_report(tmp_path)], shell=Shell(fail=("systemctl",))
            )
    assert not fake.deleted


class NeverDeployed(Shell):
    """CAMP-1t: a manager whose deploy stopped before it made the base directory."""

    async def __call__(self, argv, stdin):
        self.calls.append((argv, stdin))
        return (0, "absent\n") if argv[-1].startswith("test -d ") else (1, "")


async def test_a_manager_without_its_base_directory_is_torn_down_without_a_shred(job, tmp_path):
    """CAMP-1t: nothing to shred is recorded, and the manager goes through the guard."""
    fake, saved, _ = await owned_world(job)
    deploy.save(job.inventory, saved, campaigns=[])
    shell = NeverDeployed()
    async with running(fake) as cloud:
        done = await deploy.teardown(
            cloud,
            job.inventory,
            [],
            keep_worker_assets=True,
            shell=shell,
            remove_agent=lambda: True,
        )
    assert shell.commands() == [deploy.PROBE]
    assert done["clean"] is True and fake.deleted == [
        ("droplet", saved["manager_id"]),
        ("firewall", saved["manager_firewall_id"]),
    ]
    assert done["secrets"] == f"none: {deploy.BASE} does not exist"
    assert inventory(job)["retired_managers"][-1]["teardown"]["secrets"] == done["secrets"]


@pytest.mark.parametrize(
    "fail, message", [("shred -u", "shred secrets"), ("test -d", "find the base directory")]
)
async def test_a_failed_shred_or_probe_on_a_reachable_manager_still_refuses(
    job, tmp_path, fail, message
):
    """CAMP-1t: only the manager's own `absent` answer skips the shred."""
    fake, _, _ = await owned_world(job)
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match=message):
            await deploy.teardown(
                cloud, job.inventory, [clean_report(tmp_path)], shell=Shell(fail=(fail,))
            )
    assert not fake.deleted and "secrets_shredded" not in inventory(job)


async def test_a_refused_release_is_reported_and_the_rest_still_go(job, tmp_path):
    fake, saved, images = await owned_world(job)
    fake.resources["ssh_key"][saved["worker_key_id"]]["name"] = "production-key"
    async with running(fake) as cloud:
        done = await deploy.teardown(
            cloud,
            job.inventory,
            [clean_report(tmp_path)],
            shell=Shell(),
            remove_agent=lambda: False,
        )
    results = {(r["kind"], r["id"]): r["result"] for r in done["released"]}
    assert results[("ssh_key", saved["worker_key_id"])] == "failed: OwnershipError"
    assert (
        saved["worker_key_id"] in fake.resources["ssh_key"]
        and "9001" not in (fake.resources["snapshot"])
    )
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
        done = await deploy.teardown(
            cloud, job.inventory, [], shell=shell, remove_agent=lambda: True
        )
    assert shell.commands() == [deploy.PROBE, deploy.SHRED] and done["clean"] is True


async def test_a_manager_that_never_recorded_a_campaign_needs_no_report(job, tmp_path):
    """CAMP-1s: no `campaigns` key at all (a manager that never ran one) needs no report."""
    fake, saved, _ = await owned_world(job)
    saved.pop("campaigns")
    deploy.save(job.inventory, saved)
    async with running(fake) as cloud:
        done = await deploy.teardown(
            cloud,
            job.inventory,
            [],
            keep_worker_assets=True,
            shell=Shell(),
            remove_agent=lambda: True,
        )
    assert done["clean"] is True and fake.deleted == [
        ("droplet", saved["manager_id"]),
        ("firewall", saved["manager_firewall_id"]),
    ]


@pytest.mark.parametrize("campaigns", [None, [], [CID]])
async def test_a_report_for_an_unrecorded_campaign_is_refused(job, tmp_path, campaigns):
    """CAMP-1s: a clean report proves nothing about this manager unless it records that
    campaign, even when no campaign is recorded at all."""
    fake, saved, _ = await owned_world(job)
    if campaigns is None:
        saved.pop("campaigns")
    deploy.save(job.inventory, saved, **({} if campaigns is None else {"campaigns": campaigns}))
    reports = [clean_report(tmp_path, cid) for cid in [*(campaigns or []), OTHER_CID]]
    shell = Shell()
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match="for no recorded campaign"):
            await deploy.teardown(cloud, job.inventory, reports, shell=shell)
    assert shell.calls == [] and not fake.deleted


async def test_the_host_key_file_is_found_beside_the_inventory_from_any_directory(
    job, tmp_path, monkeypatch
):
    """CAMP-1s: a relative `known_hosts` record never reaches the current directory's file
    (another manager's), neither for the shred step's SSH nor when the manager retires."""
    fake, saved, _ = await owned_world(job)
    deploy.save(job.inventory, saved, known_hosts="secrets/campaign-manager.known_hosts")
    own = job.inventory.with_name("campaign-manager.known_hosts")
    own.write_text("this manager's host key\n")
    elsewhere = tmp_path / "elsewhere"
    other = elsewhere / "secrets" / "campaign-manager.known_hosts"
    other.parent.mkdir(parents=True)
    other.write_text("another manager's host key\n")
    monkeypatch.chdir(elsewhere)
    shell = Shell()
    async with running(fake) as cloud:
        done = await deploy.teardown(
            cloud,
            job.inventory,
            [clean_report(tmp_path)],
            keep_worker_assets=True,
            shell=shell,
            remove_agent=lambda: True,
        )
    assert done["clean"] is True and not own.exists() and other.exists()
    assert {known_hosts_option(own)} == {
        arg for argv, _ in shell.calls for arg in argv if arg.startswith("UserKnownHosts")
    }


def test_teardown_removes_the_mac_agent_by_default(monkeypatch):
    from proxy_index.campaign import guard

    homes = []
    monkeypatch.setattr(guard, "remove", lambda home: homes.append(home) or True)
    assert deploy._remove_agent() is True and homes == [Path.home()]


async def test_teardown_on_another_account_refuses_before_any_ssh_or_release(job, tmp_path):
    """B1: a client of another team must not pass the live-worker check or the proof."""
    fake, _, _ = await owned_world(job)
    other, shell = other_team(Fake()), Shell()
    async with running(other) as cloud:
        with pytest.raises(deploy.DeployError, match="account"):
            await deploy.teardown(
                cloud,
                job.inventory,
                [clean_report(tmp_path)],
                shell=shell,
                remove_agent=lambda: True,
            )
    assert shell.calls == [] and not other.deleted and not fake.deleted
    assert writes(other) == [] and "teardown" not in inventory(job)


async def test_deploy_on_another_account_writes_nothing(job):
    """B1: neither another account key nor another team's token touches the inventory."""
    await run_deploy(Fake(), job)
    before = job.inventory.read_bytes()
    for other_job in (replace(job, account="proxy-index"), job):
        other = other_team(Fake())
        with pytest.raises(deploy.DeployError, match="account"):
            await run_deploy(other, other_job)
        assert job.inventory.read_bytes() == before and writes(other) == []


async def test_allow_me_on_another_account_refuses(job):
    await run_deploy(Fake(), job)
    other = other_team(Fake())
    async with running(other) as cloud:
        with pytest.raises(deploy.DeployError, match="account"):
            await deploy.allow_me(cloud, job.inventory, OTHER_IP)
    assert writes(other) == []


async def test_a_rerun_after_a_failed_release_finishes_without_ssh(job, tmp_path):
    """B2: one failed delete is retried alone; the manager is not contacted again."""
    fake, saved, _ = await owned_world(job)
    worker_fw = saved["worker_firewall_id"]
    fake.faults.append(Fault("DELETE", f"/v2/firewalls/{worker_fw}", 422))
    report = clean_report(tmp_path)
    async with running(fake) as cloud:
        first = await deploy.teardown(
            cloud, job.inventory, [report], shell=Shell(), remove_agent=lambda: True
        )
    assert first["clean"] is False and worker_fw in fake.resources["firewall"]
    fake.deleted.clear()
    shell = Shell()
    async with running(fake) as cloud:
        again = await deploy.teardown(
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
        done = await deploy.teardown(
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
        first = await deploy.teardown(
            cloud, job.inventory, [report], shell=Shell(), remove_agent=lambda: True
        )
    results = {(r["kind"], r["id"]): r["result"] for r in first["released"]}
    assert results[("droplet", manager)] == "failed: ApiError"
    assert results[("firewall", manager_fw)].startswith("kept")
    assert manager_fw in fake.resources["firewall"] and first["clean"] is False
    shell = Shell()
    async with running(fake) as cloud:
        again = await deploy.teardown(
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
            await deploy.teardown(cloud, job.inventory, [report], shell=Shell())
    assert not fake.deleted


async def test_a_stray_unassigned_manager_keeps_the_teardown_unclean(job, tmp_path):
    """m1: a manager the guard could not remove at deploy time is listed, never forgotten."""
    fake, saved, _ = await owned_world(job)
    stray = fake.add("pi-camp-manager-20270101-beef", [MANAGER_TAG])
    deploy.save(job.inventory, saved, unassigned_managers=[stray, "999999"])
    async with running(fake) as cloud:
        done = await deploy.teardown(
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
        done = await deploy.teardown(
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
    assert not any(key in after for key in deploy.RESET)
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
            await deploy.teardown(cloud, job.inventory, [], keep_worker_assets=True)


async def test_the_next_manager_reuses_the_worker_firewall_and_a_full_teardown_ends_all(
    job, tmp_path
):
    fake, saved, _ = await owned_world(job)
    report = clean_report(tmp_path)
    async with running(fake) as cloud:
        await deploy.teardown(
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
            await deploy.teardown(cloud, job.inventory, [report], shell=shell)
        done = await deploy.teardown(
            cloud, job.inventory, [], shell=shell, remove_agent=lambda: True
        )
    assert shell.commands()[-1] == deploy.SHRED
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
        first = await deploy.teardown(
            cloud,
            job.inventory,
            [report],
            keep_worker_assets=True,
            shell=Shell(),
            remove_agent=lambda: True,
        )
    assert first["clean"] is False and inventory(job)["manager_id"] == saved["manager_id"]
    async with running(fake) as cloud:
        again = await deploy.teardown(
            cloud,
            job.inventory,
            [report],
            keep_worker_assets=True,
            shell=Shell(),
            remove_agent=lambda: True,
        )
    assert again["clean"] is True and "manager_id" not in inventory(job)
    assert saved["worker_firewall_id"] in fake.resources["firewall"]
