"""Shared plan world of the `campaign plan` tests (test_campaign_plan.py): recorded credentials,
manager inventory and images, a fake DigitalOcean account and the plan run against it.

The test module loads it with `pytest_plugins` after importing its names, so pytest cannot
rewrite it; it holds no asserts (PYTEST_DONT_REWRITE).
"""

from __future__ import annotations

import json

import pytest
from campaign_deploy_fakes import IMAGE, NOW, PLAN_CREDENTIALS, ROOT, WORKER_PUBLIC
from campaign_fakes import CAMPAIGN_PROJECT, FakeDigitalOcean, running

from atlas.campaign import deploy, planspec
from atlas.campaign.planspec import PlanInputs
from atlas.regions import REGION_IDS


@pytest.fixture
def plan_world(tmp_path):
    credentials = tmp_path / "credentials.toml"
    credentials.write_text(deploy.toml_table("shifter", PLAN_CREDENTIALS).decode())
    credentials.chmod(0o600)
    manager_inventory = tmp_path / "campaign-manager.json"
    manager_inventory.write_text(
        json.dumps(
            {
                "manager_id": "500001",
                "wheel_sha256": "ab" * 32,
                "framework_version": "0.9.5",
                "worker_key_name": "pi-camp-worker-20270101",
                "account": "leftclick",
                "project_id": CAMPAIGN_PROJECT,
                "credentials": ["shifter"],
            }
        )
    )
    images = tmp_path / "campaign-images.json"
    images.write_text(
        json.dumps(
            {
                "builds": [
                    {
                        "snapshot_id": "9001",
                        "snapshot_name": IMAGE,
                        "mmdb_file": "dbip-full.mmdb",
                        "mmdb_sha256": "cd" * 32,
                    }
                ]
            }
        )
    )
    fake = FakeDigitalOcean()
    fake.sizes = [{"slug": "c-2", "available": True, "regions": list(REGION_IDS)}]
    fake.images["9001"] = {"id": 9001, "name": IMAGE, "regions": list(REGION_IDS)}
    fake.add_resource("ssh_key", 77, name="pi-camp-worker-20270101", public_key=WORKER_PUBLIC)
    inputs = PlanInputs(
        "leftclick",
        "2027-01-01T23:00:00+00:00",
        ("shifter",),
        IMAGE,
        tmp_path / "plans",
        credentials,
        manager_inventory,
        images,
        configs=ROOT / "configs",
    )
    return fake, inputs


async def run_plan(fake, inputs):
    async with running(fake) as cloud:
        return await planspec.plan(cloud, inputs, now=NOW)


def with_geonode(inputs):
    """The plan world with Geonode's credentials beside Shifter's, planned together."""
    from dataclasses import replace

    with inputs.credentials.open("a") as stream:
        stream.write(deploy.toml_table("geonode", {"account": "acct_9", "password": "p"}).decode())
    data = json.loads(inputs.inventory.read_text())
    inputs.inventory.write_text(json.dumps(data | {"credentials": ["shifter", "geonode"]}))
    return replace(inputs, networks=("shifter", "geonode"))
