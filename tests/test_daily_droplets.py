"""MONITOR-1 (#1094): `atlas daily droplets`, the Slack alerts' read-only droplet checks, with a
fake cloud that has only the read calls (list_tag, close): a delete or write would fail."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from atlas.campaign import do_api
from atlas.campaign.cloud_types import Droplet
from atlas.campaign.collector import MANAGER_ROOT
from atlas.campaign.contracts import PULSE_TAG, WORKER_TAG
from atlas.campaign.do_api import TokenError
from atlas.cli import main
from atlas.daily import cli, verify_cloud

NOW = datetime(2027, 1, 1, 13, 40, tzinfo=UTC)


def droplet(name, tag, age):
    return Droplet(name, name, "fra1", "s-1vcpu-1gb-amd", "active", NOW - age, (tag,))


class FakeCloud:
    def __init__(self, found=(), error=None):
        self.found, self.error, self.closed = list(found), error, False

    async def list_tag(self, tag):
        if self.error:
            raise self.error
        return [d for d in self.found if tag in d.tags]

    async def close(self):
        self.closed = True


def test_orphans_per_tag_and_the_count_against_the_cap():
    cloud = FakeCloud(
        [
            droplet("worker-a", WORKER_TAG, timedelta(minutes=89)),
            droplet("pulse-b", PULSE_TAG, timedelta(minutes=91)),
            droplet("pulse-c", PULSE_TAG, timedelta(minutes=5)),
        ]
    )
    checks = verify_cloud.watch(cloud, NOW, cap=3)
    assert [(c.subject, c.ok, c.detail) for c in checks] == [
        (WORKER_TAG, True, "1 up, 0 orphans"),
        (PULSE_TAG, False, "2 up, 1 orphans: pulse-b"),
        ("count", True, "3 up"),
    ]
    assert cloud.closed
    over = verify_cloud.watch(FakeCloud(cloud.found), NOW, cap=2)
    assert (over[-1].ok, over[-1].detail) == (False, "3 up, more than 2")


def test_an_unreadable_cloud_is_one_failed_check():
    cloud = FakeCloud(error=TokenError("token file is empty"))
    checks = verify_cloud.watch(cloud, NOW)
    assert [(c.subject, c.ok, c.detail) for c in checks] == [
        ("list", False, "not listed (TokenError)")
    ]
    assert cloud.closed


def test_the_command_prints_one_line_per_check(monkeypatch, capsys):
    cloud = FakeCloud([droplet("pulse-b", PULSE_TAG, timedelta(minutes=91))])
    tokens = []
    monkeypatch.setattr(do_api, "file_token", lambda path: tokens.append(path) or "t")
    monkeypatch.setattr(do_api, "DigitalOcean", lambda token: cloud)
    monkeypatch.setattr(cli, "utc_now", lambda: NOW)
    assert main(["daily", "droplets"]) == 3
    assert capsys.readouterr().out.splitlines() == [
        f"{WORKER_TAG}\tok\t0 up, 0 orphans",
        f"{PULSE_TAG}\tfailed\t1 up, 1 orphans: pulse-b",
        "count\tok\t1 up",
    ]
    assert tokens == [MANAGER_ROOT / ".secrets" / "do-token"]
    cloud.found = []
    assert main(["daily", "droplets", "--do-token-file", "t.txt", "--cap", "10"]) == 0
    assert tokens[-1].name == "t.txt" and cloud.closed
