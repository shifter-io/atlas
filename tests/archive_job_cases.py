"""Shared helpers of the archive job tests (test_archive_job.py): a fake S3 bucket behind a local
HTTP server, its Spaces settings file, and moving a census run onto DAY."""

import base64
import hashlib
import re
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_archive import SLOT, day_of_data

from proxy_index.archive.spaces import sign
from proxy_index.calculated import store
from proxy_index.partitions import partition
from proxy_index.storage import connect

KEY_ID, SECRET = "DO00EXAMPLE", "example-secret"


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
        # Close the listening socket now, not when the server is collected: until then its
        # port still accepts, so a request to it is reset instead of refused (FLAKE-1).
        server.server_close()


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


def spaces_settings(path):
    text = path.read_text()
    return dict(re.findall(r'(\w+) = "([^"]*)"', text))


async def census_day(database, tmp_path):
    """day_of_data with the census run moved onto DAY (its rows in DAY's partitions)."""
    store.initialize(database)
    key = await day_of_data(database, tmp_path)
    with connect(database) as conn:
        shift(conn, key, SLOT + timedelta(minutes=5))
    return key
