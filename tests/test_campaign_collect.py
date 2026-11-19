"""Verified collect: the "manager" is a local directory and commands run in a local shell.

Offline, RFC 5737 addresses only; the campaign data comes from `campaign_data_cases.py`.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from campaign_data_cases import CID

from atlas.campaign import collect as collector
from atlas.cli import main
from atlas.onboarding.files import read_json

pytest_plugins = ["campaign_data_cases"]


def roomy(path):
    return shutil._ntuple_diskusage(10**12, 10**11, 9 * 10**11)


@pytest.fixture
def manager(root, tmp_path, monkeypatch):
    # The free-space rule is tested on its own below; collect tests never read the real disk.
    monkeypatch.setattr(collector.shutil, "disk_usage", roomy)
    campaigns = tmp_path / "manager" / "campaigns"
    remote = Path(shutil.copytree(root, campaigns / CID))
    (remote / ".runtime").mkdir(mode=0o700)
    (remote / ".runtime" / "hosts-00.json").write_text('{"700001": "203.0.113.10"}')
    inventory = tmp_path / "campaign-manager.json"
    inventory.write_text(
        json.dumps(
            {"host": "192.0.2.10", "identity_file": "~/.ssh/k", "campaigns_root": str(campaigns)}
        )
    )
    return remote, inventory


# macOS tar would add AppleDouble `._*` entries that a Linux manager never sends.
SHELL_ENV = os.environ | {"COPYFILE_DISABLE": "1"}
POPEN = subprocess.Popen  # kept: one test replaces `subprocess.Popen` for the SSH path


def sh(command, **kwargs):
    return POPEN(["sh", "-c", command], env=SHELL_ENV, **kwargs)


def local_shell(before_tar=None):
    def spawn(command):
        if command.startswith("tar ") and before_tar:
            before_tar()
        return sh(command, stdout=subprocess.PIPE)

    return spawn


def test_collect_downloads_and_verifies_every_file(manager, tmp_path):
    remote, inventory = manager
    dest = tmp_path / "archive" / f"campaign-{CID}"
    report = collector.collect(inventory, CID, dest, spawn=local_shell())
    files = [p for p in remote.rglob("*") if p.is_file() and ".runtime" not in p.parts]
    assert report["clean"] and report["files"] == report["verified"] == len(files) - 1
    assert (report["mismatches"], report["missing"], report["extra"]) == ([], [], [])
    assert not (dest / ".runtime").exists() and (dest / collector.MANIFEST).is_file()
    assert read_json(dest / collector.REPORT) == report
    with pytest.raises(ValueError, match="already holds files"):
        collector.collect(inventory, CID, dest, spawn=local_shell())


def test_collect_detects_a_change_a_missing_and_an_extra_file(manager, tmp_path):
    remote, inventory = manager

    def tamper():
        (remote / "status.json").write_text('{"changed": true}')
        (remote / "campaign-spec.json").unlink()
        (remote / "alerts.jsonl").write_text("{}\n")

    (remote / "status.json").write_text("{}")
    report = collector.collect(inventory, CID, tmp_path / "a", spawn=local_shell(tamper))
    assert not report["clean"]
    assert (report["mismatches"], report["missing"], report["extra"]) == (
        ["status.json"],
        ["campaign-spec.json"],
        ["alerts.jsonl"],
    )


def test_collect_refuses_unsafe_or_failed_transfers(manager, tmp_path, monkeypatch):
    remote, inventory = manager

    def replace_manifest():
        (remote / collector.MANIFEST).write_text("")

    report = collector.collect(inventory, CID, tmp_path / "a", spawn=local_shell(replace_manifest))
    assert report["mismatches"] == [collector.MANIFEST] and not report["clean"]
    (remote / "link").symlink_to("/etc/hosts")
    with pytest.raises(RuntimeError, match="manifest failed"):
        collector.collect(inventory, CID, tmp_path / "b", spawn=local_shell())
    (remote / "link").unlink()
    with pytest.raises(RuntimeError, match="stream failed"):
        collector.collect(
            inventory,
            CID,
            tmp_path / "c",
            spawn=lambda c: sh(
                c + (" ; exit 2" if c.startswith("tar") else ""), stdout=subprocess.PIPE
            ),
        )
    monkeypatch.setattr(
        collector.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100, 90, 10)
    )
    with pytest.raises(ValueError, match="disk space"):
        collector.collect(inventory, CID, tmp_path / "d", spawn=local_shell())
    with pytest.raises(ValueError, match="campaign id"):
        collector.collect(inventory, "not-a-campaign", tmp_path / "e", spawn=local_shell())


def test_collect_cli_uses_ssh_from_the_inventory(manager, tmp_path, monkeypatch, capsys):
    _, inventory = manager
    commands = []

    def popen(argv, **kwargs):
        commands.append(argv)
        return sh(argv[-1], **kwargs)

    monkeypatch.setattr(collector.subprocess, "Popen", popen)
    dest = tmp_path / "archive"
    assert (
        main(
            [
                "campaign",
                "collect",
                "--inventory",
                str(inventory),
                "--campaign-id",
                CID,
                "--dest",
                str(dest),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["clean"] is True
    assert commands[0][:-1] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "StrictHostKeyChecking=yes",
        "-i",
        str(Path("~/.ssh/k").expanduser()),
        "root@192.0.2.10",
    ]
    (dest / "campaign-spec.json").write_text("{}")
    report = collector.verify_tree(
        dest, collector.parse_manifest((dest / collector.MANIFEST).read_text() + "size 1\n")[0]
    )
    assert report["mismatches"] == ["campaign-spec.json"] and not report["clean"]


@pytest.mark.parametrize("free, ok", [(3000, True), (2999, False), (1249, False)])
def test_free_space_keeps_a_fifth_of_the_disk_and_a_quarter_spare(monkeypatch, free, ok):
    # 1,000 bytes on a 10,000-byte disk: 2,000 must stay free afterwards (and 1,250 spare).
    monkeypatch.setattr(
        collector.shutil,
        "disk_usage",
        lambda p: shutil._ntuple_diskusage(10000, 10000 - free, free),
    )
    if ok:
        collector.check_space(Path("."), 1000)
    else:
        with pytest.raises(ValueError, match="disk space"):
            collector.check_space(Path("."), 1000)


@pytest.mark.parametrize(
    "data, message",
    [
        ({"host": "manager.example"}, "does not appear to be an IPv4 or IPv6 address"),
        ({"host": "192.0.2.10", "user": "Root"}, "SSH user"),
        ({"host": "192.0.2.10", "campaigns_root": "relative"}, "absolute"),
    ],
)
def test_inventory_validation(tmp_path, data, message):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=message):
        collector.load_inventory(path)


@pytest.mark.parametrize("text", ["", "x", "abc  ./f\nsize 1", f"{'0' * 64}  ./../f\nsize 1"])
def test_manifest_parsing_rejects_bad_lines(text):
    with pytest.raises(ValueError):
        collector.parse_manifest(text)
