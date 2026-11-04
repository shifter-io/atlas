"""The PostgreSQL server the framework runs on: a read-only check and the nightly ANALYZE.

`proxy-index db-server check` (check.py) reports the server's version, SSL, settings,
extensions, sizes, roles, owners and grants from one read-only transaction, and writes every
privilege gap it finds as the exact statements that close it. It never runs them; an operator
runs them once with the admin config. `proxy-index db-server analyze` (analyze.py) runs ANALYZE
on the partitioned parents, which autovacuum never analyzes, and prints one health line.

Both read an existing database TOML in place. A file without `sslmode` connects with
`sslmode=require`. No output holds the password. PostgreSQL 17 or newer (MAINTAIN privilege).
"""

from __future__ import annotations

from typing import NamedTuple

from ..providers.credentials import read_credentials

SCHEMA = "public"  # the framework's tables and views
FIELDS = {"host", "port", "dbname", "user", "password", "sslmode"}


class Roles(NamedTuple):
    """The roles the check judges, by name."""

    runtime: str = "proxy_reviews"  # writes every table, owns the run-time partitions
    api: str = "proxy_api"  # reads every framework table and view
    admin: str = "proxy_admin"  # runs migrations and creates scratch databases


def connect(credentials_file, **options):
    """A psycopg connection from a `[postgres]` TOML; SSL is required unless the file says."""
    import psycopg

    credentials = read_credentials(credentials_file, "postgres")
    if set(credentials) - FIELDS:
        raise ValueError("Unsupported database connection field")
    return psycopg.connect(**({"sslmode": "require"} | credentials), connect_timeout=5, **options)
