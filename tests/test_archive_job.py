"""ARCHIVE-1 part 2: the Spaces client, deleting archived days and the daily archive job."""

import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from archive_job_cases import KEY_ID, census_day, fake_bucket, spaces_settings
from test_archive import DAY, SLOT, day_of_data, empty_database, fact_rows

from proxy_index.archive import ArchiveError
from proxy_index.archive import spaces as spaces_module
from proxy_index.archive.export import export_day
from proxy_index.archive.job import facts_digest, object_key, run_due, store
from proxy_index.archive.purge import purge_day
from proxy_index.archive.spaces import Spaces, manager_settings, sign
from proxy_index.cli import main
from proxy_index.orchestrator.db import Database, put_setting
from proxy_index.orchestrator.jobs import archive, retention
from proxy_index.partitions import children
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

# When DAY (and the day before) are due: their rows end on DAY, 30 days earlier. The daily-test
# run of the fixture starts at the real time, later, so its day is still waiting then.
LATER = datetime(2026, 11, 5, tzinfo=UTC)


def test_signature_matches_the_aws_get_object_example():
    request = {
        "method": "GET",
        "host": "examplebucket.s3.amazonaws.com",
        "path": "/test.txt",
        "query": {},
        "headers": {
            "Range": "bytes=0-9",
            "x-amz-content-sha256": spaces_module.EMPTY,
            "x-amz-date": "20130524T000000Z",
        },
        "payload": spaces_module.EMPTY,
    }
    header = sign(
        request,
        "AKIAIOSFODNN7EXAMPLE",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "us-east-1",
        datetime(2013, 5, 24, tzinfo=UTC),
    )
    assert header == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_client_uploads_private_verifies_and_checks_the_bucket(tmp_path):
    with fake_bucket(tmp_path) as (bucket, settings):
        client = Spaces.from_file(settings)
        file = tmp_path / "day.tar.gz"
        file.write_bytes(b"archive bytes")
        digests = {
            "bytes": 13,
            "sha256": hashlib.sha256(b"archive bytes").hexdigest(),
            "md5": hashlib.md5(b"archive bytes").hexdigest(),
        }
        assert client.head("raw/x.tar.gz") is None
        client.put_file("raw/x.tar.gz", file, digests, {"facts": "f" * 64})
        assert bucket.objects["/raw/x.tar.gz"][2] == "private"
        assert client.head("raw/x.tar.gz") == {
            "bytes": 13,
            "md5": digests["md5"],
            "sha256": digests["sha256"],
            "facts": "f" * 64,
        }
        client.get_file("raw/x.tar.gz", tmp_path / "copy")
        assert (tmp_path / "copy").read_bytes() == b"archive bytes"
        with pytest.raises(ArchiveError, match="answered 404"):
            client.get_file("raw/none", tmp_path / "none")
        with pytest.raises(ArchiveError, match="BadDigest"):
            client.put_file("raw/y", file, digests | {"md5": "0" * 32})
        assert client.versioning() is None
        client.enable_versioning()
        assert client.versioning() == "Enabled"
        bucket.state["grants"] = ["http://acs.amazonaws.com/groups/global/AllUsers", "id:owner"]
        assert client.public_grants() == ["http://acs.amazonaws.com/groups/global/AllUsers"]
        wrong = Spaces({**spaces_settings(settings), "secret_access_key": "other"})
        with pytest.raises(ArchiveError, match="HEAD raw/x.tar.gz answered 403"):
            wrong.head("raw/x.tar.gz")
        with pytest.raises(ArchiveError, match="SignatureDoesNotMatch"):
            wrong.versioning()
    with pytest.raises(ArchiveError, match="must be https"):
        Spaces({**spaces_settings(settings), "endpoint": "http://spaces.invalid"})
    with pytest.raises(ArchiveError, match="invalid bucket"):
        Spaces({**spaces_settings(settings), "bucket": "Bad_Bucket"})
    with pytest.raises(ArchiveError, match="failed: ConnectionRefusedError"):
        Spaces(spaces_settings(settings)).head("x")
    settings.chmod(0o644)
    with pytest.raises(ArchiveError, match="mode 600"):
        Spaces.from_file(settings)


def test_the_secret_comes_from_the_keychain_when_the_file_has_none(tmp_path, monkeypatch):
    calls = []

    def security(command, **_):
        calls.append(command)
        found = command[command.index("-a") + 1] == KEY_ID
        return subprocess.CompletedProcess(
            command, 0 if found else 44, "kc-secret\n" if found else ""
        )

    monkeypatch.setattr(subprocess, "run", security)
    settings = tmp_path / "spaces.toml"
    settings.write_text(
        f'[spaces]\nendpoint = "https://fra1.digitaloceanspaces.com"\nbucket = "archive"\n'
        f'access_key_id = "{KEY_ID}"\n'
    )
    settings.chmod(0o600)
    client = Spaces.from_file(settings)
    assert (client.secret, client.region, client.host) == (
        "kc-secret",
        "fra1",
        "fra1.digitaloceanspaces.com",
    )
    assert calls[0][:5] == [
        "security",
        "find-generic-password",
        "-s",
        "Proxy Index Spaces archive",
        "-a",
    ]
    assert b'secret_access_key = "kc-secret"' in manager_settings(settings)
    settings.write_text(settings.read_text().replace(KEY_ID, "UNKNOWN"))
    with pytest.raises(ArchiveError, match="no Spaces secret"):
        manager_settings(settings)
    settings.write_text('[other]\nx = "y"\n')
    with pytest.raises(ArchiveError, match="Spaces settings"):
        manager_settings(settings)


async def test_the_job_archives_due_days_oldest_first_and_deletes_their_rows(
    database, databases, tmp_path, capsys
):
    key = await census_day(database, tmp_path)
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO outages(product_id,country_code,source,batch_id,started_at) "
            "SELECT product_id,'DE','census',batch_id,%s FROM batch_runs",
            (SLOT,),
        )
    source = fact_rows(database, key)
    with fake_bucket(tmp_path) as (bucket, settings):
        summary = run_due(database, Spaces.from_file(settings), tmp_path / "work", LATER)
        assert summary["archived"] == ["2026-09-30", "2026-10-01"]
        assert "less than 30 days ago" in summary["waiting"] and summary["deleted_rows"] > 0
        assert set(bucket.objects) == {"/raw/2026/2026-09-30.tar.gz", "/raw/2026/2026-10-01.tar.gz"}
        assert not list((tmp_path / "work").iterdir())
        with connect(database) as conn:
            assert conn.execute("SELECT count(*) FROM test_runs").fetchone()[0] == 1  # today's
            assert conn.execute("SELECT count(*) FROM test_batches").fetchone()[0] == 0
            assert (
                conn.execute("SELECT count(*) FROM test_events WHERE batch_id IS NULL").fetchone()[
                    0
                ]
                == 2
            )
            assert conn.execute("SELECT batch_id FROM outages").fetchall() == [(None,)]
            names = [n for t in ("run_buckets", "ip_observations") for n, _, _ in children(conn, t)]
            assert not [n for n in names if n.endswith("2026_10_01")]
        # Nothing is due: a second run archives nothing.
        assert (
            run_due(database, Spaces.from_file(settings), tmp_path / "work", LATER)["archived"]
            == []
        )
        target = empty_database(databases)
        args = ["archive", "restore", "--date", "2026-10-01", "--database-file", str(target)]
        assert main([*args, "--spaces-file", str(settings)]) == 0
        assert json.loads(capsys.readouterr().out)["inserted"]["test_runs"] == 1
        restored = fact_rows(target, key)
        assert restored["run_buckets"] == source["run_buckets"]
        assert restored["ip_observations"] == source["ip_observations"]
        assert main(args) == 2
        assert "--date needs --spaces-file" in capsys.readouterr().err


async def test_a_rerun_after_a_crash_deletes_and_a_changed_object_is_left_alone(database, tmp_path):
    await census_day(database, tmp_path)
    first = DAY - timedelta(days=1)
    with fake_bucket(tmp_path) as (bucket, settings):
        spaces = Spaces.from_file(settings)
        path, manifest = export_day(database, first, tmp_path / "out")
        store(spaces, path, manifest)  # uploaded, then the run stopped before deleting
        with connect(database) as conn:  # a dimension row added since: other bytes, same facts
            conn.execute("INSERT INTO providers VALUES ('newcomer')")
        before = bucket.objects[f"/{object_key(first)}"]
        # Another object already holds DAY's key: DAY is left alone and the run fails.
        bucket.objects[f"/{object_key(DAY)}"] = (b"other", [("x-amz-meta-facts", "0")], "private")
        with pytest.raises(ArchiveError, match="archive_changed: raw/2026/2026-10-01.tar.gz"):
            run_due(database, spaces, tmp_path / "work", LATER)
        assert bucket.objects[f"/{object_key(first)}"] == before  # never overwritten
        assert dict(before[1])["x-amz-meta-facts"] == facts_digest(manifest)
        with connect(database) as conn:  # the first day went; DAY's batch stays
            assert conn.execute("SELECT count(*) FROM test_batches").fetchone()[0] == 1
        del bucket.objects[f"/{object_key(DAY)}"]
        assert run_due(database, spaces, tmp_path / "work", LATER)["archived"] == ["2026-10-01"]


async def test_purge_refuses_a_day_that_is_not_due_or_changed(database, tmp_path):
    key = await census_day(database, tmp_path)
    _, manifest = export_day(database, DAY, tmp_path / "out")
    with pytest.raises(ArchiveError, match="not of 2026-10-02"):
        purge_day(database, DAY + timedelta(days=1), manifest, LATER)
    with pytest.raises(ArchiveError, match="is not due"):
        purge_day(database, DAY, manifest, LATER - timedelta(days=5))
    with connect(database) as conn:
        conn.execute("UPDATE batch_runs SET status='running'")
    with pytest.raises(ArchiveError, match="still open"):
        purge_day(database, DAY, manifest, LATER)
    with connect(database) as conn:
        conn.execute("UPDATE batch_runs SET status='done'")
        conn.execute("DELETE FROM calculation_roles WHERE run_key=%s", (key,))
    with pytest.raises(ArchiveError, match="changed since it was archived"):
        purge_day(database, DAY, manifest, LATER)
    with connect(database) as conn:
        assert (
            conn.execute("SELECT count(*) FROM test_runs WHERE run_key=%s", (key,)).fetchone()[0]
            == 1
        )


async def test_the_orchestrator_job_and_the_cli(database, tmp_path, capsys):
    await day_of_data(database, tmp_path)
    db = Database(database)
    try:
        assert await archive(db, database) == {"skipped": "archive.enabled is off"}
        with fake_bucket(tmp_path) as (bucket, settings):
            async with db.transaction() as conn:
                for name, value in (
                    ("archive.enabled", True),
                    ("archive.spaces_file", str(settings)),
                    ("archive.work_dir", str(tmp_path / "work")),
                ):
                    await conn.execute(
                        "INSERT INTO settings(key,value) VALUES (%s,%s::jsonb)",
                        (name, json.dumps(value)),
                    )
            # The day before DAY is due now and is archived; DAY waits for its run, which ran
            # today, so its rows are over RAW_DAYS + STALL_DAYS old and the job fails.
            with pytest.raises(ArchiveError, match="archive stalled: 2026-10-01"):
                await archive(db, database)
            assert list(bucket.objects) == ["/raw/2026/2026-09-30.tar.gz"]
            check = ["archive", "bucket", "--spaces-file", str(settings)]
            assert main(check) == 2 and "not private and versioned" in capsys.readouterr().err
            assert main([*check, "--apply"]) == 0
            assert json.loads(capsys.readouterr().out)["versioning"] == "Enabled"
            run = [
                "archive",
                "run",
                "--database-file",
                str(database),
                "--spaces-file",
                str(settings),
            ]
            assert main([*run, "--work-dir", str(tmp_path / "work")]) == 2
            assert "archive stalled" in capsys.readouterr().err
    finally:
        await db.close()


async def test_a_stalled_archive_fails_and_retention_keeps_unarchived_estimates(database, tmp_path):
    await census_day(database, tmp_path)
    with connect(database) as conn:
        conn.execute("UPDATE batch_runs SET status='running'")  # DAY can never be due
    stalled = LATER + timedelta(days=10)
    with fake_bucket(tmp_path) as (bucket, settings):
        with pytest.raises(ArchiveError, match="archive stalled: 2026-10-01.*still open"):
            run_due(database, Spaces.from_file(settings), tmp_path / "work", stalled)
        assert list(bucket.objects) == ["/raw/2026/2026-09-30.tar.gz"]  # the due day went
    db = Database(database)
    try:
        async with db.transaction() as conn:
            await put_setting(conn, "archive.enabled", True)
        # Long past retention.estimates_days: with the archive on, the estimates stay.
        assert (await retention(db, stalled + timedelta(days=30)))["estimated_hours_deleted"] == 0
        with connect(database) as conn:
            assert conn.execute("SELECT count(*) FROM estimated_hours").fetchone()[0] == 1
        async with db.transaction() as conn:
            await put_setting(conn, "archive.enabled", False)
        assert (await retention(db, stalled + timedelta(days=30)))["estimated_hours_deleted"] == 1
    finally:
        await db.close()


async def test_a_failed_upload_or_verification_leaves_every_row(database, tmp_path, monkeypatch):
    await census_day(database, tmp_path)

    def counts():
        with connect(database) as conn:
            return [
                conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("test_batches", "test_runs", "run_buckets", "estimated_hours")
            ]

    before = counts()
    with fake_bucket(tmp_path) as (bucket, settings):
        spaces = Spaces.from_file(settings)

        def bad_digest(*args, **kwargs):
            raise ArchiveError("Spaces PUT answered BadDigest")

        with monkeypatch.context() as patch:
            patch.setattr(Spaces, "put_file", bad_digest)
            with pytest.raises(ArchiveError, match="BadDigest"):
                run_due(database, spaces, tmp_path / "work", LATER)
        assert counts() == before and bucket.objects == {}
        real_head = Spaces.head

        def no_sha(self, key):  # the object is there, but without its stored sha256
            found = real_head(self, key)
            return found and {k: v for k, v in found.items() if k != "sha256"}

        monkeypatch.setattr(Spaces, "head", no_sha)
        with pytest.raises(ArchiveError, match="archive_changed: raw/2026/2026-09-30"):
            run_due(database, spaces, tmp_path / "work", LATER)
        assert counts() == before
