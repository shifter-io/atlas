"""Byte accounting.

What a provider bills is bytes on the wire between the client and the gateway,
including the CONNECT exchange (or SOCKS5 negotiation) and the TLS handshake on
HTTPS. An application-layer estimate misses all of that, so on Linux the count
comes from the kernel: `TCP_INFO` carries `tcpi_bytes_acked` and
`tcpi_bytes_received` per socket, read before the socket is closed.

macOS has no comparable field, so laptop runs fall back to an estimate built
from the actual request line, headers and body sizes plus a measured constant
for the handshake. Estimated figures are flagged as such (`exact=False`),
because "we measured 26 MB" and "we think it was about 26 MB" differ when they
are compared against an invoice.

Ported from the old collector's engine/bytes_io.py (removed from the website
repository); the SOCKS5 negotiation size and the `connect` switch are framework
additions.
"""

from __future__ import annotations

import socket
import struct
import sys
from dataclasses import dataclass

# Linux UAPI struct tcp_info: bytes_acked and bytes_received follow the
# 104-byte base and two u64 pacing-rate fields, at offsets 120 and 128
# on all current kernels (>= 4.1).
_TCP_INFO_MIN_LEN = 136
_OFF_BYTES_ACKED = 120
_OFF_BYTES_RECEIVED = 128

# tcpi_state values (include/net/tcp_states.h) after our FIN was acknowledged
# and after the peer's FIN arrived.
_OWN_FIN_ACKED = frozenset({5, 6})  # FIN_WAIT2, TIME_WAIT
_PEER_FIN_RECEIVED = frozenset({6, 8, 9, 11})  # TIME_WAIT, CLOSE_WAIT, LAST_ACK, CLOSING

LINUX = sys.platform.startswith("linux")

# `socket.TCP_INFO` is absent on macOS; 11 is the Linux value.
_TCP_INFO = getattr(socket, "TCP_INFO", 11)


@dataclass(slots=True)
class ByteCount:
    """Bytes moved on one connection, and how we know."""

    up: int = 0
    down: int = 0
    exact: bool = True

    def __add__(self, other: ByteCount) -> ByteCount:
        return ByteCount(
            up=self.up + other.up,
            down=self.down + other.down,
            exact=self.exact and other.exact,
        )

    def __iadd__(self, other: ByteCount) -> ByteCount:
        self.up += other.up
        self.down += other.down
        self.exact = self.exact and other.exact
        return self


def from_socket(sock: object) -> ByteCount | None:
    """Read `TCP_INFO`. Returns None where the platform has no equivalent."""
    if not LINUX or sock is None:
        return None
    try:
        raw = sock.getsockopt(socket.IPPROTO_TCP, _TCP_INFO, 256)  # type: ignore[attr-defined]
    except (OSError, ValueError, AttributeError):
        # A socket already closed by the connector, or a kernel too old to
        # carry the counters. The estimate is the fallback.
        return None
    if len(raw) < _TCP_INFO_MIN_LEN:
        return None
    state = raw[0]
    (acked,) = struct.unpack_from("=Q", raw, _OFF_BYTES_ACKED)
    (received,) = struct.unpack_from("=Q", raw, _OFF_BYTES_RECEIVED)
    # Each FIN, like the SYN, takes one sequence number that the counters
    # include. bytes_acked counts the initial SYN, so it is one higher than the
    # payload; CLOSE is left alone because a reset and a full close look alike.
    acked -= 1 + (state in _OWN_FIN_ACKED)
    received -= state in _PEER_FIN_RECEIVED
    return ByteCount(up=max(acked, 0), down=max(received, 0), exact=True)


# Measured against checkip.amazonaws.com on 2026-09-09. Providers meter
# application bytes, so TCP/IP framing is excluded; the CONNECT exchange and
# the TLS handshake are not, because they cross the metered link.
FRAMING_BYTES = 0
CONNECT_REQUEST_BYTES = 60
CONNECT_RESPONSE_BYTES = 39
TLS_HANDSHAKE_UP_BYTES = 600
TLS_HANDSHAKE_DOWN_BYTES = 4_400
TLS_RESUMED_UP_BYTES = 320
TLS_RESUMED_DOWN_BYTES = 700


def estimate(
    *,
    request_line: int,
    request_headers: int,
    request_body: int,
    response_headers: int,
    response_body: int,
    tls: bool = False,
    tls_resumed: bool = False,
    connect: bool | None = None,
    extra_up: int = 0,
    extra_down: int = 0,
) -> ByteCount:
    """Application-layer estimate, for platforms without `TCP_INFO`.

    Sizes come from the bytes actually sent and received, so the only estimated
    parts are the CONNECT exchange and the handshake. `connect` defaults to
    `tls` (an HTTP gateway tunnels TLS through CONNECT); pass False for SOCKS5,
    whose negotiation goes in `extra_up`/`extra_down` (see `socks5_handshake`).
    """
    up = request_line + request_headers + request_body + extra_up
    down = response_headers + response_body + extra_down
    if connect if connect is not None else tls:
        up += CONNECT_REQUEST_BYTES
        down += CONNECT_RESPONSE_BYTES
    if tls:
        if tls_resumed:
            up += TLS_RESUMED_UP_BYTES
            down += TLS_RESUMED_DOWN_BYTES
        else:
            up += TLS_HANDSHAKE_UP_BYTES
            down += TLS_HANDSHAKE_DOWN_BYTES
    return ByteCount(up=up, down=down, exact=False)


def socks5_handshake(
    *, username: str | None, password: str | None, host: str, ipv6_bound: bool = False,
) -> tuple[int, int]:
    """(up, down) bytes of a SOCKS5 greeting, auth and CONNECT with remote DNS.

    Every field has a fixed or known length, so this is a calculation rather
    than a measured constant. The reply's bound address is assumed IPv4.
    """
    auth = username is not None
    up = 2 + (2 if auth else 1)  # VER NMETHODS METHODS...
    down = 2  # VER METHOD
    if auth:
        up += 3 + len(username.encode()) + len((password or "").encode())
        down += 2
    up += 4 + 1 + _size(host) + 2  # VER CMD RSV ATYP LEN HOST PORT
    down += 4 + (16 if ipv6_bound else 4) + 2
    return up, down


def header_bytes(headers: object) -> int:
    """Wire size of a header mapping: `Name: value\\r\\n` per entry, plus the
    terminating blank line."""
    items = headers.items() if hasattr(headers, "items") else headers  # type: ignore[union-attr]
    total = 2
    for name, value in items:  # type: ignore[misc]
        total += _size(name) + 2 + _size(value) + 2
    return total


def _size(value: object) -> int:
    if isinstance(value, bytes):
        return len(value)
    return len(str(value).encode("utf-8", errors="replace"))
