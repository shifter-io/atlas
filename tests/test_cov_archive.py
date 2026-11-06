"""COV-6: the archive paths the ARCHIVE-1 tests leave out (job loop, purge, restore, Spaces)."""

import subprocess
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from archive_job_cases import census_day, fake_bucket
from test_archive import DAY, SLOT, day_of_data, empty_database
from test_archive_job import LATER

from proxy_index.archive import ArchiveError, job
from proxy_index.archive.export import export_day
from proxy_index.archive.purge import purge_day
from proxy_index.archive.restore import restore_file
from proxy_index.archive.spaces import Spaces, manager_settings
from proxy_index.partitions import children, partition
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

NOW = datetime(2026, 11, 5, tzinfo=UTC)


def fake_days(monkeypatch, days):
    """job.run_due over a scripted sequence of oldest days; every day is due and archived."""
    found = iter(days)
    monkeypatch.setattr(job, "oldest_day", lambda database: next(found))
    monkeypatch.setattr(job, "waiting", lambda database, day, now: None)
    archived = []

    def archive_day(database, spaces, day, workdir, now):
        archived.append(day)
        return {"day": day.isoformat(), "deleted": {"test_runs": 1, "test_batches": 2}}

    monkeypatch.setattr(job, "archive_day", archive_day)
    return archived


def test_a_run_stops_after_max_days(monkeypatch, tmp_path):
    first = NOW.date() - timedelta(days=35)
    days = [first, first + timedelta(days=1), first + timedelta(days=2)]
    archived = fake_days(monkeypatch, days)
    summary = job.run_due(None, None, tmp_path / "work", NOW, max_days=2)
    assert archived == days[:2]
    assert summary == {
        "archived": [d.isoformat() for d in days[:2]],
        "waiting": None,
        "deleted_rows": 6,
    }
    assert (tmp_path / "work").stat().st_mode & 0o777 == 0o700


def test_a_day_still_holding_rows_after_its_archive_fails_the_run(monkeypatch, tmp_path):
    day = NOW.date() - timedelta(days=35)
    archived = fake_days(monkeypatch, [day, day])
    with pytest.raises(ArchiveError, match=f"{day} still holds rows after it was archived"):
        job.run_due(None, None, tmp_path / "work", NOW)
    assert archived == [day]


def test_a_database_without_raw_rows_archives_nothing(database, tmp_path):
    summary = job.run_due(database, None, tmp_path / "work", NOW)
    assert summary == {"archived": [], "waiting": "no raw rows", "deleted_rows": 0}


async def test_a_partition_with_another_runs_rows_stays_and_empty_ones_go(database, tmp_path):
    key = await census_day(database, tmp_path)
    with connect(database) as conn:
        # Today's daily-test run with one bucket inside DAY's partition: not the day's row.
        other = conn.execute("SELECT run_key FROM test_runs WHERE batch_id IS NULL").fetchone()[0]
        conn.execute(
            "INSERT INTO run_buckets(bucket_start,run_key,completed,successes,response_body_bytes,"
            "errors,status_codes,histogram_steps,histogram_counts) "
            "VALUES (%s,%s,0,0,0,'{}','{}','{}','{}')",
            (SLOT, other),
        )
        empty_day = datetime(2026, 9, 28, tzinfo=UTC)
        partition(conn, "run_buckets", empty_day)
        found = children(conn, "run_buckets")
        empty = next(name for name, first, _ in found if first == empty_day.date())
    _, manifest = export_day(database, DAY, tmp_path / "out")
    purged = purge_day(database, DAY, manifest, LATER)
    assert empty in purged["dropped_partitions"]
    assert purged["deleted"]["run_buckets"] == next(
        t["rows"] for t in manifest["tables"] if t["name"] == "run_buckets"
    )
    with connect(database) as conn:
        names = [name for name, _, _ in children(conn, "run_buckets")]
        assert "run_buckets_2026_10_01" in names and empty not in names
        left = conn.execute("SELECT run_key FROM run_buckets_2026_10_01").fetchall()
        assert left == [(other,)]
        assert not conn.execute("SELECT 1 FROM test_runs WHERE run_key=%s", (key,)).fetchone()


async def test_a_table_the_target_lacks_is_skipped_when_the_file_has_no_rows_for_it(
    database, databases, tmp_path
):
    await day_of_data(database, tmp_path)
    # The day before DAY holds only the batch DAY's batch retries: no run, no calculation role.
    path, manifest = export_day(database, DAY - timedelta(days=1), tmp_path / "out")
    rows = {t["name"]: t["rows"] for t in manifest["tables"]}
    assert rows["test_batches"] == 1 and rows["calculation_roles"] == 0
    restored = restore_file(path, empty_database(databases, calculated=False))
    assert restored["inserted"]["test_batches"] == 1
    assert "calculation_roles" not in restored["inserted"]


def test_a_database_without_the_calculated_schema_exports_without_it(database, databases, tmp_path):
    path, manifest = export_day(database, DAY, tmp_path / "out")
    assert manifest["calculated_schema"] is None and manifest["storage_schema"]
    names = [t["name"] for t in manifest["tables"]]
    assert "test_runs" in names and "calculation_roles" not in names
    restored = restore_file(path, empty_database(databases, calculated=False))
    assert restored["day"] == DAY.isoformat()
    assert restored["inserted"]["test_batches"] == restored["inserted"]["test_runs"] == 0


def test_restore_refuses_an_oversized_manifest(database, tmp_path, monkeypatch):
    path, _ = export_day(database, DAY, tmp_path / "out")
    monkeypatch.setattr("proxy_index.archive.restore.MAX_MANIFEST", 10)
    with pytest.raises(ArchiveError, match="manifest.json is too large"):
        restore_file(path, database)


class NoEtag(BaseHTTPRequestHandler):
    """An object store that answers HEAD with a size but without an ETag."""

    def do_HEAD(self):  # noqa: N802 - the http.server name
        self.send_response(200)
        self.send_header("Content-Length", "5")
        self.end_headers()

    def log_message(self, *args):
        pass


def test_a_head_without_an_etag_is_refused():
    server = ThreadingHTTPServer(("127.0.0.1", 0), NoEtag)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = Spaces(
            {
                "endpoint": f"http://127.0.0.1:{server.server_port}",
                "bucket": "archive",
                "region": "fra1",
                "access_key_id": "DO00EXAMPLE",
                "secret_access_key": "example-secret",
            }
        )
        with pytest.raises(ArchiveError, match="HEAD raw/x answered without a size or ETag"):
            client.head("raw/x")
    finally:
        server.shutdown()
        server.server_close()


def test_manager_settings_keep_a_secret_from_the_file(tmp_path, monkeypatch):
    def no_keychain(*args, **kwargs):
        raise AssertionError("the keychain must not be read")

    monkeypatch.setattr(subprocess, "run", no_keychain)
    with fake_bucket(tmp_path) as (_, settings):
        data = manager_settings(settings)
    assert data.startswith(b"[spaces]\n")
    assert b'secret_access_key = "example-secret"' in data
    assert b'access_key_id = "DO00EXAMPLE"' in data
