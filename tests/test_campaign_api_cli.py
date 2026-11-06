"""The campaign command group, `campaign limits` and the DigitalOcean token sources."""

from __future__ import annotations

import argparse
import json

import pytest
from campaign_api_cases import IP, OURS
from campaign_fakes import CAMPAIGN_PROJECT, SID, TOKEN, FakeDigitalOcean, running

from proxy_index import cli as main_cli
from proxy_index.campaign import cli as campaign_cli
from proxy_index.campaign import do_api
from proxy_index.campaign.contracts import ACCOUNTS, BUILDER_TAG, MANAGER_TAG, WORKER_TAG
from proxy_index.campaign.do_api import TokenError


def test_token_sources_read_memory_only_and_refuse_loose_files(tmp_path, monkeypatch):
    token = tmp_path / "do-token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    assert do_api.file_token(token)() == TOKEN
    link = tmp_path / "link"
    link.symlink_to(token)
    empty = tmp_path / "empty"
    empty.write_text("")
    empty.chmod(0o600)
    token.chmod(0o644)
    for path in (token, link, empty):
        with pytest.raises(TokenError):
            do_api.file_token(path)()
    calls = []

    def run(command, **_):
        calls.append(command)
        found = type(
            "Done",
            (),
            {
                "returncode": 0 if len(calls) == 1 else 44,
                "stdout": TOKEN + "\n" if len(calls) == 1 else "",
            },
        )
        return found

    monkeypatch.setattr(do_api.subprocess, "run", run)
    read = do_api.keychain_token(ACCOUNTS["leftclick"])
    assert read() == TOKEN
    with pytest.raises(TokenError) as missing:
        read()
    assert calls[0] == [
        "security",
        "find-generic-password",
        "-s",
        "Leftclick DigitalOcean",
        "-a",
        "team: Leftclick",
        "-w",
    ]
    assert TOKEN not in str(missing.value)


def test_campaign_command_group_is_registered(monkeypatch):
    monkeypatch.setitem(campaign_cli.HANDLERS, "probe", lambda args: 7)
    assert campaign_cli.dispatch(argparse.Namespace(campaign_action="probe")) == 7
    with pytest.raises(SystemExit):
        main_cli.main(["campaign"])


async def test_limits_reports_counts_only():
    fake = FakeDigitalOcean()
    fake.seed_production(30)
    fake.add(f"pic-{SID}-h03-lon1-shifter", OURS, project=CAMPAIGN_PROJECT)
    async with running(fake) as cloud:
        report = await campaign_cli.limits(cloud, "Proxy Index", ["c-2", "s-1vcpu-1gb", "x"])
    assert report["droplet_limit"] == 10000 and report["account_status"] == "active"
    assert report["our_tag_counts"] == {WORKER_TAG: 1, MANAGER_TAG: 0, BUILDER_TAG: 0}
    assert report["project"] == {"name": "Proxy Index", "droplets": 1}
    assert report["sizes"] == {"c-2": ["lon1", "fra1"], "s-1vcpu-1gb": [], "x": []}
    text = json.dumps(report)
    assert not IP.search(text) and TOKEN not in text and "owner-at-example" not in text
    listed = [q for m, p, q, _ in fake.requests if p == "/v2/droplets"]
    assert listed and all(q["tag_name"].startswith("pi-camp-") for q in listed)


def test_a_missing_keychain_token_names_the_expected_item(monkeypatch, capsys):
    def run(command, **_):
        return type("Missing", (), {"returncode": 44, "stdout": ""})

    monkeypatch.setattr(do_api.subprocess, "run", run)
    assert main_cli.main(["campaign", "limits", "--account", "leftclick"]) == 2
    error = capsys.readouterr().err
    assert "Command failed: no DigitalOcean token for account leftclick" in error
    assert "'Leftclick DigitalOcean'" in error and "'team: Leftclick'" in error
    assert "(TokenError)" not in error


def test_campaign_limits_command_is_wired(monkeypatch, capsys):
    seen = {}

    async def limits(cloud, project, sizes):
        seen.update(cloud=cloud, project=project, sizes=sizes)
        return {"droplet_limit": 1}

    monkeypatch.setattr(campaign_cli, "limits", limits)
    monkeypatch.setattr(do_api, "keychain_token", lambda account: lambda: TOKEN)
    assert main_cli.main(["campaign", "limits", "--account", "leftclick"]) == 0
    assert json.loads(capsys.readouterr().out) == {"droplet_limit": 1}
    assert seen["project"] == "Proxy Index" and seen["sizes"] == list(campaign_cli.DEFAULT_SIZES)
    assert isinstance(seen["cloud"], do_api.DigitalOcean)
