"""Test doubles for the DB-IP update: a tiny real MMDB and a loopback CDN.

`tiny_mmdb` writes a valid MaxMind DB (IPv4, one search-tree node): addresses in the lower half
of IPv4 resolve to `low`, the rest to `high`. `FakeCdn` serves dated files over HEAD and GET on
127.0.0.1 and counts the requests.
"""

from __future__ import annotations

import threading
import zlib
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

US = {"country": {"iso_code": "US", "names": {"en": "United States"}}}
DE = {"country": {"iso_code": "DE", "names": {"en": "Germany"}}}
NO_COUNTRY = {"traits": {"isp": "Example"}}


def _control(kind: int, size: int) -> bytes:
    """The control byte, the extended type byte and the size bytes (sizes under 285)."""
    low, extra = (size, b"") if size < 29 else (29, bytes([size - 29]))
    head = bytes([kind << 5 | low]) if kind <= 7 else bytes([low, kind - 7])
    return head + extra


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


class FakeCdn:
    """A loopback CDN directory: `files` maps a name to its bytes, `status` forces an answer
    for a name, `hits` counts (method, name)."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.status: dict[str, int] = {}
        self.cut: dict[str, int] = {}  # serve only this many bytes of a file
        self.hits: Counter[tuple[str, str]] = Counter()
        cdn = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, body: bool) -> None:
                name = self.path.removeprefix("/dbip/")
                cdn.hits[self.command, name] += 1
                status = cdn.status.get(name, 200 if name in cdn.files else 404)
                data = cdn.files.get(name, b"") if status == 200 else b""
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("ETag", f'"{zlib.crc32(data):08x}"')
                self.end_headers()
                if body:
                    self.wfile.write(data[: cdn.cut.get(name, len(data))])

            def do_HEAD(self) -> None:  # noqa: N802 - the http.server name
                self._answer(False)

            def do_GET(self) -> None:  # noqa: N802
                self._answer(True)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/dbip/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def downloads(self, name: str) -> int:
        return self.hits["GET", name]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
