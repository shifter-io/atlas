#!/usr/bin/env python3
"""SCALE-SCHEMA-1: where the scale scripts may write, and the operator's schema SQL.

Usage:
  target.py sql                       the SQL the admin role runs to create and drop the schema
  target.py check --database <toml>   read-only: may this session be written to?

The scripts write to a database named proxy_scale, or to the schema proxy_scale of another
database (production's proxy_reviews) when every process of the run has
PGOPTIONS='-c search_path=proxy_scale'. The search path must be exactly proxy_scale: with
public on it, a name the scratch schema lacks would reach production's table. The scripts never
create or drop a schema; the admin role does, with the SQL `target.py sql` prints.
"""

from __future__ import annotations

import argparse
import sys

SCRATCH = "proxy_scale"
ALLOWED = f"(or its schema {SCRATCH!r} with search_path exactly {SCRATCH!r})"
STATE = "SELECT current_database(),current_schema(),current_setting('search_path')"
SQL = f"""\
-- Before the run, as proxy_admin in proxy_reviews:
CREATE SCHEMA {SCRATCH} AUTHORIZATION proxy_admin;
-- Every command of the run: PGOPTIONS='-c search_path={SCRATCH}'
-- After the run, as proxy_admin in proxy_reviews:
DROP SCHEMA {SCRATCH} CASCADE;
-- Read-only check that it is gone (0):
SELECT count(*) FROM pg_namespace WHERE nspname='{SCRATCH}';
"""


def refusal(conn, expected: str = SCRATCH) -> str | None:
    """None when the session may be written to: the database `expected`, or the schema
    proxy_scale alone on the search path; otherwise what the session is, for the refusal."""
    database, schema, path = conn.execute(STATE).fetchone()
    if database == expected or (schema == SCRATCH and path == SCRATCH):
        return None
    return f"the database is {database!r} with search_path {path!r}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("sql", "check"))
    parser.add_argument("--database", help="credentials TOML (check)")
    args = parser.parse_args(argv)
    if args.command == "sql":
        print(SQL, end="")
        return 0
    if not args.database:
        parser.error("check needs --database")
    from proxy_index import storage

    with storage.connect(args.database) as conn:
        database, schema, path = conn.execute(STATE).fetchone()
        found = refusal(conn)
    print(f"database {database!r}, schema {schema!r}, search_path {path!r}")
    if found:
        print(f"refused: {found}; the scale scripts write only to {SCRATCH!r} {ALLOWED}")
        return 2
    print("allowed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
