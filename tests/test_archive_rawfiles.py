"""ARCHIVE-1 part 3: the raw files of each update, spooled 30 days and uploaded hourly."""

import json
import tarfile
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from test_archive import empty_database
from test_archive_job import fake_bucket
from test_storage import measurement

from proxy_index.archive import ArchiveError
from proxy_index.archive.export import file_digests
from proxy_index.archive.rawfiles import rebuild, spool, upload_hours
from proxy_index.archive.spaces import Spaces
from proxy_index.cli import main
from proxy_index.orchestrator.archive import pack
from proxy_index.orchestrator.db import Database, put_setting
from proxy_index.orchestrator.jobs import raw_upload
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

HOUR = datetime(2026, 10, 1, 10, tzinfo=UTC)


def raw(tmp_path, data=b"run bytes"):
    path = tmp_path / f"{uuid.uuid4().hex}.src"
    path.write_bytes(data)
    return path


def name(kind="pulse", batch=7):
    return f"{kind}-b{batch}-{uuid.uuid4()}.tar.gz"


def test_ended_hours_are_uploaded_once_and_removed_after_30_days(tmp_path):
    root = tmp_path / "spool"
    first = spool(root, HOUR + timedelta(minutes=3), name(), raw(tmp_path), move=True)
    assert first.parent == root / "2026/10/01/10" and first.stat().st_mode & 0o777 == 0o600
    assert spool(root, HOUR, first.name, raw(tmp_path, b"replay"), move=False) == first
    assert first.read_bytes() == b"run bytes"
    assert spool(tmp_path / "other", HOUR, name("browser"), raw(tmp_path), move=False).exists()
    with pytest.raises(ArchiveError, match="not a raw file name"):
        spool(root, HOUR, "../evil.tar.gz", raw(tmp_path), move=False)
    with fake_bucket(tmp_path) as (bucket, settings):
        spaces = Spaces.from_file(settings)
        # Settling: nothing before 5 minutes after the hour ended.
        assert upload_hours(root, spaces, HOUR + timedelta(minutes=64))["uploaded"] == []
        done = upload_hours(root, spaces, HOUR + timedelta(minutes=66))
        assert done == {"uploaded": ["raw-files/2026/10/01/2026-10-01T10.tar"], "removed_hours": []}
        body, _, acl = bucket.objects["/raw-files/2026/10/01/2026-10-01T10.tar"]
        assert acl == "private"
        (tmp_path / "hour.tar").write_bytes(body)
        with tarfile.open(tmp_path / "hour.tar") as archive:
            manifest = json.loads(archive.extractfile("manifest.json").read())
            assert archive.extractfile(first.name).read() == b"run bytes"
        assert manifest["files"] == [
            {"name": first.name, "bytes": 9, "sha256": file_digests(first)["sha256"]}
        ]
        assert upload_hours(root, spaces, HOUR + timedelta(hours=3))["uploaded"] == []
        # A file filed late into an uploaded hour goes into a further part.
        late = spool(root, HOUR, name("census"), raw(tmp_path, b"late"), move=False)
        again = upload_hours(root, spaces, HOUR + timedelta(hours=3))
        assert again["uploaded"] == ["raw-files/2026/10/01/2026-10-01T10.1.tar"]
        parts = json.loads((first.parent / "uploaded.json").read_text())["parts"]
        assert [p["files"] for p in parts] == [[first.name], [late.name]]
        # An hour whose object in the bucket differs is never removed, even when old.
        other = HOUR + timedelta(hours=1)
        kept = spool(root, other, name(), raw(tmp_path), move=False)
        bucket.objects["/raw-files/2026/10/01/2026-10-01T11.tar"] = (b"x", [], "private")
        expired = HOUR + timedelta(days=31)
        with pytest.raises(
            ArchiveError, match="archive_changed: raw-files/2026/10/01/2026-10-01T11"
        ):
            upload_hours(root, spaces, expired)
        assert kept.exists() and not first.parent.exists()  # hour 10 was all uploaded
        del bucket.objects["/raw-files/2026/10/01/2026-10-01T11.tar"]
        result = upload_hours(root, spaces, expired)
        assert result["removed_hours"] == ["2026-10-01T11"]
        assert not root.exists() or not any(root.iterdir())


async def test_spooled_runs_rebuild_a_database(database, databases, tmp_path, capsys):
    root = tmp_path / "spool"
    daily = await measurement(tmp_path / "daily")
    spool(root, HOUR, f"daily-{daily.name}.tar.gz", daily, move=False)
    census = await measurement(tmp_path / "census")
    run_id = json.loads((census / "summary.json").read_text())["run_id"]
    upload = pack(census, tmp_path / "census.tar.gz")
    spool(root, HOUR + timedelta(hours=1), f"census-b99-{run_id}.tar.gz", upload, move=True)
    target = empty_database(databases)
    report = rebuild(root, target, date(2026, 10, 1), date(2026, 10, 1))
    assert report == {"runs": 2, "imported": 2, "unlinked": [f"census-b99-{run_id}.tar.gz"]}
    with connect(target) as conn:
        assert conn.execute("SELECT count(*) FROM test_runs").fetchone()[0] == 2
    args = ["archive", "rebuild", "--spool", str(root), "--from", "2026-10-01", "--to"]
    assert main([*args, "2026-10-01", "--database-file", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["imported"] == 0  # replays
    assert rebuild(root, target, date(2026, 10, 2), date(2026, 10, 3))["runs"] == 0


async def test_the_raw_upload_job(database, tmp_path):
    db = Database(database)
    try:
        assert await raw_upload(db) == {"skipped": "archive.enabled or raw.spool_dir is off"}
        root = tmp_path / "spool"
        spool(root, HOUR, name(), raw(tmp_path), move=True)
        with fake_bucket(tmp_path) as (bucket, settings):
            async with db.transaction() as conn:
                await put_setting(conn, "archive.enabled", True)
                await put_setting(conn, "archive.spaces_file", str(settings))
                await put_setting(conn, "raw.spool_dir", str(root))
            result = await raw_upload(db)
            assert result["uploaded"] == ["raw-files/2026/10/01/2026-10-01T10.tar"]
            assert result["removed_hours"] == ["2026-10-01T10"]  # over 30 days old today
            assert list(bucket.objects) == ["/raw-files/2026/10/01/2026-10-01T10.tar"]
    finally:
        await db.close()
