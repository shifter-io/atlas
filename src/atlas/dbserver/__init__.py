"""The PostgreSQL server the framework runs on: a read-only check and the nightly ANALYZE.

`atlas db-server check` (check.py) reports the server's version, SSL, settings,
extensions, sizes, roles, owners and grants from one read-only transaction, and writes every
privilege gap it finds as the exact statements that close it. It never runs them; an operator
runs them once with the admin config. `atlas db-server analyze` (analyze.py) runs ANALYZE
on the partitioned parents, which autovacuum never analyzes, and prints one health line.

Both read an existing database TOML in place, with the TLS rules of providers/dbconnect.py
(a host that is not local must use TLS; a file without `sslmode` gets `sslmode=require`).
No output holds the password. PostgreSQL 17 or newer (MAINTAIN privilege).
"""

from __future__ import annotations

from typing import NamedTuple

from ..providers.dbconnect import connect_options

SCHEMA = "public"  # the framework's tables and views


class Roles(NamedTuple):
    """The roles the check judges, by name."""

    runtime: str = "proxy_reviews"  # writes every table, owns the run-time partitions
    api: str = "proxy_api"  # reads every framework table and view
    admin: str = "proxy_admin"  # runs migrations and creates scratch databases
    ingest: str = "atlas_ingest"  # the upload path's login: adds runs (dbserver/ingest.py)


def connect(credentials_file, **options):
    """A psycopg connection from a `[postgres]` TOML (see providers/dbconnect.py)."""
    import psycopg

    return psycopg.connect(**connect_options(credentials_file), connect_timeout=5, **options)
