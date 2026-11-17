"""A fake Spaces bucket with multipart uploads and faults (ARCHIVE-UPLOAD-ROBUST-1 tests).

PartsBucket is archive_job_cases.Bucket plus CreateMultipartUpload, UploadPart,
CompleteMultipartUpload and AbortMultipartUpload. Every request is labelled ("create",
"part <n>", "complete", "abort", "put", "head", "get") and logged in state["log"];
state["faults"] maps a label to what its next requests get, one each: "reset" (the connection
closed with a reset after the body was read), "hang" (no answer for HANG_S), "500"
(InternalError), "bad" (400 BadDigest) or "error200" (200 with an error body).
"""

import base64
import hashlib
import io
import re
import socket
import struct
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

from archive_job_cases import KEY_ID, SECRET, Bucket

HANG_S = 2.0


class PartsBucket(Bucket):
    def _label(self, query: dict) -> str:
        if "uploads" in query:
            return "create"
        if "partNumber" in query:
            return f"part {query['partNumber']}"
        if "uploadId" in query:
            return "abort" if self.command == "DELETE" else "complete"
        return self.command.lower()

    def _fault(self, label: str) -> bool:
        """Apply the label's next fault; True when the request was answered (or dropped)."""
        with self.state["lock"]:
            queued = self.state["faults"].get(label) or []
            fault = queued.pop(0) if queued else None
        if fault == "reset":
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.close_connection = True
            self.connection.close()
        elif fault == "hang":
            time.sleep(HANG_S)
            self.close_connection = True
        elif fault == "500":
            self._answer(500, b"<Error><Code>InternalError</Code></Error>")
        elif fault == "bad":
            self._answer(400, b"<Error><Code>BadDigest</Code></Error>")
        elif fault == "error200":
            self._answer(200, b"<Error><Code>InternalError</Code></Error>")
        return fault is not None

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if self.command in ("PUT", "POST") else None
        url, _, raw_query = self.path.partition("?")
        query = dict(q.partition("=")[::2] for q in raw_query.split("&") if q)
        label = self._label(query)
        with self.state["lock"]:
            self.state["log"].append(label)
        if self._fault(label):
            return None
        key = url.removeprefix("/archive")
        if label in ("create", "complete", "abort") or label.startswith("part "):
            if not self._signed(body):
                return self._answer(403, b"<Error><Code>SignatureDoesNotMatch</Code></Error>")
            return getattr(self, "_" + label.split()[0])(key, query, body)
        if self.command == "HEAD" and key in self.state["etags"]:
            data, meta, _ = self.objects[key]
            headers = [("ETag", f'"{self.state["etags"][key]}"'), *meta]
            return self._answer(200, data, headers)
        self.rfile = io.BytesIO(body or b"")
        return super()._handle()

    def _create(self, key, query, body):
        upload_id = uuid.uuid4().hex
        meta = [(k, v) for k, v in self.headers.items() if k.lower().startswith("x-amz-meta-")]
        self.state["uploads"][upload_id] = {"key": key, "parts": {}, "meta": meta}
        self.state["uploads"][upload_id]["acl"] = self.headers["x-amz-acl"]
        xml = f"<InitiateMultipartUploadResult><UploadId>{upload_id}</UploadId>"
        return self._answer(200, f"{xml}</InitiateMultipartUploadResult>".encode())

    def _part(self, key, query, body):
        md5 = hashlib.md5(body)
        if self.headers["Content-MD5"] != base64.b64encode(md5.digest()).decode():
            return self._answer(400, b"<Error><Code>BadDigest</Code></Error>")
        upload = self.state["uploads"].get(query["uploadId"])
        if upload is None:
            return self._answer(404, b"<Error><Code>NoSuchUpload</Code></Error>")
        with self.state["lock"]:
            upload["parts"][int(query["partNumber"])] = body
        return self._answer(200, headers=[("ETag", f'"{md5.hexdigest()}"')])

    def _complete(self, key, query, body):
        upload = self.state["uploads"].pop(query["uploadId"], None)
        if upload is None:
            return self._answer(404, b"<Error><Code>NoSuchUpload</Code></Error>")
        listed = re.findall(rb"<PartNumber>(\d+)</PartNumber><ETag>\"(\w+)\"</ETag>", body)
        parts = [upload["parts"][int(n)] for n, _ in listed]
        if [hashlib.md5(p).hexdigest().encode() for p in parts] != [e for _, e in listed]:
            return self._answer(400, b"<Error><Code>InvalidPart</Code></Error>")
        self.objects[key] = (b"".join(parts), upload["meta"], upload["acl"])
        digests = b"".join(hashlib.md5(p).digest() for p in parts)
        self.state["etags"][key] = f"{hashlib.md5(digests).hexdigest()}-{len(parts)}"
        return self._answer(200, b"<CompleteMultipartUploadResult/>")

    def _abort(self, key, query, body):
        self.state["uploads"].pop(query["uploadId"], None)
        self.state["aborted"] += 1
        return self._answer(204)

    do_GET = do_PUT = do_HEAD = do_POST = do_DELETE = _handle


@contextmanager
def parts_bucket(tmp_path):
    state = {"lock": threading.Lock(), "log": [], "faults": {}, "uploads": {}, "etags": {}}
    state["aborted"] = 0
    handler = type("Handler", (PartsBucket,), {"objects": {}, "state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
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
        server.server_close()
