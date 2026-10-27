"""The manager SSH prefix of verified collect, with and without a pinned known-hosts file."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from proxy_index.campaign.collect import ssh_command
from proxy_index.campaign.hostkeys import known_hosts, known_hosts_option

BASE = {"host": "manager.example", "identity_file": "~/.ssh/id_campaign"}
IDENTITY = os.path.expanduser("~/.ssh/id_campaign")
INVENTORY = Path("/private/inventory/campaign-manager.json")


def test_without_known_hosts_the_argv_is_unchanged():
    assert ssh_command(BASE, INVENTORY) == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout=15",
        "-i",
        IDENTITY,
        "root@manager.example",
    ]
    assert ssh_command(BASE | {"known_hosts": ""}, INVENTORY) == ssh_command(BASE, INVENTORY)


def test_known_hosts_pins_the_manager_key_strictly():
    argv = ssh_command(
        BASE | {"user": "ops", "known_hosts": "~/.secrets/manager.known_hosts"}, INVENTORY
    )
    expanded = os.path.expanduser("~/.secrets/manager.known_hosts")
    assert argv == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        f'UserKnownHostsFile="{expanded}"',
        "-o",
        "StrictHostKeyChecking=yes",
        "-i",
        IDENTITY,
        "ops@manager.example",
    ]


def test_a_relative_record_is_found_beside_the_inventory_from_another_directory(
    tmp_path, monkeypatch
):
    """KH-REL-1: the default inventory records `.secrets/campaign-manager.known_hosts`; run
    from another directory (the launchd watch runs from `/`) it must not resolve there."""
    inventory = tmp_path / "checkout" / ".secrets" / "campaign-manager.json"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    argv = ssh_command(
        BASE | {"known_hosts": ".secrets/campaign-manager.known_hosts"},
        Path(os.path.relpath(inventory)),
    )
    own = inventory.with_name("campaign-manager.known_hosts")
    assert f'UserKnownHostsFile="{own}"' in argv


def test_absolute_and_home_records_are_unchanged(tmp_path):
    assert known_hosts(INVENTORY, {"known_hosts": "/srv/kh"}) == Path("/srv/kh")
    assert known_hosts(INVENTORY, {"known_hosts": "~/kh"}) == Path.home() / "kh"
    assert known_hosts(INVENTORY, {"known_hosts": "kh"}) == INVENTORY.with_name("kh")


def test_a_path_with_a_space_is_one_quoted_value():
    assert known_hosts_option("/a b/kh") == 'UserKnownHostsFile="/a b/kh"'
    assert known_hosts_option('/a"b\\c%d') == 'UserKnownHostsFile="/a\\"b\\\\c%%d"'


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs the OpenSSH client")
def test_ssh_reads_the_quoted_option_as_one_file():
    """ssh's own parser: unquoted, `/tmp/a  b/known_hosts` is two files (printed joined by one
    space) and a `%` is a token; quoted and escaped, it is the one exact path."""
    for path in ("/tmp/a  b/known_hosts", "/tmp/a 100% b/known_hosts"):
        dump = subprocess.run(
            ["ssh", "-G", "-o", known_hosts_option(path), "host.invalid"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
        assert f"userknownhostsfile {path}\n" in dump
