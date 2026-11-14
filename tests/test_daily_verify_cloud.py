"""VERIFY-HOUR-1 (2/2): the hour's droplet and raw-archive checks, with fake clouds.

The fakes have only the read calls the report may make (DigitalOcean.list_tag and close,
Spaces.list_keys), so any other call would fail the test: the report never deletes or writes.
"""

from __future__ import annotations

from datetime import timedelta

from test_daily_verify import NOW, H, stock

from atlas.archive import ArchiveError
from atlas.campaign import do_api
from atlas.campaign.cloud_types import Droplet
from atlas.campaign.collector import MANAGER_ROOT
from atlas.campaign.contracts import PULSE_TAG, WORKER_TAG
from atlas.campaign.do_api import TokenError
from atlas.cli import main
from atlas.daily import cli, verify_cloud
from atlas.daily.verify import Hour
from atlas.orchestrator.control import write_settings
from atlas.storage import connect

# test_daily_verify's fixtures (formulas) and, through it, the database ones.
pytest_plugins = ["test_storage", "calculated_template", "test_daily_verify"]


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


class FakeSpaces:
    def __init__(self, keys):
        self.keys = keys

    def list_keys(self, prefix):
        return [key for key in self.keys if key.startswith(prefix)]


def test_droplets_older_than_their_hour_are_orphans():
    cloud = FakeCloud(
        [
            droplet("pi-camp-worker-a", WORKER_TAG, timedelta(minutes=50)),
            droplet("pi-pulse-fra1-alpha", PULSE_TAG, timedelta(minutes=91)),
        ]
    )
    checks = verify_cloud.droplets(cloud, Hour(H, NOW))
    assert [(c.subject, c.ok, c.detail) for c in checks] == [
        (WORKER_TAG, True, "1 up, 0 orphans"),
        (PULSE_TAG, False, "1 up, 1 orphans: pi-pulse-fra1-alpha"),
        ("total", False, "2 up now (read only)"),
    ]
    assert cloud.closed


def test_an_unreadable_cloud_fails_only_its_check():
    cloud = FakeCloud(error=TokenError("token file is empty"))
    checks = verify_cloud.droplets(cloud, Hour(H, NOW))
    assert [(c.ok, c.detail) for c in checks] == [(False, "not listed (TokenError)")]
    assert cloud.closed


def test_each_network_of_the_hour_has_its_raw_files(calculated_database):
    stock(calculated_database)
    hour = Hour(H, H + timedelta(hours=1, minutes=10))
    off = verify_cloud.archive(calculated_database, hour)
    assert [(c.ok, c.detail) for c in off] == [
        (None, "off (archive.enabled or raw.spool_dir is off)")
    ]
    with connect(calculated_database) as conn:
        write_settings(conn, {"archive.enabled": True, "raw.spool_dir": "spool"})
    # alpha's under the hour, beta's in the next one (imported after the hour ended).
    keys = ["raw-files/2027/01/01/13/alpha.tar.zst", "raw-files/2027/01/01/14/beta.1.tar.zst"]
    asked = []

    def spaces(path):
        asked.append(path)
        return FakeSpaces(keys)

    checks = verify_cloud.archive(calculated_database, hour, spaces)
    assert [(c.subject, c.ok) for c in checks] == [("alpha", True), ("beta", True), ("total", True)]
    # The setting archive.spaces_file, under the orchestrator's working directory.
    assert asked == [MANAGER_ROOT / ".secrets/spaces.toml"]
    early = verify_cloud.archive(calculated_database, hour, lambda _: FakeSpaces(keys[:1]))
    assert [(c.subject, c.ok, c.detail) for c in early][1:] == [
        ("beta", None, "no raw files in Spaces"),  # the next hour is not uploaded yet
        ("total", None, "1 of 2 networks' raw files in Spaces"),
    ]
    late = Hour(H, H + timedelta(hours=2, minutes=15))
    missed = verify_cloud.archive(calculated_database, late, lambda _: FakeSpaces(keys[:1]))
    assert missed[1].ok is False


def test_an_unreadable_bucket_fails_only_its_check(calculated_database):
    stock(calculated_database)
    with connect(calculated_database) as conn:
        write_settings(conn, {"archive.enabled": True, "raw.spool_dir": "spool"})

    def spaces(_):
        raise ArchiveError("Spaces settings: OSError")

    checks = verify_cloud.archive(calculated_database, Hour(H, NOW), spaces)
    assert [(c.ok, c.detail) for c in checks] == [(False, "not listed (ArchiveError)")]


def test_the_command_adds_the_cloud_checks(calculated_database, formulas, monkeypatch, capsys):
    stock(calculated_database)
    cloud = FakeCloud([droplet("pi-pulse-fra1-alpha", PULSE_TAG, timedelta(minutes=20))])
    tokens = []
    monkeypatch.setattr(do_api, "DigitalOcean", lambda source: tokens.append(source) or cloud)
    monkeypatch.setattr(cli, "utc_now", lambda: NOW)
    args = ["daily", "verify-hour", "--formulas", str(formulas), "--hour", H.isoformat()]
    args += ["--database-file", str(calculated_database), "--do-token-file", "do-token"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "droplets      ok   1 up now (read only)" in out
    assert "archive       -    off (archive.enabled or raw.spool_dir is off)" in out
    assert len(tokens) == 1 and cloud.closed
