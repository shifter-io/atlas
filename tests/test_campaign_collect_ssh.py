"""The manager SSH prefix of verified collect, with and without a pinned known-hosts file."""

import os

from proxy_index.campaign.collect import ssh_command

BASE = {"host": "manager.example", "identity_file": "~/.ssh/id_campaign"}
IDENTITY = os.path.expanduser("~/.ssh/id_campaign")


def test_without_known_hosts_the_argv_is_unchanged():
    assert ssh_command(BASE) == [
        "ssh", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=15",
        "-i", IDENTITY, "root@manager.example"]
    assert ssh_command(BASE | {"known_hosts": ""}) == ssh_command(BASE)


def test_known_hosts_pins_the_manager_key_strictly():
    argv = ssh_command(BASE | {"user": "ops", "known_hosts": "~/.secrets/manager.known_hosts"})
    assert argv == [
        "ssh", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=15",
        "-o", f"UserKnownHostsFile={os.path.expanduser('~/.secrets/manager.known_hosts')}",
        "-o", "StrictHostKeyChecking=yes", "-i", IDENTITY, "ops@manager.example"]
