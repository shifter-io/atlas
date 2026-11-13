"""CAMP-STAGING-1: a staging deploy, teardown and launch beside production (fakes only).

The staging manager is deployed, torn down and launches workers with its own tags, firewalls
and keys (`pi-camp-stg-`), while production's resources stay exactly as they were. The tag,
name and guard tests are in `test_campaign_staging.py`.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import replace

import pytest
from campaign_deploy_fakes import (
    Fake,
    Shell,
    inventory,
    make_job,
    owned_world,
    parse,
    run_deploy,
    writes,
)
from campaign_fakes import CAMPAIGN_PROJECT, CID, SID, FakeDigitalOcean, running
from campaign_hour_cases import launched, make_spec
from campaign_probe_cases import FakeProbeRemote, probed, world
from campaign_staging_cases import (
    STAGING_MANAGER_TAG,
    STAGING_WORKER_TAG,
    StagingShell,
    quick_assign,  # noqa: F401  (autouse)
    staging_job,
)

from atlas.campaign import deploy, ops_api, teardown
from atlas.campaign.contracts import (
    STAGING_MANAGER_NAME_RE,
    WORKER_TAG,
    campaign_tag,
    hour_tag,
    spec_path,
)
from atlas.campaign.probe import ProbeError


async def test_a_staging_deploy_creates_only_staging_resources_beside_production(tmp_path):
    production = make_job(tmp_path)
    fake, _, _ = await owned_world(production)
    before = copy.deepcopy({"droplet": fake.droplets, **fake.resources})
    sent, bodies = len(fake.requests), len(fake.bodies)
    staging = staging_job(production)
    result = await run_deploy(fake, staging, StagingShell())
    recorded = inventory(staging)
    assert recorded["environment"] == "staging"
    assert inventory(production)["environment"] == "production"
    assert re.fullmatch(STAGING_MANAGER_NAME_RE, result["manager_name"])
    assert fake.droplets[result["manager_id"]]["tags"] == [STAGING_MANAGER_TAG]
    firewalls = fake.resources["firewall"]
    manager, worker = (
        firewalls[recorded[k]] for k in ("manager_firewall_id", "worker_firewall_id")
    )
    assert (manager["name"], manager["droplet_ids"]) == (
        "pi-camp-stg-manager-fw",
        [int(result["manager_id"])],
    )
    assert (worker["name"], worker["tags"]) == ("pi-camp-stg-worker-fw", [STAGING_WORKER_TAG])
    keys = [fake.resources["ssh_key"][recorded[k]]["name"] for k in ("mac_key_id", "worker_key_id")]
    assert keys == ["pi-camp-stg-mac-20270101", "pi-camp-stg-worker-20270101"]
    for kind, items in before.items():  # every production resource exactly as it was
        current = fake.droplets if kind == "droplet" else fake.resources[kind]
        assert {key: current.get(key) for key in items} == items
    assert {m for m, *_ in fake.requests[sent:]} == {"GET", "POST"}  # no PUT, no DELETE
    for _, _, body in fake.bodies[bodies:]:
        assert all(n.startswith("pi-camp-stg-") for n in body.get("names") or [body["name"]])
        assert set(body.get("tags") or []) <= {STAGING_WORKER_TAG, STAGING_MANAGER_TAG}


async def test_each_environment_keeps_its_own_inventory(tmp_path):
    production, fake = make_job(tmp_path), Fake()
    await run_deploy(fake, production)
    legacy = inventory(production)
    first = (legacy["manager_name"], legacy["manager_firewall_id"], legacy["worker_key_name"])
    del legacy["environment"]  # an inventory written before environments: production's
    production.inventory.write_text(json.dumps(legacy))
    await run_deploy(fake, production)
    again = inventory(production)
    assert (again["manager_name"], again["manager_firewall_id"], again["worker_key_name"]) == first
    assert again["environment"] == "production"
    staging, done = staging_job(production), len(writes(fake))
    with pytest.raises(deploy.DeployError, match="production manager's, not staging"):
        await run_deploy(fake, replace(production, environment="staging"), StagingShell())
    with pytest.raises(ops_api.OpsError, match="registered as pi-camp-mac-20270101"):
        await run_deploy(fake, replace(staging, mac_key=production.mac_key), StagingShell())
    assert len(writes(fake)) == done and not staging.inventory.exists()
    await run_deploy(fake, staging, StagingShell())
    with pytest.raises(deploy.DeployError, match="staging manager's, not production"):
        await run_deploy(fake, replace(staging, environment="production"))


async def test_a_staging_teardown_is_clean_beside_a_running_production_manager(tmp_path):
    production = make_job(tmp_path)
    fake, saved, _ = await owned_world(production)  # production's manager, snapshot, campaign
    worker = fake.add(
        f"pic-{SID}-h00-lon1-shifter",
        [WORKER_TAG, f"pi-camp-{SID}", f"pi-camp-{SID}-h00"],
        CAMPAIGN_PROJECT,
    )  # a live production worker: production's teardown would refuse
    staging = staging_job(production)
    await run_deploy(fake, staging, StagingShell())
    recorded, kept, removed = inventory(staging), copy.deepcopy(fake.resources), []
    async with running(fake) as cloud:
        done = await teardown.teardown(
            cloud, staging.inventory, [], shell=Shell(), remove_agent=lambda: removed.append(1)
        )
    assert done["clean"] is True and done["agent_removed"] is False and removed == []
    assert done["tag_counts"] == {
        STAGING_WORKER_TAG: 0,
        STAGING_MANAGER_TAG: 0,
        "pi-camp-stg-builder": 0,
    }
    assert fake.deleted == [
        ("droplet", recorded["manager_id"]),
        ("firewall", recorded["manager_firewall_id"]),
        ("firewall", recorded["worker_firewall_id"]),
        ("ssh_key", recorded["worker_key_id"]),
        ("ssh_key", recorded["mac_key_id"]),
    ]
    assert {saved["manager_id"], worker} <= set(fake.droplets)
    for key in (saved["manager_firewall_id"], saved["worker_firewall_id"]):
        assert fake.resources["firewall"][key] == kept["firewall"][key]
    assert {saved["mac_key_id"], saved["worker_key_id"]} <= set(fake.resources["ssh_key"])
    assert fake.resources["snapshot"] == kept["snapshot"]  # the shared image stays


async def test_a_staging_teardown_keeping_worker_assets_keeps_only_its_own(tmp_path):
    production = make_job(tmp_path)
    fake, _, _ = await owned_world(production)
    staging = staging_job(production)
    await run_deploy(fake, staging, StagingShell())
    recorded = inventory(staging)
    async with running(fake) as cloud:
        done = await teardown.teardown(
            cloud, staging.inventory, [], keep_worker_assets=True, shell=Shell()
        )
    assert done["clean"] is True and fake.deleted == [
        ("droplet", recorded["manager_id"]),
        ("firewall", recorded["manager_firewall_id"]),
    ]
    worker = fake.resources["firewall"][recorded["worker_firewall_id"]]
    assert worker["tags"] == [STAGING_WORKER_TAG]  # never on production's workers
    assert "manager_id" not in inventory(staging) and inventory(staging)["environment"] == "staging"


async def test_a_staging_teardown_refuses_productions_resources_even_when_recorded(tmp_path):
    production = make_job(tmp_path)
    fake, saved, _ = await owned_world(production)
    staging = staging_job(production)
    await run_deploy(fake, staging, StagingShell())
    recorded = inventory(staging)
    ids = ("manager_id", "manager_firewall_id", "worker_firewall_id", "worker_key_id", "mac_key_id")
    deploy.save(staging.inventory, recorded, secrets_shredded=True, **{k: saved[k] for k in ids})
    async with running(fake) as cloud:
        done = await teardown.teardown(cloud, staging.inventory, [], shell=Shell())
    assert done["clean"] is False and fake.deleted == []
    assert [r["result"] for r in done["released"]] == [
        "failed: OwnershipError",
        "kept: the manager remains",
        "failed: OwnershipError",
        "failed: OwnershipError",
        "failed: OwnershipError",
    ]


async def test_a_staging_campaign_launches_staging_workers(tmp_path):
    fake = FakeDigitalOcean()
    fake.seed_production(20)
    record, *_ = await launched(tmp_path, fake, make_spec(environment="staging"))
    assert {d.status for d in record.droplets} == {"armed"}
    tags = [d["tags"] for d in fake.droplets.values() if d["name"].startswith("pic-")]
    assert len(tags) == len(record.droplets)
    assert all(t == [STAGING_WORKER_TAG, campaign_tag(CID), hour_tag(CID, 0)] for t in tags)


async def test_a_probe_refuses_a_staging_campaign(tmp_path):
    fake = FakeDigitalOcean()
    inputs, spec = world(tmp_path)
    spec_path(inputs.root).write_text(replace(spec, environment="staging").to_json())
    with pytest.raises(ProbeError, match="production's workers only"):
        await probed(fake, FakeProbeRemote({}), inputs)
    assert fake.requests == []


def test_the_deploy_cli_takes_the_environment():
    argv = ["campaign", "manager", "deploy", "--account", "leftclick", "--wheel", "w"]
    argv += ["--requirements", "r"]
    assert parse(argv).environment == "production"
    assert parse([*argv, "--environment", "staging"]).environment == "staging"
    with pytest.raises(SystemExit):
        parse([*argv, "--environment", "qa"])
