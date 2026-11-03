"""Test doubles for the DB-IP update: a tiny real MMDB and a loopback DB-IP account.

`tiny_mmdb` writes a valid MaxMind DB (IPv4, one search-tree node): addresses below 128.0.0.0
resolve to `low`, the rest to `high`. `FakeAccount` serves the account index JSON and the
download on 127.0.0.1; its URLs carry KEY, which no output of the update may contain.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

KEY = "acct-key-0b5e55ed"
US = {"country": {"iso_code": "US", "names": {"en": "United States"}}}
DE = {"country": {"iso_code": "DE", "names": {"en": "Germany"}}}
NO_COUNTRY = {"traits": {"isp": "Example"}}


def _control(kind: int, size: int) -> bytes:
    assert size < 29
    return bytes([kind << 5 | size]) if kind <= 7 else bytes([size, kind - 7])


def _uint(kind: int, value: int) -> bytes:
    data = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return _control(kind, len(data)) + data


def encode(value) -> bytes:
    """MaxMind DB data: str, dict, list and int (uint32); `(kind, int)` for another uint."""
    if isinstance(value, tuple):
        return _uint(*value)
    if isinstance(value, int):
        return _uint(6, value)
    if isinstance(value, str):
        data = value.encode()
        return _control(2, len(data)) + data
    if isinstance(value, dict):
        items = b"".join(encode(k) + encode(v) for k, v in value.items())
        return _control(7, len(value)) + items
    return _control(11, len(value)) + b"".join(encode(v) for v in value)


def tiny_mmdb(
    low=US, high=DE, *, database_type="DBIP-Location-ISP (compat=Enterprise)", marker="a"
) -> bytes:
    first, second = encode(low), encode(high)
    node_count = 1
    pointers = (node_count + 16, node_count + 16 + len(first))
    tree = b"".join(p.to_bytes(3, "big") for p in pointers)
    metadata = {
        "binary_format_major_version": (5, 2),
        "binary_format_minor_version": (5, 0),
        "build_epoch": (9, 1_700_000_000 + ord(marker[0])),
        "database_type": database_type,
        "description": {"en": f"fixture {marker}"},
        "ip_version": (5, 4),
        "languages": ["en"],
        "node_count": (6, node_count),
        "record_size": (5, 24),
    }
    return tree + bytes(16) + first + second + b"\xab\xcd\xefMaxMind.com" + encode(metadata)


class FakeAccount:
    """A loopback DB-IP account: `publish` sets what the index offers; `hits` counts GETs."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes]] = {}
        self.hits: Counter[str] = Counter()
        account = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the http.server name
                account.hits[self.path] += 1
                status, body = account.routes.get(self.path, (404, b"missing"))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def index_path(self) -> str:
        return f"/account/{KEY}/db/ip-to-location-isp/"

    @property
    def url(self) -> str:
        return self.base + self.index_path

    def download_path(self, version: str) -> str:
        return f"/download/{KEY}/{version}.mmdb.gz"

    def publish(self, data: bytes, version: str, *, packed=True, nest=False, **fields) -> dict:
        """Offer `data` as the account's MMDB; `fields` override the index entry's fields."""
        body = gzip.compress(data) if packed else data
        path = self.download_path(version)
        entry = {
            "url": self.base + path,
            "name": f"dbip-full-{version}.mmdb" + (".gz" if packed else ""),
            "date": version,
            "version": version,
            "size": len(body),
            "md5sum": hashlib.md5(body).hexdigest(),
            "sha1sum": hashlib.sha1(body).hexdigest(),
        } | fields
        entry = {key: value for key, value in entry.items() if value is not None}
        index = {"ip-to-location-isp": {"mmdb": entry}} if nest else {"csv": {}, "mmdb": entry}
        self.routes[self.index_path] = (200, json.dumps(index).encode())
        self.routes[path] = (200, body)
        return entry

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def url_file(tmp_path: Path, url: str, mode: int = 0o600) -> Path:
    path = tmp_path / "dbip-account-url"
    path.write_text(url + "\n")
    path.chmod(mode)
    return path
