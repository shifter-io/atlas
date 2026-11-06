"""CAMP-STAGING-1: a staging campaign manager beside production on one account (fakes only).

Staging has its own tags, firewalls and keys (`pi-camp-stg-`); production's names are
unchanged. Every test runs with production's resources in the fake account beside staging's.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from campaign_deploy_fakes import (
    MAC_IP,
    MAC_PUBLIC,
    WORKER_PUBLIC,
    Fake,
    Shell,
    fast_assign,
    inventory,
    make_job,
    owned_world,
    parse,
    run_deploy,
    writes,
)
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    CID,
    MANAGER_ID,
    SID,
    SPEC_TIME,
    FakeDigitalOcean,
    running,
)
from campaign_hour_cases import launched, make_spec
from campaign_probe_cases import FakeProbeRemote, probed, world

from proxy_index.campaign import contracts, deploy, do_api, ops_api, teardown
from proxy_index.campaign.contracts import (
    MANAGER_NAME_RE,
    MANAGER_TAG,
    STAGING_MANAGER_NAME_RE,
    WORKER_TAG,
    CampaignSpec,
    Scope,
    campaign_tag,
    hour_tag,
    spec_path,
)
from proxy_index.campaign.do_api import ScopeError
from proxy_index.campaign.probe import ProbeError
from proxy_index.campaign.scope import Ledger, OwnershipError, guarded_delete, scope_for

STAGING_WORKER_TAG, STAGING_MANAGER_TAG = "pi-camp-stg-worker", "pi-camp-stg-manager"
STAGING_MAC = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIStagingMacKeyForTests pi-camp-stg-mac"
STAGING_WORKER = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIStagingWorkerKeyForTests"
NAME = f"pic-{SID}-h03-lon1-shifter"
STAGING_OURS = [STAGING_WORKER_TAG, f"pi-camp-{SID}", f"pi-camp-{SID}-h03"]
PRODUCTION_OURS = [WORKER_TAG, f"pi-camp-{SID}", f"pi-camp-{SID}-h03"]
FIREWALL = "33333333-3333-4333-8333-333333333333"
DAY = datetime(2027, 1, 1, 21, 55, tzinfo=UTC)


@pytest.fixture(autouse=True)
def quick_assign(monkeypatch):
    fast_assign(monkeypatch)


def scope_of(environment: str) -> Scope:
    return Scope(
        CID, SID, CAMPAIGN_PROJECT, MANAGER_ID, datetime.fromisoformat(SPEC_TIME), environment
    )


class StagingShell(Shell):
    """The staging manager generates its own worker key on its own disk."""

    async def __call__(self, argv, stdin):
        code, out = await super().__call__(argv, stdin)
        return code, out.replace(WORKER_PUBLIC, STAGING_WORKER)


def staging_job(production):
    """The staging deploy: its own inventory beside production's and its own Mac key."""
    key = production.mac_key.with_name("proxy-index-campaign-staging")
    key.write_text("private part, never read by deploy\n")
    key.with_name(key.name + ".pub").write_text(STAGING_MAC + "\n")
    staging = production.inventory.with_name("campaign-manager-staging.json")
    return replace(production, inventory=staging, mac_key=key, environment="staging")


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
