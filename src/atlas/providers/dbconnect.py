"""The keyword arguments for a psycopg connection from a `[postgres]` credentials TOML.

A host that is not this machine (any name but localhost, any address outside loopback)
must use TLS: a file without `sslmode` gets `sslmode=require`, and `disable`, `allow` or
`prefer`, which let the connection fall back to plain text, are refused. Local hosts and
Unix sockets keep the file's mode, so local and CI databases work without TLS.
`sslrootcert` names the CA file that `verify-ca` and `verify-full` check the server against.

CALC-STALE-SESSION-1: `session_options()` are the connection settings of the framework's own
sessions (storage.connect, the orchestrator's pool). A client host that vanished (a hard reset)
sends nothing more, so without them its session, and any transaction and advisory lock it held,
stayed on the server until the server's tcp_keepalives_idle (7200 s there) ran out.
- libpq's keepalives and tcp_user_timeout: the client notices a dead server or network.
- the server's own tcp_keepalives_* for this session: the server probes the client after 60 s of
  silence and ends the session about 2 minutes after the client went away.
- idle_in_transaction_session_timeout: the last bound on a transaction left open. A calculation
  keeps its transaction idle while it builds (15 to 24 minutes in production, Nov 2026) and may
  run 3 hours (CALC-LIMITS-3), so it is 3 hours; the keepalives are the fast path.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path

from .credentials import read_credentials

FIELDS = frozenset({"host", "port", "dbname", "user", "password", "sslmode", "sslrootcert"})
PLAINTEXT_MODES = frozenset({"disable", "allow", "prefer"})
KEEPALIVES = {
    "keepalives": "1",
    "keepalives_idle": "60",
    "keepalives_interval": "10",
    "keepalives_count": "6",
    "tcp_user_timeout": "300000",  # ms: data unacknowledged for 5 minutes drops the connection
}
SERVER_SETTINGS = (
    "tcp_keepalives_idle=60",
    "tcp_keepalives_interval=10",
    "tcp_keepalives_count=6",
    "idle_in_transaction_session_timeout=3h",
)


def _local(host: str) -> bool:
    if not host or host.startswith("/") or host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def connect_options(credentials_file: str | Path) -> dict[str, str]:
    """The file's fields, with the TLS rules above applied; ValueError when one fails."""
    credentials = read_credentials(credentials_file, "postgres")
    if set(credentials) - FIELDS:
        raise ValueError("Unsupported database connection field")
    hosts = credentials.get("host") or os.environ.get("PGHOST", "")
    if all(_local(host) for host in hosts.split(",")):
        return credentials
    if credentials.setdefault("sslmode", "require") in PLAINTEXT_MODES:
        raise ValueError(
            "The database host is not local: sslmode must be require, verify-ca or verify-full"
        )
    return credentials


def session_options(*settings: str) -> dict[str, str]:
    """KEEPALIVES, and SERVER_SETTINGS then settings ("name=value") added to $PGOPTIONS, which an
    explicit options parameter would replace (the scale tools set search_path there)."""
    extra = " ".join(f"-c {setting}" for setting in (*SERVER_SETTINGS, *settings))
    return KEEPALIVES | {"options": f"{os.environ.get('PGOPTIONS', '')} {extra}".strip()}
