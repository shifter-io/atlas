"""Shared fixtures of the campaign scope suite (`tests/test_campaign_scope*.py`): the scope, our
ownership markers, every near-miss of them and a fake account seeded like the real one."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from campaign_fakes import (
    CAMPAIGN_PROJECT,
    CID,
    CREATE_TIME,
    EARLY_TIME,
    MANAGER_ID,
    OTHER_SID,
    PROD_PROJECT,
    SID,
    SPEC_TIME,
    FakeDigitalOcean,
)

from proxy_index.campaign.contracts import (
    BUILDER_TAG,
    MANAGER_TAG,
    WORKER_TAG,
    DropletRecord,
    Scope,
)
from proxy_index.campaign.scope import Ledger

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_SRC = ROOT / "src" / "proxy_index" / "campaign"
SCOPE = Scope(CID, SID, CAMPAIGN_PROJECT, MANAGER_ID, datetime.fromisoformat(SPEC_TIME))
OURS = [WORKER_TAG, f"pi-camp-{SID}", f"pi-camp-{SID}-h03"]
NAME = f"pic-{SID}-h03-lon1-shifter"
SNAPSHOT = "pi-camp-worker-0.9.5-0123456789ab-20270101"
PROD_FIREWALL = "33333333-3333-4333-8333-000000000099"
FIREWALL_RULES = {
    "name": "pi-camp-x",
    "inbound_rules": [],
    "outbound_rules": [],
    "tags": [],
    "droplet_ids": [],
}
# Near-misses: (key, name, tags, project, created, failing check, role).
NEAR_MISSES = [
    ("untagged", "web-legacy", [], PROD_PROJECT, None, "tags", "worker"),
    ("other_tags", "api-01", ["prod", "k8s"], PROD_PROJECT, None, "tags", "worker"),
    ("pic_name_without_tags", NAME, ["prod"], CAMPAIGN_PROJECT, None, "tags", "worker"),
    ("ours_in_other_project", NAME, OURS, PROD_PROJECT, None, "project", "worker"),
    ("ours_in_no_project", NAME, OURS, None, None, "project", "worker"),
    ("worker_without_campaign_tag", NAME, [WORKER_TAG], CAMPAIGN_PROJECT, None, "tags", "worker"),
    (
        "other_campaign",
        f"pic-{OTHER_SID}-h03-lon1-shifter",
        [WORKER_TAG, f"pi-camp-{OTHER_SID}"],
        CAMPAIGN_PROJECT,
        None,
        "tags",
        "worker",
    ),
    (
        "other_campaign_name",
        f"pic-{OTHER_SID}-h03-lon1-shifter",
        OURS,
        CAMPAIGN_PROJECT,
        None,
        "name",
        "worker",
    ),
    ("bad_name", "pic-01234567-lon1", OURS, CAMPAIGN_PROJECT, None, "name", "worker"),
    ("worker_tagged_manager", NAME, [*OURS, MANAGER_TAG], CAMPAIGN_PROJECT, None, "tags", "worker"),
    ("created_before_spec", NAME, OURS, CAMPAIGN_PROJECT, EARLY_TIME, "created_after", "worker"),
    (
        "foreign_manager",
        "pi-camp-manager-20270101-ee99",
        [MANAGER_TAG],
        CAMPAIGN_PROJECT,
        None,
        "not_manager",
        "manager",
    ),
    (
        "foreign_builder",
        "pi-camp-builder-20270101-ee99",
        [BUILDER_TAG],
        PROD_PROJECT,
        None,
        "project",
        "builder",
    ),
]


def seeded():
    fake = FakeDigitalOcean()
    fake.seed_production()
    near = {
        key: fake.add(name, tags, project=project, created=created or CREATE_TIME)
        for key, name, tags, project, created, _, _ in NEAR_MISSES
    }
    near["manager"] = fake.add(
        "pi-camp-manager-20270101-ab12",
        [MANAGER_TAG],
        project=CAMPAIGN_PROJECT,
        droplet_id=MANAGER_ID,
    )
    near["builder"] = fake.add(
        "pi-camp-builder-20270101-cd34", [BUILDER_TAG], project=CAMPAIGN_PROJECT
    )
    owned = {
        fake.add(f"pic-{SID}-h03-{region}-shifter", OURS, project=CAMPAIGN_PROJECT)
        for region in ("lon1", "fra1", "syd1")
    }
    return fake, near, owned


def droplets(ids):
    return Ledger({"droplet": set(ids)})


def audit_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


async def created_worker(cloud, name=NAME):
    [droplet] = await cloud.create(
        [name], region="lon1", size="c-2", image="1", tags=OURS, ssh_keys=["9"]
    )
    return droplet, {droplet.id: DropletRecord("shifter", "lon1", 3, name, droplet.id)}
