"""PULSE-ROLE-1: the always-on pulse node role. Campaign cleanup never touches a pulse node, and
only `scope.retire_pulse_node` removes one, through the guard (fakes only)."""

from __future__ import annotations

import json
import re
from datetime import datetime

import pytest
from campaign_deploy_fakes import Shell, clean_report, fast_assign, make_job, owned_world
from campaign_fakes import (
    CAMPAIGN_PROJECT,
    CID,
    EARLY_TIME,
    MANAGER_ID,
    PROD_PROJECT,
    SID,
    SPEC_TIME,
    FakeDigitalOcean,
    running,
)

from proxy_index.campaign import contracts, deploy, do_api
from proxy_index.campaign.contracts import (
    BUILDER_TAG,
    MANAGER_TAG,
    PULSE_TAG,
    WORKER_TAG,
    Scope,
)
from proxy_index.campaign.do_api import ScopeError
from proxy_index.campaign.scope import (
    Ledger,
    OwnershipError,
    guarded_delete,
    pulse_scope,
    retire_pulse_node,
)

DEV_PROJECT = "33333333-3333-4333-8333-333333333333"
SCOPE = Scope(CID, SID, CAMPAIGN_PROJECT, MANAGER_ID, datetime.fromisoformat(SPEC_TIME))
OURS = [WORKER_TAG, f"pi-camp-{SID}", f"pi-camp-{SID}-h03"]
NAME = f"pic-{SID}-h03-lon1-shifter"
PULSE_NAME = "pi-pulse-lon1-shifter"
# Pulse near-misses: (key, name, tags, project, failing checks).
PULSE_MISSES = [
    ("in_campaign_project", PULSE_NAME, [PULSE_TAG], CAMPAIGN_PROJECT, ["project"]),
    ("in_production", PULSE_NAME, [PULSE_TAG], PROD_PROJECT, ["project"]),
    ("with_camp_tag", PULSE_NAME, [PULSE_TAG, f"pi-camp-{SID}"], DEV_PROJECT, ["tags"]),
    ("untagged", PULSE_NAME, ["prod"], DEV_PROJECT, ["tags"]),
    ("bad_name", "pulse-lon1", [PULSE_TAG], DEV_PROJECT, ["name"]),
    ("a_worker", NAME, OURS, DEV_PROJECT, ["tags", "name"]),
]


def world():
    fake = FakeDigitalOcean()
    fake.projects[DEV_PROJECT] = set()
    fake.project_names[DEV_PROJECT] = "Dev Resources"
    fake.seed_production(50)
    pulse = {fake.add(f"pi-pulse-{r}-shifter", [PULSE_TAG], project=DEV_PROJECT) for r in "ab"}
    near = {key: fake.add(n, t, project=p) for key, n, t, p, _ in PULSE_MISSES}
    # Every campaign marker, plus the pulse tag: before PULSE-ROLE-1 a worker delete took it.
    near["worker_with_pulse_tag"] = fake.add(NAME, [*OURS, PULSE_TAG], project=CAMPAIGN_PROJECT)
    manager = "pi-camp-manager-20270101-ab12"
    fake.add(manager, [MANAGER_TAG], project=CAMPAIGN_PROJECT, droplet_id=MANAGER_ID)
    builder = fake.add("pi-camp-builder-20270101-cd34", [BUILDER_TAG], project=CAMPAIGN_PROJECT)
    workers = {fake.add(NAME, OURS, project=CAMPAIGN_PROJECT) for _ in range(2)}
    return fake, pulse, near, workers | {MANAGER_ID, builder}


def everything(fake):
    return Ledger({"droplet": set(fake.droplets)})


def test_the_pulse_role_is_its_own_and_the_campaign_roles_are_unchanged():
    assert contracts.CAMPAIGN_ROLES == {
        "worker": (WORKER_TAG, contracts.WORKER_NAME_RE),
        "manager": (MANAGER_TAG, contracts.MANAGER_NAME_RE),
        "builder": (BUILDER_TAG, contracts.BUILDER_NAME_RE),
    }
    assert contracts.ROLES == {
        **contracts.CAMPAIGN_ROLES,
        "pulse": (PULSE_TAG, contracts.PULSE_NAME_RE),
    }
    assert re.fullmatch(contracts.PULSE_NAME_RE, PULSE_NAME)
    assert not any(re.fullmatch(contracts.PULSE_NAME_RE, n) for n in (NAME, "pi-pulse-lon1"))
    assert not PULSE_TAG.startswith(contracts.RESOURCE_PREFIX)


def test_a_pulse_create_needs_only_pulse_tags_and_its_names():
    do_api.check_create([PULSE_NAME, "pi-pulse-nyc3-geonode"], [PULSE_TAG], ["9"])
    do_api.check_create([PULSE_NAME], [PULSE_TAG, "pi-pulse-shifter"], ["9"])
    for names, tags in (
        ([PULSE_NAME], [PULSE_TAG, WORKER_TAG]),
        ([PULSE_NAME], [PULSE_TAG, f"pi-camp-{SID}"]),
        ([PULSE_NAME], [PULSE_TAG, "prod"]),
        ([PULSE_NAME], ["pi-pulse-shifter"]),
        ([NAME], [PULSE_TAG]),
        ([PULSE_NAME], OURS),
        ([NAME], [*OURS, "pi-pulse-shifter"]),
    ):
        with pytest.raises(ScopeError):
            do_api.check_create(names, tags, ["9"])
    do_api.check_create([NAME, NAME], OURS, ["9"])
    with pytest.raises(ScopeError, match="one at a time"):
        do_api.check_create(["pi-camp-builder-20270101-cd34"] * 2, [BUILDER_TAG], ["9"])


def test_pulse_nodes_are_listed_only_by_their_exact_tag():
    do_api.check_allowed("GET", "/v2/droplets", {"tag_name": PULSE_TAG}, None)
    for tag in ("pi-pulse-shifter", "prod", "pi-pulse-node-x"):
        with pytest.raises(ScopeError, match="listed only"):
            do_api.check_allowed("GET", "/v2/droplets", {"tag_name": tag}, None)


async def test_campaign_deletes_over_the_whole_account_never_touch_a_pulse_node(tmp_path):
    fake, pulse, near, ours = world()
    ledger, audit = everything(fake), tmp_path / "audit.jsonl"
    early = Scope(CID, SID, CAMPAIGN_PROJECT, MANAGER_ID, datetime.fromisoformat(EARLY_TIME))
    deleted = set()
    async with running(fake, per_minute=10**6) as cloud:
        for role, role_scope in (("worker", SCOPE), ("manager", early), ("builder", SCOPE)):
            for droplet_id in sorted(ledger.ids["droplet"]):
                try:
                    result = await guarded_delete(
                        cloud, "droplet", droplet_id, role_scope, ledger, audit, role
                    )
                except OwnershipError:
                    continue
                if result == "deleted":
                    deleted.add(droplet_id)
    assert deleted == ours
    assert pulse | {near["worker_with_pulse_tag"]} <= set(fake.droplets)


async def test_retire_pulse_node_deletes_only_a_recorded_pulse_node_in_dev_resources(tmp_path):
    fake, pulse, near, ours = world()
    audit, target = tmp_path / "audit.jsonl", min(pulse)
    async with running(fake) as cloud:
        for key, *_, failed in PULSE_MISSES:
            with pytest.raises(OwnershipError) as refused:
                await retire_pulse_node(cloud, near[key], DEV_PROJECT, everything(fake), audit)
            assert refused.value.failed == failed, key
        with pytest.raises(OwnershipError) as refused:
            await retire_pulse_node(cloud, target, DEV_PROJECT, Ledger({"droplet": ours}), audit)
        assert refused.value.failed == ["recorded"]
        with pytest.raises(OwnershipError) as refused:
            await retire_pulse_node(cloud, target, CAMPAIGN_PROJECT, everything(fake), audit)
        assert refused.value.failed == ["project_id", "project"]
        assert await retire_pulse_node(cloud, target, DEV_PROJECT, everything(fake), audit) == (
            "deleted"
        )
    assert fake.deleted == [("droplet", target)] and max(pulse) in fake.droplets
    lines = [json.loads(line) for line in audit.read_text().splitlines()]
    assert {(line["role"], line["campaign"]) for line in lines} == {("pulse", "pulse")}
    assert [line["result"] for line in lines[-2:]] == ["attempt", "deleted"]


@pytest.mark.parametrize(
    "kind, role, use_pulse_scope",
    [("droplet", "pulse", False), ("droplet", "worker", True), ("ssh_key", "pulse", True)],
)
async def test_the_pulse_role_and_the_pulse_scope_go_only_together(
    tmp_path, kind, role, use_pulse_scope
):
    fake, pulse, _, _ = world()
    role_scope, audit = pulse_scope(DEV_PROJECT) if use_pulse_scope else SCOPE, tmp_path / "a"
    async with running(fake) as cloud:
        with pytest.raises(ValueError, match="retire_pulse_node"):
            await guarded_delete(cloud, kind, min(pulse), role_scope, everything(fake), audit, role)
    assert not fake.deleted and not audit.exists()


async def test_teardown_ignores_a_live_pulse_node(tmp_path, monkeypatch):
    fast_assign(monkeypatch)
    job = make_job(tmp_path)
    fake, _, _ = await owned_world(job)
    node = fake.add(PULSE_NAME, [PULSE_TAG])
    async with running(fake) as cloud:
        done = await deploy.teardown(
            cloud, job.inventory, [clean_report(tmp_path)], shell=Shell(), remove_agent=lambda: True
        )
    assert done["clean"] is True and PULSE_TAG not in done["tag_counts"]
    assert node in fake.droplets and ("droplet", node) not in fake.deleted
