"""RENAME-ATLAS-2: what was created or written under the old names (pi-camp-, pi-pulse-, pic-,
project "Proxy Index", account proxy-index, the old keychain entries, format ids, launchd label
and Mac key) is still recognised for one release, while everything new uses the Atlas names."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime

import pytest
from campaign_fakes import CAMPAIGN_PROJECT, CREATE_TIME, SID, FakeDigitalOcean, running
from campaign_scope_cases import SCOPE
from test_browser_contracts_records import manifest_fields

from atlas import legacy_names
from atlas.archive import FORMATS
from atlas.browser import contracts as browser_contracts
from atlas.browser import release
from atlas.campaign import cli, deploy, do_api, guard, scope
from atlas.campaign.contracts import (
    ACCOUNTS,
    PRODUCTION,
    PULSE_TAG,
    STAGING,
    WORKER_TAG,
    CampaignSpec,
    Droplet,
    environment_of,
    with_legacy,
)
from atlas.estimates.compute import load_formula

OLD_WORKER = [
    "pi-camp-worker",
    f"pi-camp-{SID}",
    f"pi-camp-{SID}-h03",
]


def test_old_names_read_as_the_current_ones_and_back():
    assert legacy_names.current("pi-camp-worker") == WORKER_TAG == "atlas-camp-worker"
    assert legacy_names.current("pi-camp-stg-manager-fw") == "atlas-camp-stg-manager-fw"
    assert legacy_names.current("pi-pulse-node") == PULSE_TAG
    assert legacy_names.current(f"pic-{SID}-h03-lon1-shifter") == f"atlas-{SID}-h03-lon1-shifter"
    assert legacy_names.current("prod-node-0001") == "prod-node-0001"
    assert legacy_names.old(WORKER_TAG) == "pi-camp-worker"
    assert legacy_names.old(PULSE_TAG) == "pi-pulse-node"
    assert legacy_names.old(f"atlas-{SID}-h03-lon1-x") == f"pic-{SID}-h03-lon1-x"
    assert legacy_names.old("prod") is None
    assert with_legacy([WORKER_TAG, "prod"]) == [WORKER_TAG, "prod", "pi-camp-worker"]
    assert environment_of("pi-camp-stg-worker-fw") == STAGING
    assert environment_of(f"pi-camp-{SID}") == PRODUCTION
    assert environment_of("pi-other") is None


def test_the_old_account_key_reads_as_atlas(tmp_path):
    assert legacy_names.account("proxy-index") == "atlas" and legacy_names.account("x") == "x"
    assert "proxy-index" not in ACCOUNTS and ACCOUNTS["atlas"].project_name == "Atlas"
    inventory = tmp_path / "campaign-manager.json"
    inventory.write_text(json.dumps({"account": "proxy-index", "manager_id": "1"}))
    assert deploy.load_inventory(inventory) == {"account": "atlas", "manager_id": "1"}
    assert deploy.load_inventory(tmp_path / "absent.json") == {}
    parser = cli.account_key
    assert parser("proxy-index") == "atlas"


def test_an_old_spec_reads_with_the_new_account_key():
    from campaign_hour_cases import make_spec

    spec = json.loads(make_spec(account="atlas").to_json())
    assert CampaignSpec.from_json(json.dumps(spec | {"account": "proxy-index"})).account == "atlas"


def droplet(name, tags, droplet_id="700001"):
    when = datetime.fromisoformat(CREATE_TIME)
    return Droplet(droplet_id, name, "lon1", "c-2", "active", when, tuple(tags))


def test_a_pre_rename_worker_and_pulse_node_pass_the_role_checks():
    old = droplet(f"pic-{SID}-h03-lon1-shifter", OLD_WORKER)
    assert all(scope.role_checks(old, SCOPE, "worker").values())
    node = droplet("pi-pulse-lon1-shifter", ["pi-pulse-node"])
    assert scope.role_checks(node, scope.pulse_scope("p1"), "pulse") == {"tags": True, "name": True}
    mixed = droplet(f"pic-{SID}-h03-lon1-shifter", [*OLD_WORKER, "pi-camp-manager"])
    assert scope.role_checks(mixed, SCOPE, "worker")["tags"] is False


async def test_a_pre_rename_worker_is_listed_and_removed_in_a_renamed_project(tmp_path):
    fake = FakeDigitalOcean()
    fake.project_names[CAMPAIGN_PROJECT] = "Proxy Index"  # not yet renamed by the operator
    old = fake.add(f"pic-{SID}-h03-lon1-shifter", OLD_WORKER, project=CAMPAIGN_PROJECT)
    audit = tmp_path / "audit.jsonl"
    async with running(fake) as cloud:
        assert await cloud.project_id("Atlas") == CAMPAIGN_PROJECT
        assert [d.id for d in await cloud.list_tag("pi-camp-worker")] == [old]
        ledger = scope.Ledger({"droplet": {old}})
        assert await scope.guarded_delete(cloud, "droplet", old, SCOPE, ledger, audit) == "deleted"
        with pytest.raises(do_api.ScopeError):
            await cloud.list_tag("pi-other")
    assert ("droplet", old) in fake.deleted


async def test_two_projects_with_either_name_fail_closed():
    fake = FakeDigitalOcean()
    fake.project_names[CAMPAIGN_PROJECT] = "Proxy Index"
    fake.projects["33333333-3333-4333-8333-333333333333"] = set()
    fake.project_names["33333333-3333-4333-8333-333333333333"] = "Atlas"
    async with running(fake) as cloud:
        with pytest.raises(do_api.ScopeError, match="exactly one project"):
            await cloud.project_id("Atlas")


async def test_old_keys_are_ours_and_an_old_firewall_is_renamed_by_its_update():
    from campaign_api_cases import RULES

    fake = FakeDigitalOcean()
    fake.add_resource("ssh_key", 1, name="pi-camp-mac-20270101", public_key="k")
    fake.add_resource("ssh_key", 2, name="owner-laptop", public_key="k")
    firewall = "33333333-3333-4333-8333-000000000001"
    fake.add_resource(
        "firewall", firewall, name="pi-camp-worker-fw", tags=["pi-camp-worker"], droplet_ids=[]
    )
    async with running(fake) as cloud:
        assert [k["id"] for k in await cloud.ssh_keys()] == [1]
        updated = await cloud.update_firewall(firewall, RULES, CAMPAIGN_PROJECT)
    assert updated["name"] == RULES["name"] and updated["tags"] == RULES["tags"]


def test_the_keychain_falls_back_to_the_old_entry(monkeypatch):
    asked = []

    def security(command, **_):
        asked.append((command[4], command[6]))
        found = (command[4], command[6]) == legacy_names.DO_KEYCHAIN
        return subprocess.CompletedProcess(command, 0 if found else 44, "token\n" if found else "")

    monkeypatch.setattr(do_api.subprocess, "run", security)
    assert do_api.keychain_token(ACCOUNTS["atlas"])() == "token"
    assert asked == [("Atlas DigitalOcean", "team: Atlas"), legacy_names.DO_KEYCHAIN]
    asked.clear()
    with pytest.raises(do_api.TokenError, match="Leftclick DigitalOcean"):
        do_api.keychain_token(ACCOUNTS["leftclick"])()
    assert asked == [("Leftclick DigitalOcean", "team: Leftclick")]


def test_old_format_ids_still_read(tmp_path):
    formula = {"schema": "proxy-index-hour-formula", "schema_version": 1, "network": "x"}
    (tmp_path / "x.json").write_text(json.dumps(formula))
    assert load_formula(tmp_path, "x") == formula
    (tmp_path / "y.json").write_text(json.dumps(formula | {"schema": "other"}))
    with pytest.raises(ValueError, match="not a version 1 hour formula"):
        load_formula(tmp_path, "y")
    assert FORMATS == ("atlas-raw-day", "proxy-index-raw-day")
    doc = browser_contracts.manifest(**manifest_fields())
    old = doc | {"format": "proxy-index-browser-raw"}
    assert browser_contracts.check_manifest(old) == old
    with pytest.raises(ValueError, match="browser manifest"):
        browser_contracts.check_manifest(doc | {"format": "other"})


def test_a_browser_installed_before_the_rename_is_recognised(tmp_path):
    build = release.Build("https://cdn.example/camoufox.zip", "0" * 64, 10, "camoufox-bin")
    target = tmp_path / "camoufox"
    target.mkdir()
    (target / "camoufox-bin").write_text("")
    (target / ".proxy-index-release.json").write_text(json.dumps(release._marker("n", build)))
    assert release._installed(target, "n", build) is True
    (target / ".proxy-index-release.json").write_text("{}")
    with pytest.raises(release.ReleaseError):
        release._installed(target, "n", build)


def test_the_watch_agent_replaces_and_removes_the_old_one(tmp_path):
    calls = []

    def run(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    old = guard.plist_path(tmp_path, "com.proxy-index.campaign-watch")
    old.parent.mkdir(parents=True)
    old.write_text("old")
    path = guard.install(["/bin/echo"], tmp_path, run)
    assert guard.LABEL == "io.atlas.campaign-watch" and path.name == f"{guard.LABEL}.plist"
    assert not old.exists() and any(a[-1].endswith("/com.proxy-index.campaign-watch") for a in calls)
    old.write_text("old")
    path.unlink()
    assert guard.remove(tmp_path, run) is True and not old.exists()
    assert guard.remove(tmp_path, run) is False


def test_the_mac_key_defaults_to_the_old_one_only_while_it_alone_exists(tmp_path):
    ssh = tmp_path / ".ssh"
    assert cli.default_mac_key(tmp_path) == ssh / "atlas-campaign"
    ssh.mkdir()
    (ssh / "proxy-index-campaign").write_text("old")
    assert cli.default_mac_key(tmp_path) == ssh / "proxy-index-campaign"
    (ssh / "atlas-campaign").write_text("new")
    assert cli.default_mac_key(tmp_path) == ssh / "atlas-campaign"
