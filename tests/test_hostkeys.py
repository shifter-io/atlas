"""SEC-SSH-PIN-1: a droplet's host key travels in its create request and is pinned before it
leaves; every SSH call checks it strictly; an existing server's key is pinned only when its
fingerprint is the one given, and refused otherwise. Real ssh-keygen, no network; addresses are
RFC 5737 documentation ones."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
import yaml
from campaign_api_cases import OURS, SID
from campaign_deploy_fakes import Fake
from campaign_fakes import no_sleep, running
from campaign_hour_cases import DROPLET

from atlas.campaign import fanout, hostkeys, image, ops_api
from atlas.campaign.hostkeys import HostKeyError
from atlas.cli import main

ADDRESS = "192.0.2.10"


@pytest.fixture(scope="module")
def keys():
    return hostkeys.new_host_key(), hostkeys.new_host_key()


def keygen(*args: str) -> str:
    return subprocess.run(["ssh-keygen", *args], capture_output=True, text=True).stdout


def test_cloud_init_installs_the_new_key_and_its_halves_belong_together(tmp_path, keys):
    key = keys[0]
    text = hostkeys.cloud_config(key)
    config = yaml.safe_load(text)
    assert text.startswith("#cloud-config\n") and config["ssh_deletekeys"] is True
    assert config["ssh_keys"]["ed25519_public"] == key.public
    private = tmp_path / "key"
    private.write_text(config["ssh_keys"]["ed25519_private"])
    private.chmod(0o600)
    assert keygen("-y", "-f", str(private)).split()[:2] == key.public.split()
    assert re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+=*", key.public)
    assert "PRIVATE" not in repr(key) and key.private not in repr(key)


def test_the_fingerprint_is_the_one_openssh_prints(tmp_path, keys):
    public = tmp_path / "key.pub"
    public.write_text(keys[0].public + "\n")
    assert hostkeys.parse_fingerprints(keygen("-l", "-f", str(public))) == {
        hostkeys.fingerprint(keys[0].public)
    }


def test_fingerprints_are_parsed_from_console_keygen_and_bare_text(keys):
    one, two = (hostkeys.fingerprint(key.public) for key in keys)
    console = (
        "ec2: -----BEGIN SSH HOST KEY FINGERPRINTS-----\n"
        f"ec2: 256 {one} root@host (ED25519)\n"
        "ec2: -----END SSH HOST KEY FINGERPRINTS-----\n"
    )
    assert hostkeys.parse_fingerprints(console) == {one}
    assert hostkeys.parse_fingerprints(f"{one}, {two}") == {one, two}
    for text in ("", "SHA256:short", one + "x", "MD5:" + one[7:], "x" + one):
        assert hostkeys.parse_fingerprints(text) == set(), text
    for bad in ("ssh-ed25519", "ssh-ed25519 not*base64", ""):
        with pytest.raises(HostKeyError):
            hostkeys.fingerprint(bad)


def scanner(*keys, host=ADDRESS):
    """`ssh-keyscan`'s answer: a comment line and each key line."""
    lines = [f"# {host}:22 SSH-2.0-OpenSSH_9.6"]
    lines += [f"{host} {key.public}" for key in keys]
    lines += [f"{host} ecdsa-sha2-nistp256 AAAAE2VjZHNh"]
    calls = []

    def scan(found_host, port):
        calls.append((found_host, port))
        return "\n".join(lines) + "\n"

    scan.calls = calls
    return scan


def test_a_server_key_is_pinned_only_when_its_fingerprint_matches(tmp_path, keys):
    file = tmp_path / "server.known_hosts"
    file.write_text(f"198.51.100.1 {keys[1].public}\n{ADDRESS} ssh-ed25519 AAAAold\n")
    wanted = f"256 {hostkeys.fingerprint(keys[0].public)} root@server (ED25519)"
    scan = scanner(keys[0])
    assert hostkeys.pin_scanned(ADDRESS, 22, wanted, file, scan) == wanted.split()[1]
    assert file.read_text() == f"198.51.100.1 {keys[1].public}\n{ADDRESS} {keys[0].public}\n"
    assert keygen("-F", ADDRESS, "-f", str(file)).count(keys[0].public) == 1
    assert oct(file.stat().st_mode & 0o777) == "0o600" and scan.calls == [(ADDRESS, 22)]
    hostkeys.pin_scanned(ADDRESS, 2222, wanted, file, scanner(keys[0]))
    assert keygen("-F", f"[{ADDRESS}]:2222", "-f", str(file)).count(keys[0].public) == 1


def test_a_mismatch_is_refused_and_nothing_is_pinned(tmp_path, keys):
    file = tmp_path / "server.known_hosts"
    other = hostkeys.fingerprint(keys[1].public)
    with pytest.raises(HostKeyError, match="does not match"):
        hostkeys.pin_scanned(ADDRESS, 22, other, file, scanner(keys[0]))
    with pytest.raises(HostKeyError, match="does not match"):
        hostkeys.pin_scanned(ADDRESS, 22, other, file, lambda host, port: "")
    with pytest.raises(HostKeyError, match="must hold"):
        hostkeys.pin_scanned(ADDRESS, 22, "not a fingerprint", file, scanner(keys[0]))
    assert not file.exists()


def test_pins_keep_each_names_last_keys_and_prune_stale_ones(keys):
    store = hostkeys.pin_store()
    for _ in range(hostkeys.KEEP):
        hostkeys.pin(["atlas-pulse-a", "atlas-pulse-b"], keys[1].public)
    hostkeys.pin(["atlas-pulse-a"], keys[0].public)
    lines = (store / "atlas-pulse-a").read_text().splitlines()
    assert len(lines) == hostkeys.KEEP and lines[-1] == f"atlas-pulse-a {keys[0].public}"
    assert oct(store.stat().st_mode & 0o777) == "0o700"
    stale = time.time() - (hostkeys.PIN_DAYS + 1) * 86400
    os.utime(store / "atlas-pulse-b", (stale, stale))
    hostkeys.pin(["atlas-pulse-c"], keys[0].public)
    assert sorted(p.name for p in store.iterdir()) == [
        hostkeys.SINCE,
        "atlas-pulse-a",
        "atlas-pulse-c",
    ]
    for bad in ("../x", "Name", ".since"):
        with pytest.raises(HostKeyError):
            hostkeys.pin([bad], keys[0].public)
        assert hostkeys.pinned(bad) is None


def test_only_droplets_older_than_the_first_pin_keep_trust_on_first_use(keys):
    assert hostkeys.legacy(DROPLET.created_at)  # nothing pinned here yet: an upgrade
    hostkeys.pin(["atlas-pulse-a"], keys[0].public)
    since = hostkeys.pin_store() / hostkeys.SINCE
    first = since.read_text()
    hostkeys.pin(["atlas-pulse-b"], keys[0].public)
    assert since.read_text() == first  # the first pin's time, kept
    now = datetime.fromisoformat(first.strip())
    assert hostkeys.legacy(now - timedelta(minutes=1))
    assert not hostkeys.legacy(now + timedelta(minutes=1))
    since.write_text("garbled\n")
    assert not hostkeys.legacy(now - timedelta(days=1))


def test_the_manager_file_gets_the_pinned_keys_by_address(tmp_path, keys):
    file = tmp_path / "manager.known_hosts"
    assert not hostkeys.publish(None, ADDRESS, file)
    assert not hostkeys.publish("atlas-camp-manager-x", ADDRESS, file) and not file.exists()
    hostkeys.pin(["atlas-camp-manager-x"], keys[0].public)
    assert hostkeys.publish("atlas-camp-manager-x", ADDRESS, file)
    assert file.read_text() == f"{ADDRESS} {keys[0].public}\n"


def options(argv):
    return [argv[i + 1] for i, item in enumerate(argv) if item == "-o"]


def test_a_pinned_droplet_is_reached_only_with_its_key(tmp_path, keys):
    hostkeys.pin([DROPLET.name], keys[0].public)
    argv = fanout.OpenSSHRemote(tmp_path / "key", tmp_path / "rt", 0).argv(DROPLET, ["true"])
    pinned = hostkeys.pin_store() / DROPLET.name
    assert {
        f"HostKeyAlias={DROPLET.name}",
        hostkeys.known_hosts_option(pinned),
        "StrictHostKeyChecking=yes",
        "HostKeyAlgorithms=ssh-ed25519",
    } <= set(options(argv))
    assert not [o for o in options(argv) if "accept-new" in o]
    assert len([o for o in options(argv) if o.startswith("UserKnownHostsFile")]) == 1


def test_an_unpinned_droplet_newer_than_the_first_pin_is_refused(tmp_path, keys):
    hostkeys.pin(["atlas-pulse-other"], keys[0].public)
    since = datetime.fromisoformat((hostkeys.pin_store() / hostkeys.SINCE).read_text().strip())
    newer = replace(DROPLET, created_at=since + timedelta(minutes=5))
    older = replace(DROPLET, created_at=since - timedelta(days=1))
    remote = fanout.OpenSSHRemote(tmp_path / "key", tmp_path / "rt", 0)
    assert "StrictHostKeyChecking=yes" in options(remote.argv(newer, ["true"]))
    assert "StrictHostKeyChecking=accept-new" in options(remote.argv(older, ["true"]))


async def test_a_create_request_carries_the_key_pinned_for_its_droplets():
    fake = Fake()  # records the create bodies
    names = [f"atlas-{SID}-h03-lon1-net{i:02d}" for i in range(12)]
    async with running(fake) as cloud:
        await cloud.create(
            names, region="lon1", size="c-2", image="ubuntu-24-04-x64", tags=OURS, ssh_keys=["9"]
        )
    creates = [body for _, path, body in fake.bodies if path == "/v2/droplets"]
    assert [len(body["names"]) for body in creates] == [10, 2]
    publics = [yaml.safe_load(b["user_data"])["ssh_keys"]["ed25519_public"] for b in creates]
    assert publics[0] != publics[1]  # one new key per request
    for body, public in zip(creates, publics, strict=True):
        for name in body["names"]:
            assert hostkeys.pinned(name).read_text() == f"{name} {public}\n"


async def test_a_builder_without_a_pinned_key_gets_nothing(tmp_path):
    calls = []

    async def shell(argv, stdin):
        calls.append(argv)
        return 0, "clean\n"

    with pytest.raises(ops_api.OpsError, match="not pinned"):
        await image.provision(shell, None, DROPLET, tmp_path, no_sleep)
    assert calls == []


def test_the_cli_pins_a_matching_key_and_refuses_another(tmp_path, monkeypatch, capsys, keys):
    file = tmp_path / "server.known_hosts"
    monkeypatch.setattr(hostkeys, "keyscan", scanner(keys[0]))
    command = ["campaign", "manager", "pin-host-key", "--host", ADDRESS, "--known-hosts"]
    wanted = hostkeys.fingerprint(keys[0].public)
    assert main([*command, str(file), "--fingerprint", wanted]) == 0
    assert json.loads(capsys.readouterr().out) == {"pinned": wanted, "known_hosts": str(file)}
    other = hostkeys.fingerprint(keys[1].public)
    assert main([*command, str(tmp_path / "other"), "--fingerprint", other]) == 2
    assert "does not match" in capsys.readouterr().err and not (tmp_path / "other").exists()


def test_a_failed_keygen_or_keyscan_is_a_typed_refusal(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "ssh-keyscan" and argv[-1] == "down.invalid":
            raise subprocess.TimeoutExpired(argv, 60)
        return subprocess.CompletedProcess(argv, 1, stdout="answer\n")

    monkeypatch.setattr(hostkeys.subprocess, "run", run)
    with pytest.raises(HostKeyError, match="ssh-keygen"):
        hostkeys.new_host_key()
    assert hostkeys.keyscan(ADDRESS, 2222) == "answer\n"
    assert calls[-1] == ["ssh-keyscan", "-T", "10", "-t", "ed25519", "-p", "2222", ADDRESS]
    with pytest.raises(HostKeyError, match="ssh-keyscan"):
        hostkeys.keyscan("down.invalid", 22)


def test_each_environment_root_has_its_own_pin_store(monkeypatch, tmp_path):
    from atlas.campaign import collector

    assert hostkeys.ROOT_ENV == collector.ROOT_ENV
    monkeypatch.delenv(hostkeys.STORE_ENV)
    monkeypatch.setenv(hostkeys.ROOT_ENV, str(tmp_path / "staging"))
    assert hostkeys.pin_store() == tmp_path / "staging" / ".host-keys"
    monkeypatch.delenv(hostkeys.ROOT_ENV)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert hostkeys.pin_store() == tmp_path / ".local" / "state" / "atlas" / "host-keys"
    monkeypatch.setenv(hostkeys.STORE_ENV, str(tmp_path / "chosen"))
    assert hostkeys.pin_store() == tmp_path / "chosen"
