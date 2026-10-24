"""CAMP-1m: the manager's `--size`, checked as offered in lon1 and recorded (fakes only)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from campaign_deploy_fakes import Fake, fast_assign, inventory, make_job, parse, run_deploy, writes
from campaign_fakes import Fault

from proxy_index.campaign import deploy
from proxy_index.campaign.do_api import CreateError


@pytest.fixture(autouse=True)
def quick_assign(monkeypatch):
    fast_assign(monkeypatch)


@pytest.fixture
def job(tmp_path):
    return make_job(tmp_path)


def created_sizes(fake):
    return [b["size"] for _, p, b in fake.bodies if p == "/v2/droplets"]


async def test_the_default_size_is_unchanged_and_recorded(job):
    fake = Fake()
    result = await run_deploy(fake, job)
    assert created_sizes(fake) == ["s-8vcpu-16gb"] == [deploy.MANAGER_SIZE]
    assert inventory(job)["manager_size"] == result["manager_size"] == "s-8vcpu-16gb"


async def test_a_smaller_offered_size_creates_the_manager_with_it(job):
    fake = Fake()
    result = await run_deploy(fake, replace(job, size="s-2vcpu-4gb"))
    assert created_sizes(fake) == ["s-2vcpu-4gb"]
    assert inventory(job)["manager_size"] == result["manager_size"] == "s-2vcpu-4gb"


@pytest.mark.parametrize("size, regions", [
    ("s-2vcpu-4gb", ["fra1"]),  # offered, but not in lon1
    ("s-1vcpu-1gb", ["lon1"]),  # listed in lon1, but not available
    ("s-64vcpu-9gb", None),  # not a size at all
])
async def test_a_size_not_offered_in_lon1_is_refused_before_any_write(job, size, regions):
    fake = Fake()
    if regions is not None:
        next(s for s in fake.sizes if s["slug"] == size)["regions"] = regions
    with pytest.raises(deploy.DeployError, match=f"size {size} is not offered in lon1"):
        await run_deploy(fake, replace(job, size=size))
    assert writes(fake) == [] and not job.inventory.exists()


async def test_a_recorded_manager_keeps_its_size_on_a_rerun(job):
    fake = Fake()
    await run_deploy(fake, replace(job, size="s-2vcpu-4gb"))
    for size in (None, "s-2vcpu-4gb"):
        await run_deploy(fake, replace(job, size=size))
    assert created_sizes(fake) == ["s-2vcpu-4gb"] and inventory(job)["manager_size"] == (
        "s-2vcpu-4gb")
    posts = len(writes(fake))
    with pytest.raises(deploy.DeployError, match="tear it down with --keep-worker-assets"):
        await run_deploy(fake, replace(job, size="s-8vcpu-16gb"))
    assert len(writes(fake)) == posts


def forget_size(job):
    saved = inventory(job)
    saved.pop("manager_size")
    deploy.save(job.inventory, saved)


async def test_a_manager_recorded_before_sizes_counts_as_the_default_size(job):
    fake = Fake()
    await run_deploy(fake, job)
    forget_size(job)
    await run_deploy(fake, job)
    assert inventory(job)["manager_size"] == "s-8vcpu-16gb"
    assert created_sizes(fake) == ["s-8vcpu-16gb"]


async def test_a_new_size_on_a_legacy_manager_is_refused_before_any_write(job):
    """m2: the live O1 manager has no `manager_size`; `--size` before a teardown is refused."""
    fake = Fake()
    await run_deploy(fake, job)
    forget_size(job)
    before, requests = job.inventory.read_text(), len(fake.requests)
    with pytest.raises(deploy.DeployError, match="tear it down with --keep-worker-assets"):
        await run_deploy(fake, replace(job, size="s-2vcpu-4gb"))
    assert job.inventory.read_text() == before
    assert [p for m, p, _, _ in fake.requests[requests:] if m != "GET" or p == "/v2/sizes"] == []


async def test_a_rerun_after_a_failed_create_keeps_the_size_it_asked_for(job):
    """m3: the size of an attempt whose manager was never recorded is kept without --size."""
    fake = Fake()
    fake.faults.append(Fault("POST", "/v2/droplets", 422))
    with pytest.raises(CreateError):
        await run_deploy(fake, replace(job, size="s-2vcpu-4gb"))
    assert "manager_id" not in inventory(job)
    await run_deploy(fake, job)
    assert created_sizes(fake)[-1] == "s-2vcpu-4gb" == inventory(job)["manager_size"]


def test_the_cli_options():
    common = ["campaign", "manager", "deploy", "--account", "leftclick", "--wheel", "w.whl",
              "--requirements", "r.txt"]
    assert parse(common).size is None
    assert parse([*common, "--size", "s-2vcpu-4gb"]).size == "s-2vcpu-4gb"
    teardown = ["campaign", "manager", "teardown", "--after-collect", "c.json"]
    assert parse(teardown).keep_worker_assets is False
    assert parse([*teardown, "--keep-worker-assets"]).keep_worker_assets is True


@pytest.mark.parametrize("flags, keep", [([], False), (["--keep-worker-assets"], True)])
def test_the_teardown_cli_passes_the_keep_flag(monkeypatch, tmp_path, flags, keep):
    from proxy_index.campaign import cli as campaign_cli

    recorded, seen = tmp_path / "campaign-manager.json", []
    recorded.write_text('{"account": "proxy-index"}')

    async def teardown(cloud, path, reports, **kwargs):
        seen.append(kwargs)
        return {"clean": True}

    class NoCloud:
        def __init__(self, account):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(deploy, "teardown", teardown)
    monkeypatch.setattr(campaign_cli, "_cloud", NoCloud)
    args = parse(["campaign", "manager", "teardown", "--after-collect", "r.json", *flags,
                  "--inventory", str(recorded)])
    assert campaign_cli.dispatch(args) == 0 and seen == [{"keep_worker_assets": keep}]
