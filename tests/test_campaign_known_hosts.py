"""KH-REL-1: every SSH call to the manager finds the inventory's host-key file beside the
inventory, from any directory, and passes it to ssh as one quoted value (fakes only)."""

from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest
from campaign_deploy_fakes import (
    CID,
    MANAGER_IP,
    NOW,
    Fake,
    Shell,
    fast_assign,
    make_job,
    run_deploy,
)
from campaign_fakes import CAMPAIGN_PROJECT

from proxy_index.campaign import collect, deploy, teardown, watch

RECORD = ".secrets/campaign-manager.known_hosts"  # what the default inventory path records


def quoted(path: Path) -> str:
    """The expected option: the absolute path as one quoted value (no `%`, `"` or `\\` here)."""
    return f'UserKnownHostsFile="{path}"'


def options(argvs) -> set[str]:
    return {arg for argv in argvs for arg in argv if arg.startswith("UserKnownHostsFile")}


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A checkout whose path holds a space, run from another directory."""
    root = tmp_path / "Proxy reviews"
    (root / ".secrets").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / ".secrets").mkdir(parents=True)  # another manager's file must never be used
    monkeypatch.chdir(elsewhere)
    return root


def own(checkout: Path) -> str:
    return quoted(checkout / RECORD)


async def test_deploy_ssh_quotes_a_host_key_path_with_a_space(checkout, monkeypatch):
    fast_assign(monkeypatch)
    job = make_job(checkout)
    shell = Shell()
    await run_deploy(Fake(), job, shell)
    assert shell.calls and options(argv for argv, _ in shell.calls) == {
        quoted(job.inventory.with_name("campaign-manager.known_hosts"))
    }


@pytest.fixture
def recorded(checkout):
    """A deployed inventory with the relative record, named by a relative path from `/`-like
    elsewhere (as `--inventory` may be given)."""
    path = checkout / ".secrets" / "campaign-manager.json"
    deploy.save(
        path,
        {},
        manager_id="500001",
        host=MANAGER_IP,
        user="root",
        identity="k",
        identity_file="k",
        known_hosts=RECORD,
        project_id=CAMPAIGN_PROJECT,
        campaigns=[CID],
        campaign_id=CID,
        specs={CID: {"starts_at": "2027-01-01T23:00:00+00:00", "synthetic": True}},
    )
    return Path(os.path.relpath(path))


async def test_enable_and_disable_use_the_record_beside_the_inventory(checkout, recorded):
    go = checkout / "go.md"
    go.write_text("Owner: go.\n")
    shell = Shell()
    await deploy.enable(recorded, CID, go, shell=shell, now=NOW)
    await deploy.disable(recorded, CID, shell=shell, now=NOW)
    assert len(shell.calls) == 2 and options(argv for argv, _ in shell.calls) == {own(checkout)}


async def test_upload_spec_uses_the_record_beside_the_inventory(checkout, recorded):
    from test_campaign_deploy import planned_root

    deploy.save(
        recorded,
        json.loads(recorded.read_text()),
        campaigns=[],
        wheel_sha256="ab" * 32,
        account="leftclick",
    )
    shell = Shell()
    await deploy.upload_spec(recorded, planned_root(checkout), shell=shell)
    assert len(shell.calls) == 1 and options(argv for argv, _ in shell.calls) == {own(checkout)}


def test_collect_uses_the_record_beside_the_inventory(checkout, recorded, monkeypatch):
    seen = []

    def popen(argv, **_kwargs):
        seen.append(argv)
        raise OSError("no SSH in tests")

    monkeypatch.setattr(collect.subprocess, "Popen", popen)
    deploy.save(recorded, json.loads(recorded.read_text()), host="192.0.2.10")
    with pytest.raises(OSError, match="no SSH"):
        collect.collect(recorded, CID, checkout / "collected")
    assert options(seen) == {own(checkout)}


def test_the_watch_uses_the_record_beside_the_inventory(checkout, recorded, monkeypatch):
    """The launchd agent runs from `/`: the record must not resolve in the current directory."""
    seen = []

    def run(argv, **_kwargs):
        seen.append(argv)
        return types.SimpleNamespace(
            stdout=json.dumps({"status": None, "alerts": [], "offset": 0, "reports": {}, "tsv": ""})
        )

    monkeypatch.setattr(watch.subprocess, "run", run)
    io = watch.WatchIO(notify=lambda _text: None)
    watch.run_watch(recorded, checkout / "STATUS.md", checkout / "traffic", io=io, iterations=1)
    assert options(seen) == {own(checkout)}


async def test_two_inventories_in_one_folder_keep_their_own_host_key_files(tmp_path, monkeypatch):
    """F1: each inventory records `<stem>.known_hosts`, so retiring one manager never unlinks
    the other's pinned key; the default inventory keeps `campaign-manager.known_hosts`."""
    from dataclasses import replace

    fast_assign(monkeypatch)
    fake, first = Fake(), make_job(tmp_path)
    second = replace(first, inventory=first.inventory.with_name("campaign-manager-b.json"))
    for job in (first, second):
        await run_deploy(fake, job)
    files = [
        deploy.known_hosts(job.inventory, json.loads(job.inventory.read_text()))
        for job in (first, second)
    ]
    assert [f.name for f in files] == [
        "campaign-manager.known_hosts",
        "campaign-manager-b.known_hosts",
    ]
    for file in files:
        file.write_text("pinned host key\n")
    teardown._retire(second.inventory, json.loads(second.inventory.read_text()), {})
    assert files[0].exists() and not files[1].exists()
