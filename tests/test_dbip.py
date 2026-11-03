"""`proxy-index dbip update` against a loopback DB-IP account and tiny real MMDBs (DBIP-DAILY-1).

No test reaches DB-IP: every index and download comes from dbip_fakes.FakeAccount, and every
output is checked not to contain the account key its URLs carry."""

from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import maxminddb
import pytest
from dbip_fakes import DE, KEY, NO_COUNTRY, FakeAccount, tiny_mmdb, url_file

from proxy_index import dbip
from proxy_index.cli import main
from proxy_index.dbip import Checks, update

pytest_plugins = ["test_storage"]

SMALL = Checks(min_bytes=1)
TODAY = datetime.now(UTC).date().isoformat()


@pytest.fixture
def account():
    fake = FakeAccount()
    yield fake
    fake.close()


@pytest.fixture
def cli(tmp_path, account, capsys, monkeypatch):
    """Runs `dbip update` and returns (exit code, health record); asserts the key never shows."""
    monkeypatch.delenv("DBIP_ACCOUNT_URL", raising=False)
    monkeypatch.delenv("PROXY_INDEX_DATABASE_FILE", raising=False)
    secret = url_file(tmp_path, account.url)
    dest = tmp_path / "db-ip"

    def run(*extra: str, url: Path | None = secret) -> tuple[int, dict]:
        argv = ["dbip", "update", "--dest", str(dest), "--min-bytes", "1", *extra]
        code = main(argv + (["--url-file", str(url)] if url else []))
        out, err = capsys.readouterr()
        assert KEY not in out + err and out.count("\n") == 1
        if dest.exists():
            assert all(KEY not in p.read_text() for p in dest.glob("*.json"))
        return code, json.loads(out)

    return run


def releases(dest: Path) -> list[str]:
    return sorted(p.name for p in dest.glob("dbip-full-*.mmdb"))


def test_update_installs_verifies_and_is_idempotent(tmp_path, account, cli):
    account.publish(tiny_mmdb(), "2026-11")
    code, health = cli()
    dest = tmp_path / "db-ip"
    assert code == 0 and health["dbip"] == "updated" and health["removed"] == []
    assert health["file"] == f"dbip-full-{TODAY}.mmdb" and releases(dest) == [health["file"]]
    assert os.readlink(dest / "current") == health["file"]  # relative: the directory can move
    assert (dest / "current").stat().st_mode & 0o777 == 0o644
    with maxminddb.open_database(str(dest / "current")) as reader:
        assert reader.get("198.51.100.7") == DE
    state = json.loads((dest / "release.json").read_text())
    assert state["sha256"] == health["sha256"] and state["source"]["version"] == "2026-11"
    assert health["database_type"].startswith("DBIP") and health["recorded"] is None
    assert not [p for p in dest.iterdir() if "partial" in p.name or "download" in p.name]

    code, again = cli()
    assert code == 0 and again["dbip"] == "unchanged" and again["sha256"] == health["sha256"]
    assert account.hits[account.download_path("2026-11")] == 1  # nothing downloaded again
    assert releases(dest) == [health["file"]]


def test_new_releases_swing_current_and_keep_the_previous_two(tmp_path, account, cli):
    dest = tmp_path / "db-ip"
    shas = []
    for n, marker in enumerate("abcd"):
        account.publish(tiny_mmdb(marker=marker), f"2026-1{n}", packed=n % 2 == 0)
        code, health = cli()
        assert code == 0 and health["dbip"] == "updated"
        shas.append(health["sha256"])
    assert len(set(shas)) == 4 and len(releases(dest)) == 3
    assert health["removed"] == [f"dbip-full-{TODAY}.mmdb"]  # the first, oldest release
    assert (dest / "current").resolve().name == health["file"]
    assert health["file"] == f"dbip-full-{TODAY}-{shas[-1][:8]}.mmdb"  # same day, new bytes


def test_new_index_fields_over_the_same_bytes_change_nothing(tmp_path, account, cli):
    data = tiny_mmdb()
    account.publish(data, "2026-11")
    first = cli()[1]
    state = tmp_path / "db-ip" / "release.json"
    installed = json.loads(state.read_text())["updated_at"]
    account.publish(data, "2026-11b", packed=False)
    code, health = cli()
    assert code == 0 and health["dbip"] == "unchanged" and health["file"] == first["file"]
    assert releases(tmp_path / "db-ip") == [first["file"]]
    after = json.loads(state.read_text())
    assert after["source"]["version"] == "2026-11b" and after["updated_at"] == installed


@pytest.mark.parametrize(
    ("data", "fields", "error"),
    [
        (tiny_mmdb(), {"md5sum": "0" * 32}, "verify: md5sum differs"),
        (tiny_mmdb(), {"size": 7}, "verify: size differs"),
        (b"not a database" * 10, {}, "verify: not a readable MMDB"),
        (tiny_mmdb(database_type="GeoIP2-City"), {}, "verify: not a DB-IP database"),
        (tiny_mmdb(low=NO_COUNTRY), {}, "verify: 96 of 221 test lookups found a country"),
    ],
)
def test_a_release_that_fails_verification_is_never_installed(
    tmp_path, account, cli, data, fields, error
):
    account.publish(data, "2026-11", **fields)
    code, health = cli()
    assert code == 3 and health == {"dbip": "failed", "error": health["error"]}
    assert health["error"].startswith(error)
    assert list((tmp_path / "db-ip").iterdir()) == [tmp_path / "db-ip" / ".update.lock"]


def test_size_checks_refuse_a_small_or_shrunken_release(tmp_path, account):
    dest = tmp_path / "db-ip"
    account.publish(tiny_mmdb(), "2026-11")
    with pytest.raises(dbip.UpdateError, match="under the 100 minimum"):
        update(dest, account.url, checks=Checks(min_bytes=100))
    update(dest, account.url, checks=SMALL)
    account.publish(tiny_mmdb(marker="b"), "2026-12")
    with pytest.raises(dbip.UpdateError, match="under half the installed release"):
        update(dest, account.url, checks=Checks(min_bytes=1, shrink_limit=2))
    assert len(releases(dest)) == 1


def test_a_broken_gzip_download_fails_cleanly(tmp_path, account, cli):
    # No size or checksum in the index: the truncated gzip itself must fail.
    account.publish(tiny_mmdb(), "2026-11", size=None, md5sum=None, sha1sum=None)
    path = account.download_path("2026-11")
    account.routes[path] = (200, account.routes[path][1][:20])
    code, health = cli()
    assert code == 3 and health["error"].startswith("download: not a valid gzip file")


@pytest.mark.parametrize(
    ("index", "error"),
    [
        (b"<html>", "index: not JSON"),
        (json.dumps({"csv": {}}).encode(), "index: 0 MMDB downloads instead of one"),
        (
            json.dumps({"a": {"mmdb": {"url": "https://x"}}, "b": {"mmdb": {"url": "https://y"}}}),
            "index: 2 MMDB downloads instead of one",
        ),
        (json.dumps({"mmdb": {"url": "http://example.com/x"}}), "index: the download URL"),
    ],
)
def test_an_unusable_index_fails_without_a_download(account, cli, index, error):
    account.routes[account.index_path] = (200, index if isinstance(index, bytes) else index.encode())
    code, health = cli()
    assert code == 3 and health["error"].startswith(error)


def test_http_and_connection_errors_name_only_the_step(tmp_path, account, cli):
    code, health = cli()
    assert code == 3 and health["error"] == "index: HTTP 404"
    account.publish(tiny_mmdb(), "2026-11")
    account.routes[account.download_path("2026-11")] = (500, b"")
    assert cli()[1]["error"] == "download: HTTP 500"
    account.close()
    assert cli()[1]["error"] == "index: URLError"


def test_the_nested_index_and_the_url_from_the_environment(tmp_path, account, cli, monkeypatch):
    account.publish(tiny_mmdb(), "2026-11", nest=True)
    monkeypatch.setenv("DBIP_ACCOUNT_URL", account.url)
    code, health = cli(url=None)
    assert code == 0 and health["dbip"] == "updated"


def test_configuration_errors_exit_2(tmp_path, account, cli, monkeypatch):
    code, health = cli(url=None)
    assert code == 2 and health["error"].startswith("no DB-IP account URL")
    loose = url_file(tmp_path, account.url, mode=0o640)
    code, health = cli(url=loose)
    assert code == 2 and health["error"].startswith("account URL file: needs one URL")
    monkeypatch.setenv("DBIP_ACCOUNT_URL", f"http://example.com/account/{KEY}/")
    code, health = cli(url=None)
    assert code == 2 and health["error"] == "account URL must be https"


def test_a_second_update_of_the_directory_is_refused(tmp_path, account, cli):
    account.publish(tiny_mmdb(), "2026-11")
    dest = tmp_path / "db-ip"
    dest.mkdir()
    fd = os.open(dest / ".update.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        code, health = cli()
    finally:
        os.close(fd)
    assert code == 4 and health["error"] == "another update is running in this directory"
    assert account.hits[account.index_path] == 0


def test_the_release_is_recorded_and_a_recording_failure_is_reported(
    tmp_path, account, cli, monkeypatch
):
    executed = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params):
            executed.append((query, params))

    monkeypatch.setattr("proxy_index.storage.connect", lambda path: Connection())
    account.publish(tiny_mmdb(), "2026-11")
    code, health = cli("--database-file", "db.toml")
    assert code == 0 and health["recorded"] is True
    (query, (sha256, metadata)), = executed
    assert "dbip_releases" in query and sha256 == health["sha256"]
    assert set(metadata.obj) == {"file", "sha256", "database_type", "build_epoch", "built_at"}

    def refuse(path):
        raise OSError(f"cannot reach host for {path}")

    monkeypatch.setattr("proxy_index.storage.connect", refuse)
    code, health = cli("--database-file", "db.toml")
    assert code == 3 and health["dbip"] == "unchanged" and health["recorded"] is False
    assert health["error"] == "record: OSError"


def test_a_file_error_is_a_failed_health_line(tmp_path, account, cli):
    (tmp_path / "db-ip").write_text("a file, not a directory")
    code, health = cli()
    assert code == 3 and health["error"] == "files: FileExistsError"


def test_the_release_is_recorded_in_postgres(tmp_path, account, database):
    from proxy_index.storage import connect

    account.publish(tiny_mmdb(), "2026-11")
    health = update(tmp_path / "db-ip", account.url, database_file=database, checks=SMALL)
    update(tmp_path / "db-ip", account.url, database_file=database, checks=SMALL)  # once
    with connect(database) as conn:
        rows = conn.execute("SELECT sha256,metadata FROM dbip_releases").fetchall()
    assert rows == [(health["sha256"], {k: health[k] for k in rows[0][1]})]
    assert rows[0][1]["file"] == health["file"]

