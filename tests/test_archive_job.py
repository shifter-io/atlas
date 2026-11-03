"""ARCHIVE-1 part 2: the Spaces client, deleting archived days and the daily archive job."""

import base64
import hashlib
import json
import re
import subprocess
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_archive import DAY, SLOT, day_of_data, empty_database, fact_rows

from proxy_index.archive import ArchiveError
from proxy_index.archive import spaces as spaces_module
from proxy_index.archive.export import export_day
from proxy_index.archive.job import facts_digest, object_key, run_due, store
from proxy_index.archive.purge import purge_day
from proxy_index.archive.spaces import Spaces, manager_settings, sign
from proxy_index.cli import main
from proxy_index.orchestrator.db import Database
from proxy_index.orchestrator.jobs import archive
from proxy_index.partitions import children, partition
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

KEY_ID, SECRET = "DO00EXAMPLE", "example-secret"
# When DAY (and the day before) are due: their rows end on DAY, 30 days earlier. The daily-test
# run of the fixture starts at the real time, later, so its day is still waiting then.
LATER = datetime(2026, 11, 5, tzinfo=UTC)


class Bucket(BaseHTTPRequestHandler):
    """A fake S3 bucket: checks each request's signature, payload hash and Content-MD5."""

    objects: dict
    state: dict

    def log_message(self, *args):
        pass

    def _signed(self, body):
        url, _, query = self.path.partition("?")
        headers = {
            k: v for k, v in self.headers.items() if k.lower() not in ("host", "authorization")
        }
        names = re.search(r"SignedHeaders=([^,]+)", self.headers["Authorization"])[1].split(";")
        headers = {k: v for k, v in headers.items() if k.lower() in names}
        request = {"method": self.command, "host": self.headers["Host"], "path": url}
        request |= {"query": dict(q.partition("=")[::2] for q in query.split("&") if q)}
        request |= {"headers": headers, "payload": self.headers["x-amz-content-sha256"]}
        now = datetime.strptime(self.headers["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        expected = sign(request, KEY_ID, SECRET, "fra1", now)
        payload_ok = body is None or hashlib.sha256(body).hexdigest() == request["payload"]
        return self.headers["Authorization"] == expected and payload_ok

    def _answer(self, status, body=b"", headers=()):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if self.command == "PUT" else None
        if not self._signed(body):
            return self._answer(403, b"<Error><Code>SignatureDoesNotMatch</Code></Error>")
        key = self.path.partition("?")[0].removeprefix("/archive")
        query = self.path.partition("?")[2]
        if query == "versioning":
            if self.command == "PUT":
                self.state["versioning"] = "Enabled"
                return self._answer(200)
            status = self.state.get("versioning")
            xml = f"<Status>{status}</Status>" if status else ""
            return self._answer(
                200, f"<VersioningConfiguration>{xml}</VersioningConfiguration>".encode()
            )
        if query == "acl":
            grants = "".join(f"<Grant><URI>{u}</URI></Grant>" for u in self.state.get("grants", ()))
            return self._answer(
                200, f"<AccessControlPolicy>{grants}</AccessControlPolicy>".encode()
            )
        if self.command == "PUT":
            md5 = hashlib.md5(body).digest()
            if base64.b64decode(self.headers["Content-MD5"]) != md5:
                return self._answer(400, b"<Error><Code>BadDigest</Code></Error>")
            meta = [(k, v) for k, v in self.headers.items() if k.lower().startswith("x-amz-meta-")]
            self.objects[key] = (body, meta, self.headers["x-amz-acl"])
            return self._answer(200, headers=[("ETag", f'"{md5.hex()}"')])
        if key not in self.objects:
            return self._answer(404)
        body, meta, _ = self.objects[key]
        headers = [("ETag", f'"{hashlib.md5(body).hexdigest()}"'), *meta]
        return self._answer(200, body, headers)

    do_GET = do_PUT = do_HEAD = _handle


@contextmanager
def fake_bucket(tmp_path):
    handler = type("Handler", (Bucket,), {"objects": {}, "state": {}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = tmp_path / "spaces.toml"
    settings.write_text(
        f'[spaces]\nendpoint = "http://127.0.0.1:{server.server_port}"\nbucket = "archive"\n'
        f'region = "fra1"\naccess_key_id = "{KEY_ID}"\nsecret_access_key = "{SECRET}"\n'
    )
    settings.chmod(0o600)
    try:
        yield handler, settings
    finally:
        server.shutdown()


def shift(conn, run_key, to):
    """Move a run and its bucket rows so it starts at `to` (rows change partitions)."""
    start = conn.execute("SELECT started_at FROM test_runs WHERE run_key=%s", (run_key,))
    delta = to - start.fetchone()[0]
    for table in ("run_buckets", "ip_observations", "run_geo_buckets"):
        partition(conn, table, to)
        times = ",first_seen=first_seen+%(d)s,last_seen=last_seen+%(d)s" * (
            table == "ip_observations"
        )
        conn.execute(
            f"UPDATE {table} SET bucket_start=bucket_start+%(d)s{times} WHERE run_key=%(k)s",
            {"d": delta, "k": run_key},
        )
    conn.execute(
        "UPDATE test_runs SET started_at=started_at+%(d)s,finished_at=finished_at+%(d)s "
        "WHERE run_key=%(k)s",
        {"d": delta, "k": run_key},
    )


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


def spaces_settings(path):
    text = path.read_text()
    return dict(re.findall(r'(\w+) = "([^"]*)"', text))


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


async def census_day(database, tmp_path):
    """day_of_data with the census run moved onto DAY (its rows in DAY's partitions)."""
    key = await day_of_data(database, tmp_path)
    with connect(database) as conn:
        shift(conn, key, SLOT + timedelta(minutes=5))
    return key


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
            # The day before DAY is due now; DAY waits for its run, which ran today.
            assert (await archive(db, database))["archived"] == ["2026-09-30"]
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
            assert main([*run, "--work-dir", str(tmp_path / "work")]) == 0
            assert json.loads(capsys.readouterr().out)["archived"] == []
    finally:
        await db.close()
