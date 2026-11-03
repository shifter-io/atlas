"""`proxy-index dbip update` against a loopback CDN and tiny real MMDBs (DBIP-DAILY-1).

No test reaches DB-IP or a real CDN: every file comes from dbip_fakes.FakeCdn."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import date, timedelta
from pathlib import Path

import maxminddb
import pytest
from dbip_fakes import DE, NO_COUNTRY, US, FakeCdn, tiny_mmdb

from proxy_index import dbip
from proxy_index.cli import main
from proxy_index.dbip import Checks, update

pytest_plugins = ["test_storage"]

SMALL = Checks(min_bytes=1)
DAY = date(2026, 11, 2)


def name(day: date) -> str:
    return f"dbip-full-{day.isoformat()}.mmdb"


@pytest.fixture
def cdn():
    fake = FakeCdn()
    yield fake
    fake.close()


@pytest.fixture
def cli(tmp_path, cdn, capsys, monkeypatch):
    """Runs `dbip update` (today is DAY) and returns (exit code, health record)."""
    monkeypatch.delenv("DBIP_BASE_URL", raising=False)
    monkeypatch.delenv("PROXY_INDEX_DATABASE_FILE", raising=False)
    real = dbip.update
    monkeypatch.setattr(dbip, "update", lambda *a, **k: real(*a, **k, today=DAY))

    def run(*extra: str, base: str | None = cdn.url) -> tuple[int, dict]:
        argv = ["dbip", "update", "--dest", str(tmp_path / "db-ip"), "--min-bytes", "1"]
        code = main(argv + list(extra) + (["--base-url", base] if base else []))
        out, err = capsys.readouterr()
        assert out.count("\n") == 1 and not err
        return code, json.loads(out)

    return run


def installed(dest: Path) -> list[str]:
    return sorted(p.name for p in dest.glob("*.mmdb"))


def test_the_newest_dated_release_is_installed_once(tmp_path, cdn, cli):
    cdn.files[name(DAY - timedelta(days=40))] = tiny_mmdb(marker="o")
    cdn.files[name(DAY - timedelta(days=9))] = tiny_mmdb(marker="n")
    code, health = cli()
    dest = tmp_path / "db-ip"
    assert code == 0 and health["dbip"] == "updated" and health["file"] == "dbip-full.mmdb"
    assert installed(dest) == ["dbip-full.mmdb"]
    assert health["source"]["name"] == name(DAY - timedelta(days=9))
    assert health["source"]["date"] == "2026-10-24" and health["recorded"] is None
    # Tomorrow back to the newest: 11 probes, one download.
    assert sum(n for (method, _), n in cdn.hits.items() if method == "HEAD") == 11
    assert cdn.downloads(health["source"]["name"]) == 1
    assert (dest / "dbip-full.mmdb").stat().st_mode & 0o777 == 0o644
    with maxminddb.open_database(str(dest / "dbip-full.mmdb")) as reader:
        assert reader.get("198.51.100.7") == DE
    state = json.loads((dest / "release.json").read_text())
    assert state["sha256"] == health["sha256"] and state["size"] > 0
    assert health["database_type"].startswith("DBIP")
    assert sorted(p.name for p in dest.iterdir()) == [
        ".update.lock",
        "dbip-full.mmdb",
        "release.json",
    ]

    cdn.hits.clear()
    code, again = cli()
    assert code == 0 and again["dbip"] == "unchanged" and again["sha256"] == health["sha256"]
    assert cdn.downloads(health["source"]["name"]) == 0 and len(cdn.hits) == 11


def test_each_new_release_replaces_the_file_under_the_same_name(tmp_path, cdn, cli):
    dest = tmp_path / "db-ip"
    shas = []
    for n, day in enumerate(DAY - timedelta(days=d) for d in (90, 30, 0)):
        cdn.files[name(day)] = tiny_mmdb(marker="abc"[n])
        code, health = cli("--lookback-days", "100")
        assert code == 0 and health["dbip"] == "updated" and health["source"]["name"] == name(day)
        shas.append(health["sha256"])
    assert len(set(shas)) == 3 and installed(dest) == ["dbip-full.mmdb"]
    assert hashlib.sha256((dest / "dbip-full.mmdb").read_bytes()).hexdigest() == shas[-1]
    # A file replaced on the CDN under the same name (new bytes, new ETag) replaces it too.
    cdn.files[name(DAY)] = tiny_mmdb(marker="d")
    code, health = cli()
    assert code == 0 and health["dbip"] == "updated" and health["sha256"] not in shas


def test_a_reader_of_the_old_file_keeps_it_until_it_reopens(tmp_path, cdn, cli):
    cdn.files[name(DAY - timedelta(days=1))] = tiny_mmdb(high=DE, marker="a")
    cli()
    current = tmp_path / "db-ip" / "dbip-full.mmdb"
    with maxminddb.open_database(str(current)) as reader:
        cdn.files[name(DAY)] = tiny_mmdb(high=US, marker="b")
        assert cli()[1]["dbip"] == "updated"
        assert reader.get("198.51.100.7") == DE  # the replaced file, still open
    with maxminddb.open_database(str(current)) as reader:
        assert reader.get("198.51.100.7") == US


def test_new_headers_over_the_same_bytes_change_nothing(tmp_path, cdn, cli):
    cdn.files[name(DAY)] = tiny_mmdb()
    first = cli()[1]
    state = tmp_path / "db-ip" / "release.json"
    installed_state = json.loads(state.read_text())
    installed_state["source"]["etag"] = '"older"'
    state.write_text(json.dumps(installed_state))
    inode = (tmp_path / "db-ip" / "dbip-full.mmdb").stat().st_ino
    code, health = cli()
    assert code == 0 and health["dbip"] == "unchanged" and health["sha256"] == first["sha256"]
    after = json.loads(state.read_text())
    assert after["source"]["etag"] != '"older"'
    assert after["updated_at"] == installed_state["updated_at"]
    assert (tmp_path / "db-ip" / "dbip-full.mmdb").stat().st_ino == inode  # not replaced
    assert installed(tmp_path / "db-ip") == ["dbip-full.mmdb"]


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (b"not a database" * 10, "verify: not a readable MMDB"),
        (tiny_mmdb(database_type="GeoIP2-City"), "verify: not a DB-IP database"),
        (tiny_mmdb(low=NO_COUNTRY), "verify: 96 of 221 test lookups found a country"),
    ],
)
def test_a_release_that_fails_verification_is_never_installed(tmp_path, cdn, cli, data, error):
    cdn.files[name(DAY)] = data
    code, health = cli()
    assert code == 3 and health == {"dbip": "failed", "error": health["error"]}
    assert health["error"].startswith(error)
    assert [p.name for p in (tmp_path / "db-ip").iterdir()] == [".update.lock"]


def test_a_cut_short_download_fails(tmp_path, cdn, cli):
    cdn.files[name(DAY)] = tiny_mmdb()
    cdn.cut[name(DAY)] = 50
    code, health = cli()
    assert code == 3 and health["error"].startswith(f"download {name(DAY)}")
    assert [p.name for p in (tmp_path / "db-ip").iterdir()] == [".update.lock"]


def test_size_checks_refuse_a_small_or_shrunken_release(tmp_path, cdn):
    dest = tmp_path / "db-ip"
    cdn.files[name(DAY - timedelta(days=1))] = tiny_mmdb()
    with pytest.raises(dbip.UpdateError, match="under the 100000 minimum"):
        update(dest, cdn.url, checks=Checks(min_bytes=100_000), today=DAY)
    update(dest, cdn.url, checks=SMALL, today=DAY)
    cdn.files[name(DAY)] = tiny_mmdb(marker="b")
    with pytest.raises(dbip.UpdateError, match="under half the installed release"):
        update(dest, cdn.url, checks=Checks(min_bytes=1, shrink_limit=2), today=DAY)
    state = json.loads((dest / "release.json").read_text())
    assert state["source"]["name"] == name(DAY - timedelta(days=1))  # still the first one
    assert installed(dest) == ["dbip-full.mmdb"]


def test_no_release_in_the_window_fails_and_errors_name_only_the_file(tmp_path, cdn, cli):
    code, health = cli("--lookback-days", "3")
    assert code == 3 and health["error"].startswith("probe: no dbip-full-YYYY-MM-DD.mmdb")
    assert health["error"].endswith("in the last 3 days")
    assert len(cdn.hits) == 5  # tomorrow, today and three days back
    cdn.status[name(DAY)] = 503
    code, health = cli()
    assert code == 3 and health["error"] == f"probe {name(DAY)}: HTTP 503"
    cdn.files[name(DAY)] = tiny_mmdb()
    del cdn.status[name(DAY)]
    cdn.close()
    code, health = cli()
    assert code == 3 and health["error"] == f"probe {name(DAY + timedelta(days=1))}: URLError"
    assert cdn.url not in json.dumps(health)


def test_a_redirect_to_plain_http_is_refused(tmp_path, cdn, cli):
    cdn.files[name(DAY)] = tiny_mmdb()
    cdn.redirects[name(DAY)] = "http://cdn.invalid/elsewhere.mmdb"
    code, health = cli()
    assert (
        code == 3 and health["error"] == f"probe {name(DAY)}: redirected to a URL that is not https"
    )
    cdn.redirects[name(DAY)] = cdn.url + "moved.mmdb"  # another secure URL is followed
    cdn.files["moved.mmdb"] = cdn.files[name(DAY)]
    code, health = cli()
    assert code == 0 and health["dbip"] == "updated" and cdn.downloads("moved.mmdb") == 1


def test_a_failed_get_after_the_probe_names_the_status(tmp_path, cdn, cli):
    cdn.files[name(DAY)] = tiny_mmdb()

    class Flaky(dict):
        def get(self, key, default=None):  # the GET answers 500, the HEAD 200
            return 500 if key == name(DAY) and cdn.hits["GET", key] else default

    cdn.status = Flaky()
    code, health = cli()
    assert code == 3 and health["error"] == f"download {name(DAY)}: HTTP 500"


def test_the_base_url_from_the_environment_and_configuration_errors(cdn, cli, monkeypatch):
    code, health = cli(base=None)
    assert code == 2 and health["error"].startswith("no DB-IP base URL")
    for wrong in ("http://example.com/dbip/", "https://example.com/dbip/?key=1", "ftp://x/"):
        code, health = cli(base=wrong)
        assert code == 2 and health["error"] == "the DB-IP base URL must be https, without a query"
    cdn.files[name(DAY)] = tiny_mmdb()
    monkeypatch.setenv("DBIP_BASE_URL", cdn.url.rstrip("/"))  # the trailing slash is added
    code, health = cli(base=None)
    assert code == 0 and health["dbip"] == "updated"


def test_a_second_update_of_the_directory_is_refused(tmp_path, cdn, cli):
    dest = tmp_path / "db-ip"
    dest.mkdir()
    fd = os.open(dest / ".update.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        code, health = cli()
    finally:
        os.close(fd)
    assert code == 4 and health["error"] == "another update is running in this directory"
    assert not cdn.hits


def test_the_release_is_recorded_and_a_recording_failure_is_reported(cdn, cli, monkeypatch):
    executed = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params):
            executed.append((query, params))

    monkeypatch.setattr("proxy_index.storage.connect", lambda path: Connection())
    cdn.files[name(DAY)] = tiny_mmdb()
    code, health = cli("--database-file", "db.toml")
    assert code == 0 and health["recorded"] is True
    ((query, (sha256, metadata)),) = executed
    assert "dbip_releases" in query and sha256 == health["sha256"]
    assert set(metadata.obj) == {"file", "sha256", "database_type", "build_epoch", "built_at"}

    def refuse(path):
        raise OSError(f"cannot reach the host of {path}")

    monkeypatch.setattr("proxy_index.storage.connect", refuse)
    code, health = cli("--database-file", "db.toml")
    assert code == 3 and health["dbip"] == "unchanged" and health["recorded"] is False
    assert health["error"] == "record: OSError"


def test_a_file_error_is_a_failed_health_line(tmp_path, cli):
    (tmp_path / "db-ip").write_text("a file, not a directory")
    code, health = cli()
    assert code == 3 and health["error"] == "files: FileExistsError"


def test_the_release_is_recorded_in_postgres(tmp_path, cdn, database):
    from proxy_index.storage import connect

    cdn.files[name(DAY)] = tiny_mmdb()
    dest = tmp_path / "db-ip"
    health = update(dest, cdn.url, database_file=database, checks=SMALL, today=DAY)
    update(dest, cdn.url, database_file=database, checks=SMALL, today=DAY)  # recorded once
    with connect(database) as conn:
        rows = conn.execute("SELECT sha256,metadata FROM dbip_releases").fetchall()
    assert len(rows) == 1 and rows[0][0] == health["sha256"]
    assert rows[0][1] == {k: health[k] for k in rows[0][1]} and rows[0][1]["file"] == name(DAY)
