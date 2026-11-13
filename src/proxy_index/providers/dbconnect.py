"""The keyword arguments for a psycopg connection from a `[postgres]` credentials TOML.

A host that is not this machine (any name but localhost, any address outside loopback)
must use TLS: a file without `sslmode` gets `sslmode=require`, and `disable`, `allow` or
`prefer`, which let the connection fall back to plain text, are refused. Local hosts and
Unix sockets keep the file's mode, so local and CI databases work without TLS.
`sslrootcert` names the CA file that `verify-ca` and `verify-full` check the server against.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path

from .credentials import read_credentials

FIELDS = frozenset({"host", "port", "dbname", "user", "password", "sslmode", "sslrootcert"})
PLAINTEXT_MODES = frozenset({"disable", "allow", "prefer"})


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
