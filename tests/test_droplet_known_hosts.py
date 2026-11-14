"""PULSE-KNOWNHOSTS-1: every droplet has its own known_hosts file, so a droplet that reuses an
old droplet's address (with a new host key) is still accepted; a failed ready check says why
(from ssh's -E log) and a changed host key is retried once with a fresh file. A fake ssh that
honours StrictHostKeyChecking=accept-new answers; addresses are RFC 5737 documentation ones."""

from __future__ import annotations

import os
import time
from dataclasses import replace

from campaign_hour_cases import DROPLET, FakeClock

from atlas.campaign import fanout, pulse_fleet

ADDRESS = DROPLET.public_ipv4


class FakeSsh:
    """ssh with accept-new on the file it is given: a known address with another key refuses."""

    def __init__(self, keys, *, down=(), refuse_keys=False):
        self.keys, self.down, self.refuse_keys, self.calls = dict(keys), set(down), refuse_keys, []

    async def __call__(self, argv, stdin, seconds, sink, limit):
        options = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-o")
        hosts, log = options["UserKnownHostsFile"], argv[argv.index("-E") + 1]
        address = argv[-3]
        self.calls.append(hosts)
        if address in self.down:
            return self._fail(log, f"ssh: connect to host {address} port 22: Connection timed out")
        known = dict(line.split() for line in _lines(hosts))
        key = self.keys[address]
        if self.refuse_keys or (address in known and known[address] != key):
            return self._fail(log, "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\n"
                              "Host key verification failed.")  # fmt: skip
        if address not in known:
            _append(hosts, f"{address} {key}")  # accept-new
        return 0

    @staticmethod
    def _fail(log, text):
        _append(log, text)
        return 255


def _append(path, text):
    with open(path, "a") as file:
        file.write(text + "\n")


def _lines(path):
    return open(path).read().splitlines() if os.path.exists(path) else []


def remote(tmp_path, runner, clock=None):
    clock = clock or FakeClock(0.0)
    return fanout.OpenSSHRemote(
        tmp_path / "key", tmp_path / "rt", 0, runner=runner, sleep=clock.sleep, clock=clock.now
    )


async def test_a_droplet_reusing_an_old_droplets_address_with_a_new_key_still_connects(tmp_path):
    old, new = DROPLET, replace(DROPLET, id="700002", name="atlas-pulse-lon1-x")
    ssh = FakeSsh({ADDRESS: "key-of-the-old-droplet"})
    assert await remote(tmp_path, ssh).wait_ready(old, 30)
    ssh.keys[ADDRESS] = "key-of-the-new-droplet"  # DigitalOcean gave the address again
    assert await remote(tmp_path, ssh).wait_ready(new, 30)
    hosts = tmp_path / "rt" / "known_hosts"
    assert _lines(hosts / old.id) == [f"{ADDRESS} key-of-the-old-droplet"]
    assert _lines(hosts / new.id) == [f"{ADDRESS} key-of-the-new-droplet"]
    # One file shared by both, as before, would have refused the new droplet.
    shared = FakeSsh({ADDRESS: "key-of-the-new-droplet"})
    (hosts / new.id).write_text(f"{ADDRESS} key-of-the-old-droplet\n")
    assert await shared(remote(tmp_path, shared).argv(new, ["true"]), None, 5, None, 0) == 255


async def test_a_changed_host_key_is_retried_once_with_a_fresh_file(tmp_path):
    ssh = FakeSsh({ADDRESS: "the-key-now"})
    hosts = tmp_path / "rt" / "known_hosts"
    hosts.mkdir(parents=True)
    (hosts / DROPLET.id).write_text(f"{ADDRESS} a-key-before-a-rebuild\n")
    ready = remote(tmp_path, ssh)
    assert await ready.wait_ready(DROPLET, 30) and len(ssh.calls) == 2
    assert ready.ready_failure(DROPLET) is None
    assert _lines(hosts / DROPLET.id) == [f"{ADDRESS} the-key-now"]


async def test_a_key_refused_again_after_the_fresh_file_is_not_retried_without_end(tmp_path):
    clock = FakeClock(0.0)
    ssh = FakeSsh({ADDRESS: "k"}, refuse_keys=True)
    ready = remote(tmp_path, ssh, clock)
    assert not await ready.wait_ready(DROPLET, 10)
    assert ready.ready_failure(DROPLET) == "host_key_changed"
    # The fresh retry once, then the ordinary 2-second polls until the limit.
    assert len(ssh.calls) == 1 + 1 + 4 and clock.t == 8.0


async def test_an_unreachable_droplet_reports_why(tmp_path):
    ready = remote(tmp_path, FakeSsh({ADDRESS: "k"}, down=[ADDRESS]))
    assert not await ready.wait_ready(DROPLET, 4)
    assert ready.ready_failure(DROPLET) == "unreachable"


def test_refusal_reads_the_ssh_log(tmp_path):
    log = tmp_path / "x.log"
    assert fanout.refusal(log) == "ssh_failed"  # no log at all
    for text, reason in (
        ("root@203.0.113.7: Permission denied (publickey).", "auth_failed"),
        ("Connection closed by 203.0.113.7 port 22", "unreachable"),
        ("kex_exchange_identification: read: Connection reset", "ssh_failed"),
    ):
        log.write_text(text + "\n")
        assert fanout.refusal(log) == reason


async def test_old_known_hosts_files_are_pruned_once_per_remote(tmp_path):
    hosts = tmp_path / "rt" / "known_hosts"
    hosts.mkdir(parents=True)
    stale, fresh = hosts / "600001", hosts / "600002"
    for path in (stale, fresh, hosts / "600001.log"):
        path.write_text("x\n")
    old = time.time() - 3 * 86400
    os.utime(stale, (old, old))
    os.utime(hosts / "600001.log", (old, old))
    assert await remote(tmp_path, FakeSsh({ADDRESS: "k"})).wait_ready(DROPLET, 5)
    assert sorted(p.name for p in hosts.iterdir()) == ["600002", DROPLET.id]


def test_a_boot_failure_records_its_reason():
    class Refused:
        def ready_failure(self, droplet):
            return "host_key_changed"

    assert pulse_fleet._boot_reason(Refused(), DROPLET) == "host_key_changed"
    assert pulse_fleet._boot_reason(object(), DROPLET) == "ssh_failed"  # a remote without it
    assert pulse_fleet._boot_reason(Refused(), None) == "not_active"
