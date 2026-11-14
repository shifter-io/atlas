"""MANAGER-REGION-1: the manager's `--region`, default lon1, recorded and never moved (fakes only).

A new manager is created in `--region` (else the inventory's `manager_region`, else lon1), its
size must be offered there, and the region is recorded. A recorded manager is never moved: another
region is refused until `teardown --keep-worker-assets` retires it.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from campaign_deploy_fakes import MAC_IP, Fake, inventory, make_job, parse, run_deploy, writes
from campaign_fakes import TOKEN, running
from campaign_staging_cases import (
    STAGING_MANAGER_TAG,
    StagingShell,
    quick_assign,  # noqa: F401  (autouse)
    staging_job,
)
from manager_orch_cases import deployed

from atlas.campaign import deploy, manager_orch


@pytest.fixture
def job(tmp_path):
    return make_job(tmp_path)


def tor1_fake():
    """The fake with the default manager size offered in tor1 too (s-2vcpu-4gb stays lon1/fra1)."""
    fake = Fake()
    next(s for s in fake.sizes if s["slug"] == deploy.MANAGER_SIZE)["regions"].append("tor1")
    return fake


def created(fake):
    return [(b["region"], b["size"]) for _, p, b in fake.bodies if p == "/v2/droplets"]


def forget_region(job):
    saved = inventory(job)
    saved.pop("manager_region")
    deploy.save(job.inventory, saved)


async def test_a_staging_deploy_in_tor1_creates_the_manager_there_and_records_it(job):
    fake, staging = tor1_fake(), replace(staging_job(job), region="tor1")
    result = await run_deploy(fake, staging, StagingShell())
    assert created(fake) == [("tor1", deploy.MANAGER_SIZE)]
    assert fake.droplets[result["manager_id"]]["tags"] == [STAGING_MANAGER_TAG]
    assert inventory(staging)["manager_region"] == "tor1"
    await run_deploy(fake, replace(staging, region=None), StagingShell())  # the recorded one
    assert len(created(fake)) == 1 and inventory(staging)["manager_region"] == "tor1"


async def test_a_size_not_offered_in_tor1_is_refused_before_any_write(job):
    fake = tor1_fake()
    with pytest.raises(deploy.DeployError, match="size s-2vcpu-4gb is not offered in tor1"):
        await run_deploy(fake, replace(job, region="tor1", size="s-2vcpu-4gb"))
    assert writes(fake) == [] and not job.inventory.exists()


async def test_the_default_region_is_lon1_and_recorded(job):
    fake = Fake()
    await run_deploy(fake, job)
    assert created(fake) == [("lon1", deploy.MANAGER_SIZE)]
    assert inventory(job)["manager_region"] == "lon1" == deploy.MANAGER_REGION


async def test_an_inventory_without_the_region_keeps_lon1(job):
    """Production's inventory has no `manager_region`: its manager counts as lon1."""
    fake = tor1_fake()
    await run_deploy(fake, job)
    forget_region(job)
    await run_deploy(fake, job)
    assert created(fake) == [("lon1", deploy.MANAGER_SIZE)]
    assert inventory(job)["manager_region"] == "lon1"
    forget_region(job)
    before, sent = job.inventory.read_text(), len(fake.requests)
    with pytest.raises(deploy.DeployError, match="recorded manager is in lon1"):
        await run_deploy(fake, replace(job, region="tor1"))
    assert job.inventory.read_text() == before
    assert [p for m, p, _, _ in fake.requests[sent:] if m != "GET" or p == "/v2/sizes"] == []


async def test_another_region_than_the_recorded_one_is_refused(job):
    fake = tor1_fake()
    await run_deploy(fake, replace(job, region="tor1"))
    before, posts = job.inventory.read_text(), len(writes(fake))
    with pytest.raises(deploy.DeployError) as refused:
        await run_deploy(fake, replace(job, region="lon1"))
    message = str(refused.value)
    assert "tor1" in message and "lon1" in message and "--keep-worker-assets" in message
    assert job.inventory.read_text() == before and len(writes(fake)) == posts
    await run_deploy(fake, replace(job, region="tor1"))  # the same region is no change
    assert created(fake) == [("tor1", deploy.MANAGER_SIZE)]


def test_a_retired_manager_lets_the_next_one_choose_its_region():
    """After `teardown --keep-worker-assets` (no `manager_id`) the recorded region is only the
    default."""
    retired = {"manager_region": "tor1", "manager_id": None}
    assert deploy.manager_region(retired, None) == "tor1"
    assert deploy.manager_region(retired, "lon1") == "lon1"
    assert deploy.manager_region({}, None) == "lon1"


async def test_resize_checks_the_size_in_the_recorded_region(tmp_path):
    job = make_job(tmp_path, account="atlas")
    fake = await deployed(job)
    deploy.save(job.inventory, inventory(job), manager_region="tor1")
    before = inventory(job)
    async with running(fake) as cloud:
        with pytest.raises(deploy.DeployError, match="is not offered in tor1"):
            await manager_orch.resize(cloud, job.inventory, apply=True)
    assert inventory(job) == before


async def test_the_cli_passes_the_region_to_the_deploy(monkeypatch, tmp_path):
    from atlas.campaign import cli as campaign_cli
    from atlas.campaign import do_api, image

    seen = []

    async def fake_deploy(cloud, job):
        seen.append(job)
        return {}

    monkeypatch.setattr(deploy, "deploy", fake_deploy)
    monkeypatch.setattr(do_api, "keychain_token", lambda account: lambda: TOKEN)
    monkeypatch.setattr(image, "ensure_mac_key", lambda path: path)
    monkeypatch.setattr(deploy, "credential_files", lambda path, networks: {})
    common = ["campaign", "manager", "deploy", "--account", "leftclick"]
    common += ["--wheel", "w.whl", "--requirements", "r.txt"]
    common += ["--inventory", str(tmp_path / "campaign-manager.json")]
    assert parse(common).region is None
    await campaign_cli._deploy(parse([*common, "--region", "tor1"]), None, MAC_IP)
    assert [j.region for j in seen] == ["tor1"]
