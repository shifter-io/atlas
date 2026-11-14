"""ARCHIVE-RAW-1: each network's raw files of an hour, one zstd tar in Spaces (a fake bucket),
uploaded once, expired locally after 30 days, listed and extracted back."""

import io
import json
import tarfile
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import zstandard
from archive_job_cases import fake_bucket
from browser_ingest_cases import listing_bucket

from atlas.archive import ArchiveError
from atlas.archive.export import file_digests
from atlas.archive.rawfiles import spool
from atlas.archive.rawhours import extract, fetch, object_key, pack, upload_hours
from atlas.archive.spaces import Spaces
from atlas.cli import main
from atlas.orchestrator.db import Database, put_setting
from atlas.orchestrator.jobs import raw_upload

pytest_plugins = ["test_storage"]

HOUR = datetime(2026, 10, 1, 10, tzinfo=UTC)
KEY = "raw-files/2026/10/01/10/decodo.tar.zst"


def raw(tmp_path, data=b"run bytes"):
    path = tmp_path / f"{uuid.uuid4().hex}.src"
    path.write_bytes(data)
    return path


def name(kind="pulse", batch=7):
    return f"{kind}-b{batch}-{uuid.uuid4()}.tar.gz"


def members(body: bytes) -> dict[str, bytes]:
    """The object's tar members, as `zstd -d | tar x` reads them."""
    plain = zstandard.ZstdDecompressor().decompressobj().decompress(body)
    with tarfile.open(fileobj=io.BytesIO(plain)) as archive:
        return {m.name: archive.extractfile(m).read() for m in archive.getmembers()}


def test_ended_network_hours_are_uploaded_once_and_removed_after_30_days(tmp_path):
    root = tmp_path / "spool"
    first = spool(root, HOUR + timedelta(minutes=3), "decodo", name(), raw(tmp_path), move=True)
    other = spool(root, HOUR, "iproyal", name("census"), raw(tmp_path, b"other"), move=False)
    with fake_bucket(tmp_path) as (bucket, settings):
        spaces = Spaces.from_file(settings)
        # Settling: nothing before 5 minutes after the hour ended.
        assert upload_hours(root, spaces, HOUR + timedelta(minutes=64))["uploaded"] == []
        done = upload_hours(root, spaces, HOUR + timedelta(minutes=66))
        iproyal = "raw-files/2026/10/01/10/iproyal.tar.zst"
        assert done == {"uploaded": [KEY, iproyal], "removed_hours": []}
        body, _, acl = bucket.objects[f"/{KEY}"]
        assert acl == "private"
        found = members(body)
        assert list(found) == ["manifest.json", first.name]
        assert found[first.name] == b"run bytes"
        manifest = json.loads(found["manifest.json"])
        assert manifest == {
            "hour": HOUR.isoformat(),
            "network": "decodo",
            "files": [{"name": first.name, "bytes": 9, "sha256": file_digests(first)["sha256"]}],
        }
        assert members(bucket.objects[f"/{iproyal}"][0])[other.name] == b"other"
        assert upload_hours(root, spaces, HOUR + timedelta(hours=3))["uploaded"] == []
        # A file filed late into an uploaded network-hour goes into a further part.
        late = spool(root, HOUR, "decodo", name("census"), raw(tmp_path, b"late"), move=False)
        again = upload_hours(root, spaces, HOUR + timedelta(hours=3))
        assert again["uploaded"] == ["raw-files/2026/10/01/10/decodo.1.tar.zst"]
        parts = json.loads((first.parent / "uploaded.json").read_text())["parts"]
        assert [p["files"] for p in parts] == [[first.name], [late.name]]
        # An hour whose object in the bucket differs is never removed, even when old.
        kept = spool(root, HOUR + timedelta(hours=1), "decodo", name(), raw(tmp_path), move=False)
        changed = "/raw-files/2026/10/01/11/decodo.tar.zst"
        bucket.objects[changed] = (b"x", [], "private")
        expired = HOUR + timedelta(days=31)
        with pytest.raises(ArchiveError, match="archive_changed: raw-files/2026/10/01/11/decodo"):
            upload_hours(root, spaces, expired)
        assert kept.exists() and not (root / "2026/10/01/10").exists()  # 10 was all uploaded
        del bucket.objects[changed]
        # A file filed directly in an hour (the layout before ARCHIVE-RAW-1) keeps its hour.
        loose = root / "2026/10/01/12" / name()
        loose.parent.mkdir(parents=True)
        loose.write_bytes(b"loose")
        result = upload_hours(root, spaces, expired)
        assert result == {
            "uploaded": ["raw-files/2026/10/01/11/decodo.tar.zst"],
            "removed_hours": ["2026-10-01T11"],
        }
        assert loose.exists() and [p.name for p in (root / "2026/10/01").iterdir()] == ["12"]


def test_the_object_is_deterministic(tmp_path):
    files = [raw(tmp_path, b"a" * 5000), raw(tmp_path, b"b")]
    one = pack(HOUR, "decodo", files, tmp_path / "one.tar.zst").read_bytes()
    assert pack(HOUR, "decodo", files, tmp_path / "two.tar.zst").read_bytes() == one
    assert object_key(HOUR, "decodo", 2) == "raw-files/2026/10/01/10/decodo.2.tar.zst"


def test_the_raw_command_lists_and_extracts_a_network_hour(tmp_path, capsys):
    root, out = tmp_path / "spool", tmp_path / "restored"
    first = spool(root, HOUR, "decodo", name(), raw(tmp_path), move=False)
    spool(root, HOUR, "iproyal", name(), raw(tmp_path), move=False)
    with listing_bucket(tmp_path) as (bucket, settings):
        spaces = Spaces.from_file(settings)
        upload_hours(root, spaces, HOUR + timedelta(hours=2))
        late = spool(root, HOUR, "decodo", name("census"), raw(tmp_path, b"late"), move=False)
        upload_hours(root, spaces, HOUR + timedelta(hours=2))
        base = ["archive", "raw", "--spaces-file", str(settings), "--hour", "2026-10-01T10"]
        assert main(base) == 0
        assert json.loads(capsys.readouterr().out)["objects"] == [
            "raw-files/2026/10/01/10/decodo.1.tar.zst",
            KEY,
            "raw-files/2026/10/01/10/iproyal.tar.zst",
        ]
        assert main([*base, "--network", "decodo", "--out", str(out)]) == 0
        report = json.loads(capsys.readouterr().out)
        assert sorted(report["files"]) == sorted([first.name, late.name])
        folder = out / "2026/10/01/10/decodo"
        assert (folder / first.name).read_bytes() == b"run bytes"
        assert (folder / late.name).read_bytes() == b"late"
        assert (folder / first.name).stat().st_mode & 0o777 == 0o600
        assert main([*base, "--network", "decodo"]) == 2
        assert "--network needs --out" in capsys.readouterr().err
        assert main([*base, "--network", "massive", "--out", str(out)]) == 2
        assert "no raw files of massive" in capsys.readouterr().err
        # An object that is not the one uploaded is refused before it is read.
        body, meta, acl = bucket.objects[f"/{KEY}"]
        bucket.objects[f"/{KEY}"] = (body + b"x", meta, acl)
        with pytest.raises(ArchiveError, match="does not match its stored sha256"):
            fetch(spaces, HOUR, "decodo", tmp_path / "again")


def handmade(path, manifest_files, files, first="manifest.json"):
    """A network-hour object written by hand: `first` holds the manifest, then `files`."""
    data = json.dumps({"files": manifest_files}).encode()
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        for member, body in [(first, data), *files.items()]:
            info = tarfile.TarInfo(member)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    path.write_bytes(zstandard.ZstdCompressor().compress(out.getvalue()))
    return path


def test_extract_refuses_what_its_manifest_does_not_hold(tmp_path):
    folder, file = tmp_path / "out", name()
    folder.mkdir()
    entry = {"name": file, "bytes": 2, "sha256": file_digests(raw(tmp_path, b"ok"))["sha256"]}
    assert extract(handmade(tmp_path / "ok", [entry], {file: b"ok"}), folder) == [file]
    cases = {
        "does not match the manifest": ([entry], {file: b"no"}),
        "is not a file of the manifest": ([], {file: b"ok"}),
        "missing from the object": ([entry], {}),
    }
    for message, (listed, files) in cases.items():
        with pytest.raises(ArchiveError, match=message):
            extract(handmade(tmp_path / "bad", listed, files), tmp_path / "out")
    with pytest.raises(ArchiveError, match="is not a file of the manifest"):
        extract(handmade(tmp_path / "bad", [entry | {"name": "../x"}], {"../x": b"ok"}), folder)
    with pytest.raises(ArchiveError, match="does not start with its manifest"):
        extract(handmade(tmp_path / "bad", [entry], {file: b"ok"}, first=file), folder)
    assert sorted(p.name for p in folder.iterdir()) == [file]  # no partial file is left


async def test_the_raw_upload_job(database, tmp_path):
    db = Database(database)
    try:
        assert await raw_upload(db) == {"skipped": "archive.enabled or raw.spool_dir is off"}
        root = tmp_path / "spool"
        spool(root, HOUR, "decodo", name(), raw(tmp_path), move=True)
        with fake_bucket(tmp_path) as (bucket, settings):
            async with db.transaction() as conn:
                await put_setting(conn, "archive.enabled", True)
                await put_setting(conn, "archive.spaces_file", str(settings))
                await put_setting(conn, "raw.spool_dir", str(root))
            result = await raw_upload(db)
            assert result == {"uploaded": [KEY], "removed_hours": ["2026-10-01T10"]}  # >30 days
            assert list(bucket.objects) == [f"/{KEY}"]
    finally:
        await db.close()
