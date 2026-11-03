"""`proxy-index dbip update` against a loopback CDN and tiny real MMDBs (DBIP-DAILY-1).

No test reaches DB-IP or a real CDN: every file comes from dbip_fakes.FakeCdn."""

from __future__ import annotations

import fcntl
import json
import os
from datetime import date, timedelta
from pathlib import Path

import maxminddb
import pytest
from dbip_fakes import DE, NO_COUNTRY, FakeCdn, tiny_mmdb

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


def releases(dest: Path) -> list[str]:
    return sorted(p.name for p in dest.glob("dbip-full-*.mmdb"))


def test_the_newest_dated_release_is_installed_once(tmp_path, cdn, cli):
    cdn.files[name(DAY - timedelta(days=40))] = tiny_mmdb(marker="o")
    cdn.files[name(DAY - timedelta(days=9))] = tiny_mmdb(marker="n")
    code, health = cli()
    dest = tmp_path / "db-ip"
    assert code == 0 and health["dbip"] == "updated" and health["removed"] == []
    assert health["file"] == name(DAY - timedelta(days=9)) and releases(dest) == [health["file"]]
    assert health["source"]["date"] == "2026-10-24" and health["recorded"] is None
    # Tomorrow back to the newest: 11 probes, one download.
    assert sum(n for (method, _), n in cdn.hits.items() if method == "HEAD") == 11
    assert cdn.downloads(health["file"]) == 1
    assert os.readlink(dest / "current") == health["file"]  # relative: the directory can move
    assert (dest / "current").stat().st_mode & 0o777 == 0o644
    with maxminddb.open_database(str(dest / "current")) as reader:
        assert reader.get("198.51.100.7") == DE
    state = json.loads((dest / "release.json").read_text())
    assert state["sha256"] == health["sha256"] and state["size"] > 0
    assert health["database_type"].startswith("DBIP")
    assert sorted(p.name for p in dest.iterdir()) == [
        ".update.lock",
        "current",
        health["file"],
        "release.json",
    ]

    cdn.hits.clear()
    code, again = cli()
    assert code == 0 and again["dbip"] == "unchanged" and again["sha256"] == health["sha256"]
    assert cdn.downloads(health["file"]) == 0 and len(cdn.hits) == 11  # back to the installed


def test_new_releases_swing_current_and_keep_the_previous_two(tmp_path, cdn, cli):
    dest = tmp_path / "db-ip"
    days = [DAY - timedelta(days=d) for d in (90, 60, 30, 0)]
    for n, day in enumerate(days):
        cdn.files[name(day)] = tiny_mmdb(marker="abcd"[n])
        code, health = cli("--lookback-days", "100")
        assert code == 0 and health["dbip"] == "updated" and health["file"] == name(day)
    assert releases(dest) == [name(day) for day in days[1:]]
    assert health["removed"] == [name(days[0])]
    assert (dest / "current").resolve().name == name(DAY)


def test_a_file_replaced_under_the_same_name_is_installed_beside_the_old_one(tmp_path, cdn, cli):
    cdn.files[name(DAY)] = tiny_mmdb(marker="a")
    first = cli()[1]
    cdn.files[name(DAY)] = tiny_mmdb(marker="b")  # new bytes, so a new ETag
    code, health = cli()
    assert code == 0 and health["dbip"] == "updated" and health["sha256"] != first["sha256"]
    assert health["file"] == f"dbip-full-{DAY}-{health['sha256'][:8]}.mmdb"
    assert releases(tmp_path / "db-ip") == sorted([first["file"], health["file"]])


def test_new_headers_over_the_same_bytes_change_nothing(tmp_path, cdn, cli):
    cdn.files[name(DAY)] = tiny_mmdb()
    first = cli()[1]
    state = tmp_path / "db-ip" / "release.json"
    installed = json.loads(state.read_text())
    installed["source"]["etag"] = '"older"'
    state.write_text(json.dumps(installed))
    code, health = cli()
    assert code == 0 and health["dbip"] == "unchanged" and health["file"] == first["file"]
    after = json.loads(state.read_text())
    assert after["source"]["etag"] != '"older"' and after["updated_at"] == installed["updated_at"]
    assert releases(tmp_path / "db-ip") == [first["file"]]


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
    assert releases(dest) == [name(DAY - timedelta(days=1))]


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
