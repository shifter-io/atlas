"""Scoped DigitalOcean operations: SSH keys, droplet actions, snapshots, firewalls and tags.

Every body outside our scope is refused before the network; the fake is in-process.
"""

from __future__ import annotations

import pytest
from campaign_api_cases import FIREWALL, OURS, RULES, SNAPSHOT
from campaign_fakes import CAMPAIGN_PROJECT, PROD_PROJECT, SID, FakeDigitalOcean, Fault, running

from proxy_index.campaign import do_api
from proxy_index.campaign.contracts import BUILDER_TAG, MANAGER_TAG, WORKER_TAG
from proxy_index.campaign.do_api import ApiError, ScopeError


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/v2/account/keys", {"name": "owner-laptop", "public_key": "ssh-ed25519 AAAA"}),
        ("POST", "/v2/account/keys", {"name": "pi-camp-mac-1", "public_key": "k", "extra": 1}),
        ("POST", "/v2/droplets/700001/actions", {"type": "rebuild", "image": "x"}),
        ("POST", "/v2/droplets/700001/actions", {"type": "restore"}),
        ("POST", "/v2/droplets/700001/actions", {"type": "snapshot", "name": "prod-backup"}),
        ("POST", "/v2/droplets/700001/actions", {"type": "power_off", "force": True}),
        ("POST", "/v2/droplets/700001/actions", None),
        ("POST", "/v2/images/700001/actions", {"type": "transfer", "region": "mars1"}),
        ("POST", "/v2/images/700001/actions", {"type": "convert", "region": "lon1"}),
        ("POST", "/v2/firewalls", {**RULES, "name": "prod-fw"}),
        ("POST", "/v2/firewalls", {**RULES, "tags": ["prod"]}),
        ("POST", "/v2/firewalls", {**RULES, "tags": ["pi-camp-other"]}),
        ("POST", "/v2/firewalls", {**RULES, "droplet_ids": ["all"]}),
        ("POST", "/v2/firewalls", {**RULES, "load_balancer_uids": ["x"]}),
        ("PUT", f"/v2/firewalls/{FIREWALL}", {**RULES, "name": "default"}),
        ("POST", "/v2/tags", {"name": "prod"}),
        ("POST", "/v2/tags", {"name": "pi-camp-Worker"}),
        ("POST", "/v2/tags", {"name": WORKER_TAG, "resources": [{"resource_id": "1"}]}),
        ("POST", "/v2/tags", None),
    ],
)
async def test_operation_bodies_outside_our_scope_are_refused_before_the_network(
    method, path, body
):
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        with pytest.raises(ScopeError):
            await cloud.request(method, path, body=body)
    assert not fake.requests


async def test_ssh_keys_lists_only_ours_and_adds_one():
    fake = FakeDigitalOcean()
    fake.add_resource("ssh_key", 1, name="owner-laptop", public_key="ssh-ed25519 AAAA owner")
    fake.add_resource("ssh_key", 2, name="pi-camp-mac-20270101", public_key="ssh-ed25519 BBBB")
    async with running(fake) as cloud:
        assert [k["id"] for k in await cloud.ssh_keys()] == [2]
        added = await cloud.create_ssh_key("pi-camp-worker-20270101", "ssh-ed25519 CCCC")
    assert (
        added["name"] == "pi-camp-worker-20270101" and str(added["id"]) in fake.resources["ssh_key"]
    )


async def test_droplet_actions_run_only_on_a_builder_this_client_created():
    fake = FakeDigitalOcean()
    production = fake.add("prod-node-0001", ["prod"])
    foreign = fake.add("pi-camp-builder-20270101-ee99", [BUILDER_TAG])
    async with running(fake) as cloud:
        [worker] = await cloud.create(
            [f"pic-{SID}-h03-lon1-shifter"],
            region="lon1",
            size="c-2",
            image="1",
            tags=OURS,
            ssh_keys=["9"],
        )
        [builder, renamed, gone] = [
            (
                await cloud.create(
                    [f"pi-camp-builder-20270101-ab1{i}"],
                    region="lon1",
                    size="s-1vcpu-1gb",
                    image="ubuntu-24-04-x64",
                    tags=[BUILDER_TAG],
                    ssh_keys=["9"],
                )
            )[0]
            for i in range(3)
        ]
        fake.droplets[renamed.id]["name"] = "prod-node-0002"
        fake.droplets.pop(gone.id)
        for target in (production, foreign, worker.id, renamed.id, gone.id, builder.id):
            with pytest.raises(ScopeError):  # the builder is not in Proxy Index yet
                await cloud.droplet_action(target, "power_off", CAMPAIGN_PROJECT)
        for droplet_id in (production, foreign, worker.id, renamed.id, builder.id):
            fake.move(droplet_id)
        for target in (production, foreign, worker.id, renamed.id):
            with pytest.raises(ScopeError):
                await cloud.droplet_action(target, "power_off", CAMPAIGN_PROJECT)
        fake.move(builder.id, PROD_PROJECT)
        fake.project_names[PROD_PROJECT] = "Proxy Index (copy)"
        with pytest.raises(ScopeError):  # a project that is not the one named Proxy Index
            await cloud.droplet_action(builder.id, "power_off", PROD_PROJECT)
        fake.move(builder.id)
        assert not [b for b in fake.bodies if "/actions" in b[1]]
        off = await cloud.droplet_action(builder.id, "shutdown", CAMPAIGN_PROJECT)
        shot = await cloud.droplet_action(builder.id, "snapshot", CAMPAIGN_PROJECT, SNAPSHOT)
        assert (await cloud.action(off["id"]))["status"] == "completed"
        [image] = await cloud.droplet_snapshots(builder.id)
        assert image["name"] == SNAPSHOT and shot["type"] == "snapshot"
        assert await cloud.image(image["id"]) == image and await cloud.image("1") is None
    assert fake.droplets[builder.id]["status"] == "off"
    assert fake.droplets[production]["status"] == "active"


async def test_only_campaign_worker_snapshots_are_transferred():
    fake = FakeDigitalOcean()
    fake.images["11"] = {"id": 11, "name": "prod-golden", "regions": ["lon1"], "droplet": ""}
    fake.images["12"] = {"id": 12, "name": SNAPSHOT, "regions": ["lon1"], "droplet": ""}
    async with running(fake) as cloud:
        for image_id in ("11", "13"):
            with pytest.raises(ScopeError):
                await cloud.transfer_image(image_id, "fra1")
        action = await cloud.transfer_image("12", "fra1")
    assert action["type"] == "transfer" and fake.images["12"]["regions"] == ["lon1", "fra1"]
    assert fake.images["11"]["regions"] == ["lon1"]


async def test_firewalls_attach_only_to_campaign_droplets():
    fake = FakeDigitalOcean()
    production = fake.add("prod-node-0001", ["prod"])
    manager = fake.add("pi-camp-manager-20270101-ab12", [MANAGER_TAG])
    fake.add_resource("firewall", FIREWALL, name="prod-fw", tags=["prod"], droplet_ids=[])
    async with running(fake) as cloud:
        fake.move(production)
        for target in (production, "999", manager):  # the manager is not in Proxy Index yet
            with pytest.raises(ScopeError):
                await cloud.create_firewall(
                    {**RULES, "tags": [], "droplet_ids": [int(target)]}, CAMPAIGN_PROJECT
                )
        for firewall_id in (FIREWALL, "33333333-3333-4333-8333-999999999999"):
            with pytest.raises(ScopeError):
                await cloud.update_firewall(firewall_id, RULES, CAMPAIGN_PROJECT)
        with pytest.raises(ScopeError):
            await cloud.create_firewall({**RULES, "name": "default"}, CAMPAIGN_PROJECT)
        fake.move(manager)
        with pytest.raises(ScopeError):  # not the project named Proxy Index
            await cloud.create_firewall({**RULES, "droplet_ids": [int(manager)]}, PROD_PROJECT)
        created = await cloud.create_firewall(
            {**RULES, "droplet_ids": [int(manager)]}, CAMPAIGN_PROJECT
        )
        updated = await cloud.update_firewall(
            created["id"], {**RULES, "droplet_ids": []}, CAMPAIGN_PROJECT
        )
    assert [(m, p) for m, p, _ in fake.bodies] == [
        ("POST", "/v2/tags"),
        ("POST", "/v2/firewalls"),
        ("PUT", f"/v2/firewalls/{created['id']}"),
    ]
    assert updated["droplet_ids"] == [] and fake.resources["firewall"][FIREWALL]["name"] == (
        "prod-fw"
    )


async def test_a_firewall_tag_is_created_only_when_missing_and_only_ours():
    """CAMP-1h: DigitalOcean refuses a firewall naming a tag that does not exist (422)."""
    fake = FakeDigitalOcean()
    manager = fake.add("pi-camp-manager-20270101-ab12", [MANAGER_TAG], project=CAMPAIGN_PROJECT)
    async with running(fake) as cloud:
        for name in ("prod", "pi-camp-", "pi-camp-x/resources"):
            with pytest.raises(ScopeError):
                await cloud._ensure_tag(name)
        for body in ({"name": "prod"}, {"name": WORKER_TAG, "resources": []}):
            with pytest.raises(ScopeError):
                await cloud.request("POST", "/v2/tags", body=body, guard=do_api._OWNED)
        fake.faults.append(Fault("GET", f"/v2/tags/{WORKER_TAG}", 403))
        with pytest.raises(ApiError):
            await cloud.create_firewall(RULES, CAMPAIGN_PROJECT)
        assert WORKER_TAG not in fake.tags and not fake.resources["firewall"]
        created = await cloud.create_firewall(RULES, CAMPAIGN_PROJECT)
        await cloud.update_firewall(
            created["id"], {**RULES, "tags": [WORKER_TAG, MANAGER_TAG]}, CAMPAIGN_PROJECT
        )
    assert [b for b in fake.bodies if b[1] == "/v2/tags"] == [
        ("POST", "/v2/tags", {"name": WORKER_TAG})
    ]
    assert fake.resources["firewall"][created["id"]]["tags"] == [WORKER_TAG, MANAGER_TAG]
    assert not fake.unlisted and fake.droplets[manager]["tags"] == [MANAGER_TAG]  # untouched
