"""MANAGER-TAG-1: a firewall write creates every missing tag its rules name, the inbound source
pi-pulse-node too, and creates no other tag (fakes only, no live calls)."""

from __future__ import annotations

import pytest
from campaign_fakes import CAMPAIGN_PROJECT, FakeDigitalOcean, running

from proxy_index.campaign import do_api
from proxy_index.campaign.contracts import MANAGER_TAG, PULSE_TAG, WORKER_TAG
from proxy_index.campaign.do_api import ScopeError

NODE_RULE = {"protocol": "tcp", "ports": "8443", "sources": {"tags": [PULSE_TAG]}}
RULES = {
    "name": "pi-camp-manager-fw",
    "inbound_rules": [NODE_RULE],
    "outbound_rules": [],
    "tags": [MANAGER_TAG],
    "droplet_ids": [],
}


def calls(fake):
    return [(method, path) for method, path, _, _ in fake.requests]


async def test_a_missing_pulse_source_tag_is_created_empty_before_the_rules():
    fake = FakeDigitalOcean()
    fake.tags.add(MANAGER_TAG)
    node = fake.add("pi-camp-manager-20270101-ab12", [MANAGER_TAG], project=CAMPAIGN_PROJECT)
    async with running(fake) as cloud:
        created = await cloud.create_firewall(RULES, CAMPAIGN_PROJECT)
    assert calls(fake) == [
        ("GET", f"/v2/tags/{MANAGER_TAG}"),
        ("GET", f"/v2/tags/{PULSE_TAG}"),
        ("POST", "/v2/tags"),
        ("POST", "/v2/firewalls"),
    ]
    assert [b for b in fake.bodies if b[1] == "/v2/tags"] == [
        ("POST", "/v2/tags", {"name": PULSE_TAG})
    ]
    assert fake.resources["firewall"][created["id"]]["inbound_rules"] == [NODE_RULE]
    assert all(PULSE_TAG not in d["tags"] for d in fake.droplets.values())  # stays empty
    assert fake.droplets[node]["tags"] == [MANAGER_TAG] and not fake.unlisted


async def test_an_existing_pulse_tag_is_not_created_again():
    fake = FakeDigitalOcean()
    fake.tags.update({MANAGER_TAG, PULSE_TAG})
    async with running(fake) as cloud:
        created = await cloud.create_firewall(RULES, CAMPAIGN_PROJECT)
        await cloud.update_firewall(created["id"], RULES, CAMPAIGN_PROJECT)
    assert not [b for b in fake.bodies if b[1] == "/v2/tags"]
    assert ("GET", f"/v2/tags/{PULSE_TAG}") in calls(fake)


async def test_pi_camp_tags_are_still_created_only_when_missing():
    fake = FakeDigitalOcean()
    fake.tags.add(PULSE_TAG)
    worker_rules = {**RULES, "name": "pi-camp-worker-fw", "tags": [WORKER_TAG, MANAGER_TAG]}
    async with running(fake) as cloud:
        await cloud.create_firewall(worker_rules, CAMPAIGN_PROJECT)
        await cloud.create_firewall(worker_rules, CAMPAIGN_PROJECT)
    assert [b[2]["name"] for b in fake.bodies if b[1] == "/v2/tags"] == [WORKER_TAG, MANAGER_TAG]


@pytest.mark.parametrize(
    "name",
    ["prod", "pi-pulse-", "pi-pulse-node2", "pi-pulse-nodes", "pi-camp-", "PI-PULSE-NODE"],
)
async def test_any_other_tag_is_refused_before_a_request(name):
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        with pytest.raises(ScopeError, match="only pi-camp- tags and pi-pulse-node are created"):
            await cloud._ensure_tag(name)
        with pytest.raises(ScopeError):
            await cloud.request("POST", "/v2/tags", body={"name": name}, guard=do_api._OWNED)
    assert not fake.requests and not fake.tags and not fake.unlisted


async def test_a_foreign_source_tag_writes_no_tag_and_no_rules():
    fake = FakeDigitalOcean()
    fake.tags.add(MANAGER_TAG)
    foreign = {**RULES, "inbound_rules": [{**NODE_RULE, "sources": {"tags": ["prod"]}}]}
    async with running(fake) as cloud:
        with pytest.raises(ScopeError):
            await cloud.create_firewall(foreign, CAMPAIGN_PROJECT)
    assert not fake.bodies and not fake.resources["firewall"]


def test_the_pulse_tag_calls_need_the_clients_own_token():
    do_api.check_allowed("GET", f"/v2/tags/{PULSE_TAG}", None, do_api._OWNED)
    do_api.check_allowed("POST", "/v2/tags", None, do_api._OWNED, {"name": PULSE_TAG})
    with pytest.raises(ScopeError, match="only after an ownership check"):
        do_api.check_allowed("GET", f"/v2/tags/{PULSE_TAG}", None, None)
    with pytest.raises(ScopeError, match="body is outside"):
        do_api.check_allowed("POST", "/v2/tags", None, do_api._OWNED, {"name": "pi-pulse-x"})


@pytest.mark.parametrize("tag", ["prod", "pi-pulse-x", None])
def test_the_list_guard_is_unchanged(tag):
    params = {} if tag is None else {"tag_name": tag}
    with pytest.raises(ScopeError, match="listed only by a pi-camp- tag or pi-pulse-node"):
        do_api.check_allowed("GET", "/v2/droplets", params, None)
    for allowed in (PULSE_TAG, WORKER_TAG):
        do_api.check_allowed("GET", "/v2/droplets", {"tag_name": allowed}, None)


@pytest.mark.parametrize("path", ["/v2/tags/pi-pulse-node", "/v2/tags", "/v2/droplets/1"])
def test_the_delete_guard_is_unchanged(path):
    with pytest.raises(ScopeError):
        do_api.check_allowed("DELETE", path, None, do_api._OWNED)
