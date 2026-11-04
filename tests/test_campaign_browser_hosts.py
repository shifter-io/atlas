"""BP-FIREWALL: the browser hosts' addresses on the manager's node API rule (fakes only)."""

from __future__ import annotations

import json

import pytest
from campaign_deploy_fakes import (
    MAC_IP,
    OTHER_IP,
    Fake,
    fast_assign,
    inventory,
    make_job,
    other_team,
    parse,
    run_deploy,
    writes,
)
from campaign_fakes import running

from proxy_index.campaign import browser_hosts, deploy, manager_rules, ops_api
from proxy_index.campaign import cli as campaign_cli
from proxy_index.campaign.contracts import PULSE_TAG

# Well-known public resolver addresses: global, and nobody's host.
HOSTS = ["8.8.8.8", "1.1.1.1"]
OTHER_HOSTS = ["9.9.9.9"]
PORT = manager_rules.NODE_API_PORT


@pytest.fixture(autouse=True)
def quick_assign(monkeypatch):
    fast_assign(monkeypatch)


@pytest.fixture
def job(tmp_path):
    return make_job(tmp_path, account="proxy-index")


class Drifting(Fake):
    """A fake whose stored firewall loses (or changes) the browser sources after a PUT."""

    drift: dict | None = None

    async def _firewall(self, request):
        reply = await super()._firewall(request)
        if self.drift is not None and request.method == "PUT":
            stored = self.resources["firewall"][request.match_info["id"]]
            for rule in stored["inbound_rules"]:
                if rule["ports"] == str(PORT):
                    rule["sources"] = dict(self.drift)
        return reply


async def opened(job, fake=None):
    """A deployed manager whose firewall has the node API rule (as orchestrator-enable)."""
    fake = fake or Fake()
    await run_deploy(fake, job)
    fake.tags.add(PULSE_TAG)
    deploy.save(job.inventory, inventory(job), node_api_port=PORT)
    async with running(fake) as cloud:
        await deploy.allow_me(cloud, job.inventory, MAC_IP)
    return fake


def firewall(fake, job):
    return fake.resources["firewall"][inventory(job)["manager_firewall_id"]]


def node_rule(hosts):
    return {"protocol": "tcp", "ports": str(PORT), "sources": node_sources(hosts)}


def node_sources(hosts):
    found = {"tags": [PULSE_TAG]}
    return found | ({"addresses": [f"{a}/32" for a in hosts]} if hosts else {})


async def set_hosts(fake, job, hosts, apply=True):
    async with running(fake) as cloud:
        return await browser_hosts.set_browser_hosts(cloud, job.inventory, hosts, apply=apply)


# --- rules ------------------------------------------------------------------------------------


def test_the_node_api_rule_lists_the_browser_hosts_and_nothing_else_changes():
    recorded = {"manager_id": "42", "node_api_port": PORT}
    plain = manager_rules.manager_rules(recorded, OTHER_IP)
    with_hosts = manager_rules.manager_rules(recorded | {"browser_host_ipv4s": HOSTS}, OTHER_IP)
    assert plain["inbound_rules"] == [ops_api.ssh_from(OTHER_IP), node_rule([])]
    assert with_hosts["inbound_rules"] == [ops_api.ssh_from(OTHER_IP), node_rule(HOSTS)]
    assert {k: v for k, v in with_hosts.items() if k != "inbound_rules"} == {
        k: v for k, v in plain.items() if k != "inbound_rules"
    }
    closed = manager_rules.manager_rules({"manager_id": "42", "browser_host_ipv4s": HOSTS}, MAC_IP)
    assert closed == ops_api.manager_firewall(MAC_IP, "42")  # no port, no browser sources


async def test_the_dry_run_changes_nothing(job):
    fake = await opened(job)
    before, sent, rules = inventory(job), writes(fake), firewall(fake, job)
    result = await set_hosts(fake, job, HOSTS, apply=False)
    assert result == {
        "applied": False,
        "node_api_rule_before": [node_rule([])],
        "node_api_rule_after": [node_rule(HOSTS)],
        "browser_host_ipv4s": HOSTS,
    }
    assert inventory(job) == before and writes(fake) == sent and firewall(fake, job) == rules


async def test_apply_sets_exactly_the_sources_and_keeps_ssh_and_outbound(job):
    fake = await opened(job)
    before = firewall(fake, job)
    result = await set_hosts(fake, job, HOSTS)
    after = firewall(fake, job)
    assert result["applied"] is True and result["node_api_rule_after"] == [node_rule(HOSTS)]
    assert after["inbound_rules"] == [ops_api.ssh_from(MAC_IP), node_rule(HOSTS)]
    for key in ("name", "outbound_rules", "tags", "droplet_ids"):
        assert after[key] == before[key]
    assert inventory(job)["browser_host_ipv4s"] == HOSTS
    # A new set replaces the old one; it never adds to it.
    result = await set_hosts(fake, job, OTHER_HOSTS)
    assert result["node_api_rule_before"] == [node_rule(HOSTS)]
    assert firewall(fake, job)["inbound_rules"][-1] == node_rule(OTHER_HOSTS)
    assert inventory(job)["browser_host_ipv4s"] == OTHER_HOSTS


async def test_clear_removes_the_browser_sources(job):
    fake = await opened(job)
    await set_hosts(fake, job, HOSTS)
    await set_hosts(fake, job, [])
    assert firewall(fake, job)["inbound_rules"] == [ops_api.ssh_from(MAC_IP), node_rule([])]
    assert inventory(job)["browser_host_ipv4s"] == []


# --- refusals ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        (["10.1.2.3"], "private"),
        (["172.16.0.9"], "private"),
        (["192.168.1.20"], "private"),
        (["169.254.10.10"], "private"),
        (["127.0.0.1"], "loopback"),
        (["192.0.2.1"], "documentation"),
        (["198.51.100.7"], "documentation"),
        (["203.0.113.10"], "documentation"),
        (["100.64.0.1"], "not a public"),
        (["224.0.0.5"], "not a public"),
        (["0.0.0.0"], "private"),
        (["8.8.8.8", " 8.8.8.8"], "twice"),
        (["8.8.8.8/32"], "not an IPv4"),
        (["2001:4860:4860::8888"], "not an IPv4"),
        (["host.example"], "not an IPv4"),
        ([f"8.8.8.{n}" for n in range(1, 10)], "at most 8"),
    ],
)
def test_only_distinct_public_ipv4s_are_accepted(values, reason):
    with pytest.raises(deploy.DeployError, match=reason):
        browser_hosts.checked(values)


def test_eight_public_addresses_are_accepted():
    values = [f"8.8.8.{n}" for n in range(1, 9)]
    assert browser_hosts.checked([f" {v}" for v in values]) == values


async def test_a_refused_address_changes_nothing(job):
    fake = await opened(job)
    before, sent = inventory(job), list(fake.requests)
    with pytest.raises(deploy.DeployError, match="private"):
        await set_hosts(fake, job, ["8.8.8.8", "10.0.0.1"])
    assert inventory(job) == before and fake.requests == sent  # refused before any call


async def test_the_port_must_be_open_for_browser_hosts(job):
    fake = Fake()
    await run_deploy(fake, job)
    with pytest.raises(deploy.DeployError, match="orchestrator-enable"):
        await set_hosts(fake, job, HOSTS)
    assert "browser_host_ipv4s" not in inventory(job)
    await set_hosts(fake, job, [])  # --clear needs no port
    assert firewall(fake, job)["inbound_rules"] == [ops_api.ssh_from(MAC_IP)]


async def test_wrong_account_is_refused(job, tmp_path):
    fake = await opened(job)
    before, sent = inventory(job), writes(fake)
    other_team(fake)
    with pytest.raises(deploy.DeployError, match="account"):
        await set_hosts(fake, job, HOSTS)
    assert inventory(job) == before and writes(fake) == sent
    other = tmp_path / "leftclick.json"
    other.write_text(json.dumps(before | {"account": "leftclick"}))
    async with running(Fake()) as cloud:
        with pytest.raises(deploy.DeployError, match="proxy-index account"):
            await browser_hosts.set_browser_hosts(cloud, other, HOSTS)


async def test_a_missing_firewall_is_refused(job):
    fake = await opened(job)
    fake.resources["firewall"].clear()
    with pytest.raises(deploy.DeployError, match="gone"):
        await set_hosts(fake, job, HOSTS, apply=False)


# --- verification -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "drift",
    [
        {"tags": [PULSE_TAG]},  # the addresses were dropped
        {"tags": [PULSE_TAG], "addresses": ["8.8.8.8/32"]},  # one is missing
        {"tags": [PULSE_TAG], "addresses": ["8.8.8.0/24", "1.1.1.1/32"]},  # a wider source
        {"addresses": ["8.8.8.8/32", "1.1.1.1/32"]},  # the pulse-node tag was dropped
        {"tags": [PULSE_TAG], "addresses": ["8.8.8.8", "1.1.1.1", "1.1.1.1"]},  # a duplicate
        {"tags": [PULSE_TAG], "addresses": ["not-an-address"]},
    ],
)
async def test_a_firewall_that_reads_back_differently_fails_and_saves_nothing(job, drift):
    fake = await opened(job, Drifting())
    before = inventory(job)
    fake.drift = drift
    with pytest.raises(deploy.DeployError, match="inventory is unchanged"):
        await set_hosts(fake, job, HOSTS)
    assert inventory(job) == before


async def test_a_read_back_without_the_prefix_is_the_same_source(job):
    fake = await opened(job, Drifting())
    fake.drift = {"tags": [PULSE_TAG], "addresses": ["1.1.1.1", "8.8.8.8/32"]}
    await set_hosts(fake, job, HOSTS)
    assert inventory(job)["browser_host_ipv4s"] == HOSTS


def test_holds_needs_exactly_one_node_api_rule():
    rule = node_rule(HOSTS)
    assert browser_hosts.holds({"inbound_rules": [rule]}, PORT, HOSTS)
    assert not browser_hosts.holds({"inbound_rules": [rule, rule]}, PORT, HOSTS)
    assert not browser_hosts.holds({"inbound_rules": []}, PORT, HOSTS)
    assert not browser_hosts.holds(None, PORT, HOSTS)
    assert browser_hosts.holds({"inbound_rules": []}, None, [])  # closed: nothing to check


# --- re-apply ---------------------------------------------------------------------------------


async def test_allow_me_and_deploy_keep_the_browser_sources(job):
    fake = await opened(job)
    await set_hosts(fake, job, HOSTS)
    async with running(fake) as cloud:
        await deploy.allow_me(cloud, job.inventory, OTHER_IP)
    assert firewall(fake, job)["inbound_rules"] == [ops_api.ssh_from(OTHER_IP), node_rule(HOSTS)]
    await run_deploy(fake, job)  # a redeploy rebuilds the firewall from the inventory too
    assert firewall(fake, job)["inbound_rules"] == [ops_api.ssh_from(MAC_IP), node_rule(HOSTS)]
    await set_hosts(fake, job, [])
    async with running(fake) as cloud:
        await deploy.allow_me(cloud, job.inventory, OTHER_IP)
    assert firewall(fake, job)["inbound_rules"] == [ops_api.ssh_from(OTHER_IP), node_rule([])]


# --- command line -----------------------------------------------------------------------------


class Cloud:
    """Stands in for the client the CLI builds; records the account it was built for."""

    accounts: list = []

    def __init__(self, account):
        self.accounts.append(account)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


@pytest.mark.parametrize(
    ("argv", "hosts", "apply"),
    [
        (["--ip", "8.8.8.8,1.1.1.1", "--ip", "9.9.9.9"], [*HOSTS, *OTHER_HOSTS], False),
        (["--ip", "8.8.8.8", "--apply"], ["8.8.8.8"], True),
        (["--clear", "--apply"], [], True),
    ],
)
def test_the_cli_routes_browser_hosts(monkeypatch, capsys, tmp_path, argv, hosts, apply):
    seen, recorded = [], tmp_path / "campaign-manager.json"
    recorded.write_text(json.dumps({"account": "proxy-index", "manager_id": 1, "host": "h"}))

    async def step(cloud, path, addresses, apply=False):
        seen.append((path, addresses, apply))
        return {"applied": apply}

    monkeypatch.setattr(browser_hosts, "set_browser_hosts", step)
    monkeypatch.setattr(campaign_cli, "_cloud", Cloud)
    Cloud.accounts = []
    args = parse(["campaign", "manager", "browser-hosts", *argv, "--inventory", str(recorded)])
    assert campaign_cli.dispatch(args) == 0
    assert seen == [(recorded, hosts, apply)] and Cloud.accounts == ["proxy-index"]
    assert json.loads(capsys.readouterr().out) == {"applied": apply}


def test_the_cli_refuses_a_bad_address_before_any_client(monkeypatch, capsys, tmp_path):
    recorded = tmp_path / "campaign-manager.json"
    recorded.write_text(json.dumps({"account": "proxy-index", "manager_id": 1, "host": "h"}))
    monkeypatch.setattr(campaign_cli, "_cloud", Cloud)
    Cloud.accounts = []
    args = parse(
        ["campaign", "manager", "browser-hosts", "--ip", "10.0.0.1", "--inventory", str(recorded)]
    )
    assert campaign_cli.dispatch(args) == 3
    assert Cloud.accounts == [] and "private" in capsys.readouterr().err


def test_the_cli_needs_ip_or_clear(tmp_path):
    for argv in ([], ["--ip", "8.8.8.8", "--clear"]):
        with pytest.raises(SystemExit):
            parse(["campaign", "manager", "browser-hosts", *argv, "--inventory", "x.json"])


@pytest.mark.parametrize("value", ["", " ", ",", "192.0.2.1,", ",192.0.2.1", "192.0.2.1,,"])
def test_an_empty_ip_entry_is_refused_and_never_clears(monkeypatch, capsys, tmp_path, value):
    """A failed `--ip "$(...)"` must not clear the hosts: only --clear does (review B2)."""
    recorded = tmp_path / "campaign-manager.json"
    recorded.write_text(json.dumps({"account": "proxy-index", "manager_id": 1, "host": "h"}))
    monkeypatch.setattr(campaign_cli, "_cloud", Cloud)
    Cloud.accounts = []
    argv = ["--ip", value, "--apply", "--inventory", str(recorded)]
    args = parse(["campaign", "manager", "browser-hosts", *argv])
    assert campaign_cli.dispatch(args) == 3
    assert Cloud.accounts == [] and "--clear" in capsys.readouterr().err


async def test_an_inventory_without_a_firewall_is_refused(job):
    fake = Fake()
    await run_deploy(fake, job)
    deploy.save(job.inventory, inventory(job) | {"manager_firewall_id": None})
    with pytest.raises(deploy.DeployError, match="deploy first"):
        await set_hosts(fake, job, [], apply=False)
