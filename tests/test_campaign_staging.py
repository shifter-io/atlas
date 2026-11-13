"""CAMP-STAGING-1: a staging campaign manager beside production on one account (fakes only).

Tags, names, specs and the guard's environment checks. Staging has its own tags, firewalls and
keys (`pi-camp-stg-`); production's names are unchanged. Every test runs with production's
resources in the fake account beside staging's. The deploy, teardown and launch tests are in
`test_campaign_staging_deploy.py`.
"""

from __future__ import annotations

import json
import re

import pytest
from campaign_deploy_fakes import MAC_IP, MAC_PUBLIC
from campaign_fakes import CAMPAIGN_PROJECT, MANAGER_ID, SID, FakeDigitalOcean, running
from campaign_hour_cases import make_spec
from campaign_staging_cases import (
    DAY,
    FIREWALL,
    NAME,
    PRODUCTION_OURS,
    STAGING_MAC,
    STAGING_MANAGER_TAG,
    STAGING_OURS,
    STAGING_WORKER_TAG,
    quick_assign,  # noqa: F401  (autouse)
    scope_of,
)

from atlas.campaign import contracts, do_api, ops_api
from atlas.campaign.contracts import (
    MANAGER_NAME_RE,
    MANAGER_TAG,
    STAGING_MANAGER_NAME_RE,
    WORKER_TAG,
    CampaignSpec,
)
from atlas.campaign.do_api import ScopeError
from atlas.campaign.scope import Ledger, OwnershipError, guarded_delete, scope_for


def test_each_environment_has_its_own_tags_and_names():
    assert contracts.tags("production") == {
        "worker": "pi-camp-worker",
        "manager": "pi-camp-manager",
        "builder": "pi-camp-builder",
    }
    assert contracts.tags("staging") == {
        "worker": STAGING_WORKER_TAG,
        "manager": STAGING_MANAGER_TAG,
        "builder": "pi-camp-stg-builder",
    }
    staging, production = contracts.names("staging"), contracts.names("production")
    assert [staging.firewall(r) for r in ("manager", "worker")] == [
        "pi-camp-stg-manager-fw",
        "pi-camp-stg-worker-fw",
    ]
    assert [production.firewall(r) for r in ("manager", "worker")] == [
        "pi-camp-manager-fw",
        "pi-camp-worker-fw",
    ]
    manager = staging.dated("manager", DAY, "ab12")
    assert manager == "pi-camp-stg-manager-20270101-ab12"
    assert re.fullmatch(STAGING_MANAGER_NAME_RE, manager)
    assert not re.fullmatch(MANAGER_NAME_RE, manager)
    assert not re.fullmatch(STAGING_MANAGER_NAME_RE, production.dated("manager", DAY, "ab12"))
    assert (staging.dated("mac", DAY), production.dated("worker", DAY)) == (
        "pi-camp-stg-mac-20270101",
        "pi-camp-worker-20270101",
    )
    found = [
        contracts.environment_of(name)
        for name in ("pi-camp-stg-mac-20270101", "pi-camp-mac-20270101", f"pi-camp-{SID}", "web")
    ]
    assert found == ["staging", "production", "production", None]
    for bad in ("prod", None, "Staging"):
        with pytest.raises(ValueError, match="environment must be one of production, staging"):
            contracts.tags(bad)


def test_a_production_spec_is_written_as_before_and_a_staging_one_records_it():
    production, staging = make_spec(), make_spec(environment="staging")
    assert "environment" not in json.loads(production.to_json())
    assert json.loads(staging.to_json())["environment"] == "staging"
    assert CampaignSpec.from_json(staging.to_json()) == staging
    assert CampaignSpec.from_json(production.to_json()).environment == "production"
    assert (scope_for(staging).environment, scope_for(production).environment) == (
        "staging",
        "production",
    )
    with pytest.raises(ValueError, match="environment"):
        make_spec(environment="qa")


@pytest.mark.parametrize(
    ("environment", "tags", "failed"),
    [
        ("staging", STAGING_OURS, None),
        ("staging", PRODUCTION_OURS, ["tags"]),  # production's worker, by a staging delete
        ("staging", [*STAGING_OURS, WORKER_TAG], ["tags"]),
        ("production", STAGING_OURS, ["tags"]),
        ("production", [*PRODUCTION_OURS, STAGING_WORKER_TAG], ["tags"]),
    ],
)
async def test_a_worker_is_removed_only_by_its_own_environments_scope(
    tmp_path, environment, tags, failed
):
    fake = FakeDigitalOcean()
    fake.seed_production(20)
    target = fake.add(NAME, tags, project=CAMPAIGN_PROJECT)
    ledger, audit = Ledger({"droplet": {target}}), tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        call = guarded_delete(cloud, "droplet", target, scope_of(environment), ledger, audit)
        if failed is None:
            assert await call == "deleted" and fake.deleted == [("droplet", target)]
            return
        with pytest.raises(OwnershipError) as refused:
            await call
    assert refused.value.failed == failed and target in fake.droplets and not fake.deleted


@pytest.mark.parametrize(
    ("name", "tag", "environment", "failed"),
    [
        ("pi-camp-stg-manager-20270101-ab12", STAGING_MANAGER_TAG, "staging", None),
        ("pi-camp-manager-20270101-ab12", MANAGER_TAG, "staging", ["tags", "name"]),
        ("pi-camp-stg-manager-20270101-ab12", MANAGER_TAG, "staging", ["tags"]),
        ("pi-camp-stg-manager-20270101-ab12", STAGING_MANAGER_TAG, "production", ["tags", "name"]),
    ],
)
async def test_the_manager_role_follows_the_scopes_environment(
    tmp_path, name, tag, environment, failed
):
    fake = FakeDigitalOcean()
    fake.add(name, [tag], project=CAMPAIGN_PROJECT, droplet_id=MANAGER_ID)
    scope = ops_api.ops_scope(CAMPAIGN_PROJECT, DAY, MANAGER_ID, environment)
    ledger, audit = Ledger({"droplet": {MANAGER_ID}}), tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        call = guarded_delete(cloud, "droplet", MANAGER_ID, scope, ledger, audit, "manager")
        if failed is None:
            assert await call == "deleted"
            return
        with pytest.raises(OwnershipError) as refused:
            await call
    assert refused.value.failed == failed and MANAGER_ID in fake.droplets


@pytest.mark.parametrize(
    ("environment", "kind", "rid", "values", "failed"),
    [
        ("staging", "ssh_key", "41", {"name": "pi-camp-stg-mac-20270101"}, None),
        ("staging", "ssh_key", "41", {"name": "pi-camp-mac-20270101"}, ["name"]),
        ("staging", "snapshot", "42", {"name": "pi-camp-worker-0.9.5-abc-20270101"}, ["name"]),
        (
            "staging",
            "firewall",
            FIREWALL,
            {"name": "pi-camp-stg-worker-fw", "tags": [STAGING_WORKER_TAG]},
            None,
        ),
        (
            "staging",
            "firewall",
            FIREWALL,
            {"name": "pi-camp-worker-fw", "tags": [WORKER_TAG]},
            ["name", "targets"],
        ),
        (
            "staging",
            "firewall",
            FIREWALL,
            {"name": "pi-camp-stg-worker-fw", "tags": [WORKER_TAG]},
            ["targets"],
        ),
        ("production", "ssh_key", "41", {"name": "pi-camp-stg-worker-20270101"}, ["name"]),
        (
            "production",
            "firewall",
            FIREWALL,
            {"name": "pi-camp-worker-fw", "tags": [STAGING_WORKER_TAG]},
            ["targets"],
        ),
    ],
)
async def test_keys_firewalls_and_snapshots_are_removed_only_by_their_environment(
    tmp_path, environment, kind, rid, values, failed
):
    fake = FakeDigitalOcean()
    fake.add_resource(kind, rid, **values)
    ledger, audit = Ledger({kind: {rid}}), tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        call = guarded_delete(cloud, kind, rid, scope_of(environment), ledger, audit, "manager")
        if failed is None:
            assert await call == "deleted" and fake.deleted == [(kind, rid)]
            return
        with pytest.raises(OwnershipError) as refused:
            await call
    assert refused.value.failed == failed and not fake.deleted


def test_a_create_carries_exactly_one_role_tag_of_one_environment():
    do_api.check_create([NAME], STAGING_OURS, ["1"])
    do_api.check_create(["pi-camp-stg-manager-20270101-ab12"], [STAGING_MANAGER_TAG], ["1"])
    for names, tags, message in (
        ([NAME], [*STAGING_OURS, WORKER_TAG], "exactly one role tag"),
        ([NAME], [STAGING_WORKER_TAG], "campaign tag of its names"),
        (["pi-camp-manager-20270101-ab12"], [STAGING_MANAGER_TAG], "role's pattern"),
        (["pi-camp-stg-manager-20270101-ab12"], [MANAGER_TAG], "role's pattern"),
    ):
        with pytest.raises(ScopeError, match=message):
            do_api.check_create(names, tags, ["1"])


def test_a_firewall_names_only_its_own_environments_role_tags():
    def body(name, tags):
        return {"name": name, "inbound_rules": [], "outbound_rules": [], "tags": tags}

    assert do_api._firewall(body("pi-camp-stg-worker-fw", [STAGING_WORKER_TAG]))
    assert do_api._firewall(body("pi-camp-worker-fw", [WORKER_TAG]))
    assert not do_api._firewall(body("pi-camp-stg-worker-fw", [WORKER_TAG]))  # found by #166
    assert not do_api._firewall(body("pi-camp-worker-fw", [STAGING_WORKER_TAG]))
    assert not do_api._firewall(body("deploy-fw", []))


async def test_a_staging_firewall_never_attaches_to_a_production_droplet():
    fake = FakeDigitalOcean()
    production = fake.add("pi-camp-manager-20270101-ab12", [MANAGER_TAG], CAMPAIGN_PROJECT)
    staging = fake.add("pi-camp-stg-manager-20270101-cd34", [STAGING_MANAGER_TAG], CAMPAIGN_PROJECT)
    async with running(fake) as cloud:
        rules = ops_api.manager_firewall(MAC_IP, staging, "staging")
        assert (await cloud.create_firewall(rules, CAMPAIGN_PROJECT))["name"] == (
            "pi-camp-stg-manager-fw"
        )
        for wrong in (
            ops_api.manager_firewall(MAC_IP, production, "staging"),
            ops_api.manager_firewall(MAC_IP, staging),  # production's firewall, staging's manager
        ):
            with pytest.raises(ScopeError, match="not the campaign's"):
                await cloud.create_firewall(wrong, CAMPAIGN_PROJECT)
        wrong = ops_api.worker_firewall(MAC_IP, "staging") | {"tags": [WORKER_TAG]}
        with pytest.raises(ScopeError, match="outside the campaign's scope"):
            await cloud.create_firewall(wrong, CAMPAIGN_PROJECT)
    assert len(fake.resources["firewall"]) == 1
    assert ops_api.worker_firewall(MAC_IP, "staging")["tags"] == [STAGING_WORKER_TAG]


async def test_a_key_registered_for_another_environment_is_never_reused():
    fake = FakeDigitalOcean()
    fake.add_resource("ssh_key", 41, name="pi-camp-mac-20270101", public_key=MAC_PUBLIC)
    async with running(fake) as cloud:
        with pytest.raises(ops_api.OpsError, match="registered as pi-camp-mac-20270101"):
            await ops_api.ensure_ssh_key(cloud, "pi-camp-stg-mac-20270102", MAC_PUBLIC)
        assert await ops_api.ensure_ssh_key(cloud, "pi-camp-mac-20270102", MAC_PUBLIC) == (
            "41",
            "pi-camp-mac-20270101",
            False,
        )
        _, name, created = await ops_api.ensure_ssh_key(
            cloud, "pi-camp-stg-mac-20270102", STAGING_MAC
        )
    assert name == "pi-camp-stg-mac-20270102" and created
