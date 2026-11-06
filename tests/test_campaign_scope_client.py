"""The scope suite's client half: nothing off the allowlist, no DELETE without the guard and no
raw write to a production droplet or firewall ever reaches the network."""

from __future__ import annotations

import pytest
from campaign_fakes import CAMPAIGN_PROJECT, PROD_PROJECT, FakeDigitalOcean, running
from campaign_scope_cases import (
    FIREWALL_RULES,
    NAME,
    OURS,
    PROD_FIREWALL,
    SNAPSHOT,
)

from proxy_index.campaign import contracts, do_api, scope
from proxy_index.campaign.do_api import ScopeError


@pytest.mark.parametrize("method", ["DELETE", "delete", "Delete", "dElEtE"])
async def test_no_delete_without_the_guard_in_any_letter_case(method):
    """B1: the method is normalized before the allowlist and the guard check."""
    fake = FakeDigitalOcean()
    target = fake.add(NAME, OURS, project=CAMPAIGN_PROJECT)
    async with running(fake) as cloud:
        attempts = [
            dict(path="/v2/droplets", params={"tag_name": "prod"}),
            dict(path="/v2/droplets", params={"tag_name": OURS[1]}, guard=scope._GUARD),
            dict(path="/v2/droplets", guard=scope._GUARD),
            dict(path=f"/v2/droplets/{target}"),
            dict(path=f"/v2/droplets/{target}", guard=object()),
            dict(path=f"/v2/droplets/{target}", params={"tag_name": "x"}, guard=scope._GUARD),
            dict(path=f"/v2/droplets/{target}?tag_name=x", guard=scope._GUARD),
            dict(path="/v2/volumes/1", guard=scope._GUARD),
        ]
        for attempt in attempts:
            path = attempt.pop("path")
            with pytest.raises(ScopeError):
                await cloud.request(method, path, **attempt)
    assert not fake.requests and target in fake.droplets and not fake.deleted


@pytest.mark.parametrize(
    ("method", "path", "params", "body"),
    [
        ("POST", "/v2/droplets/700001/actions", None, {"type": "rebuild"}),
        ("post", "/v2/droplets/actions", {"tag_name": "prod"}, {"type": "power_off"}),
        ("POST", "/v2/tags/pi-camp-worker/resources", None, {"resources": []}),
        ("DELETE", "/v2/tags/pi-camp-worker/resources", None, {"resources": []}),
        ("DELETE", "/v2/tags/pi-camp-worker", None, None),
        ("GET", "/v2/tags", None, None),
        ("GET", "/v2/tags/prod", None, None),
        ("PUT", f"/v2/projects/{CAMPAIGN_PROJECT}", None, {"name": "x"}),
        ("PATCH", "/v2/droplets/700001", None, {}),
        ("PUT", "/v2/firewalls/33333333-3333-4333-8333-333333333333", None, {}),
        ("GET", "/v2/droplets", None, None),  # listing all droplets
        ("GET", "/v2/droplets", {"tag_name": "prod"}, None),
        ("GET", "/v2/sizes", {"tag_name": "pi-camp-x"}, None),
        ("GET", "/v2/account", {"page": 1}, None),
        ("POST", f"/v2/projects/{PROD_PROJECT}/resources", None, {"resources": []}),
        ("HEAD", "/v2/account", None, None),
        # B1 (CAMP-1f): well-formed writes to a droplet or firewall need the ownership token.
        ("POST", "/v2/droplets/700001/actions", None, {"type": "power_off"}),
        ("POST", "/v2/droplets/700001/actions", None, {"type": "shutdown"}),
        ("POST", "/v2/droplets/700001/actions", None, {"type": "snapshot", "name": SNAPSHOT}),
        ("POST", "/v2/firewalls", None, {**FIREWALL_RULES, "droplet_ids": [700001]}),
        ("PUT", f"/v2/firewalls/{PROD_FIREWALL}", None, FIREWALL_RULES),
        # CAMP-1h: the tag calls too, which only the client's own `_ensure_tag` sends.
        ("GET", "/v2/tags/pi-camp-worker", None, None),
        ("POST", "/v2/tags", None, {"name": "pi-camp-worker"}),
    ],
)
async def test_everything_off_the_allowlist_is_refused_before_the_network(
    method, path, params, body
):
    """B2: only listed method, path and query shapes are ever sent."""
    fake = FakeDigitalOcean()
    async with running(fake) as cloud:
        with pytest.raises(ScopeError):
            await cloud.request(method, path, params=params, body=body)
        with pytest.raises(ScopeError):
            await cloud.assign_project(CAMPAIGN_PROJECT, ["700001"], guard=object())
    assert not fake.requests and not fake.unlisted


@pytest.mark.parametrize("token", [None, object(), "scope"])
async def test_raw_writes_never_reach_a_production_droplet_or_firewall(token):
    """B1 (CAMP-1f): power-off, snapshot and firewall writes need the client's own token,
    which only its ownership-checking methods pass; the scope guard's token does not do."""
    fake = FakeDigitalOcean()
    production = fake.add("prod-node-0001", ["prod"])
    fake.add_resource(
        "firewall",
        PROD_FIREWALL,
        name="prod-fw",
        tags=["prod"],
        droplet_ids=[int(production)],
        inbound_rules=[{"protocol": "tcp"}],
    )
    guard = scope._GUARD if token == "scope" else token
    writes = [
        ("POST", f"/v2/droplets/{production}/actions", {"type": "power_off"}),
        ("POST", f"/v2/droplets/{production}/actions", {"type": "snapshot", "name": SNAPSHOT}),
        ("POST", "/v2/firewalls", {**FIREWALL_RULES, "droplet_ids": [int(production)]}),
        ("PUT", f"/v2/firewalls/{PROD_FIREWALL}", FIREWALL_RULES),
        ("POST", "/v2/tags", {"name": "pi-camp-worker"}),
    ]
    async with running(fake) as cloud:
        for method, path, body in writes:
            with pytest.raises(ScopeError):
                await cloud.request(method, path, body=body, guard=guard)
    assert not fake.requests and fake.droplets[production]["status"] == "active"
    assert fake.resources["firewall"][PROD_FIREWALL]["name"] == "prod-fw"
    assert len(fake.resources["firewall"]) == 1 and not fake.bodies


def test_tags_are_only_read_and_created_behind_the_token():
    """CAMP-1h: exactly these two tag calls, both needing the client's own token."""
    tag_calls = {key for key in do_api.ALLOWED if "/tags" in key[1]}
    assert tag_calls == {("GET", f"/v2/tags/{do_api.ENSURED_TAG}"), ("POST", "/v2/tags")}
    assert all(
        do_api._lookup(do_api.OWNED_WRITES, m, p)
        for m, p in (
            ("GET", "/v2/tags/pi-camp-worker"),
            ("GET", "/v2/tags/pi-pulse-node"),
            ("POST", "/v2/tags"),
        )
    )
    for method, path in (
        ("GET", "/v2/tags"),
        ("GET", "/v2/tags/prod"),
        ("GET", "/v2/tags/pi-camp-worker/resources"),
        ("POST", "/v2/tags/pi-camp-worker/resources"),
        ("DELETE", "/v2/tags/pi-camp-worker/resources"),
        ("DELETE", "/v2/tags/pi-camp-worker"),
        ("DELETE", "/v2/tags/pi-pulse-node"),
        ("POST", "/v2/tags/pi-pulse-node/resources"),
        ("GET", "/v2/tags/pi-pulse-nodes"),
        ("PUT", "/v2/tags/pi-camp-x"),
    ):
        with pytest.raises(ScopeError):
            do_api.check_allowed(method, path, None, do_api._OWNED, {"resources": []})


def test_the_tables_are_read_only():
    for table, key in (
        (do_api.ALLOWED, ("POST", "/v2/droplets/1/actions")),
        (scope.PATHS, "volume"),
        (do_api.BODIES, ("PUT", "/v2/x")),
        (do_api.OWNED_WRITES, ("PUT", "/v2/x")),
    ):
        with pytest.raises(TypeError):
            table[key] = None


def test_cloud_and_client_expose_no_delete():
    for owner in (contracts.Cloud, do_api.DigitalOcean):
        public = [name for name in dir(owner) if not name.startswith("_")]
        assert not [
            name
            for name in public
            if any(word in name.lower() for word in ("delete", "destroy", "remove", "purge"))
        ]
