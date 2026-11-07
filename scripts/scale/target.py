#!/usr/bin/env python3
"""SCALE-SCHEMA-1: where the scale scripts may write, and the operator's schema SQL.

Usage:
  target.py sql                       the SQL the admin role runs to create and drop the schema
  target.py check --database <toml>   read-only: may this session be written to?
  target.py check --database <toml> --owner-go "owner-go REPLAY-RUN-1 <YYYY-MM-DD>"

The scripts write to a database named proxy_scale, or to the schema proxy_scale of another
database (production's proxy_reviews) when every process of the run has
PGOPTIONS='-c search_path=proxy_scale'. The search path must be exactly proxy_scale: with
public on it, a name the scratch schema lacks would reach production's table. The scripts never
create or drop a schema; the admin role does, with the SQL `target.py sql` prints.

The one exception is the owner's go for REPLAY-RUN-1 (#614): with it, the replay alone may write
to production's own database while it holds no test run but the replay's (owner_go_refusal).
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, date, datetime

SCRATCH = "proxy_scale"
SCHEMA_RULE = f"(or its schema {SCRATCH!r} with search_path exactly {SCRATCH!r})"
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
PRODUCTION = "proxy_reviews"
REPLAY_MARKER = "scale_replay"
GO = re.compile(r"owner-go REPLAY-RUN-1 (\d{4}-\d{2}-\d{2})")
GO_FORM = "'owner-go REPLAY-RUN-1 <YYYY-MM-DD>'"
IDENTITY = "SELECT current_database(),current_user,to_regclass('public.test_runs') IS NOT NULL"
FOREIGN_RUNS = f"SELECT count(*) FROM public.test_runs WHERE NOT (summary ? '{REPLAY_MARKER}')"


def go_date_refusal(owner_go: str, today: date) -> str | None:
    """None when the go has the owner's form and a date that is not after `today`."""
    match = GO.fullmatch(owner_go)
    try:
        given = date.fromisoformat(match.group(1)) if match else None
    except ValueError:
        given = None
    if given is None:
        return f"the owner's go {owner_go!r} is not {GO_FORM}"
    if given > today:
        return f"the owner's go is dated {given}, after today ({today})"
    return None


def owner_go_refusal(conn, owner_go: str) -> tuple[str | None, dict | None]:
    """(refusal, None), or (None, the accepted go's record) when the go is the owner's, the
    database is production's and it holds no test run without the replay's marker. The record is
    printed to stderr and kept in the replay's state and report."""
    now = datetime.now(UTC)
    database, role, has_runs = conn.execute(IDENTITY).fetchone()
    if database != PRODUCTION:
        return f"the owner's go is for {PRODUCTION!r}, not the database {database!r}", None
    found = go_date_refusal(owner_go, now.date())
    if found:
        return found, None
    foreign = conn.execute(FOREIGN_RUNS).fetchone()[0] if has_runs else 0
    if foreign:
        return f"{database!r} holds {foreign} test runs without the {REPLAY_MARKER!r} marker", None
    record = {"go": owner_go, "database": database, "role": role, "at": now.isoformat()}
    print(f"owner go accepted: {record}", file=sys.stderr)
    return None, record


def refusal(conn, expected: str = SCRATCH, owner_go: str | None = None) -> str | None:
    """None when the session may be written to: the database `expected`, or the schema
    proxy_scale alone on the search path; otherwise what the session is, for the refusal.
    With the owner's go (the replay only), production's database instead (owner_go_refusal)."""
    if owner_go is not None:
        return owner_go_refusal(conn, owner_go)[0]
    database, schema, path = conn.execute(STATE).fetchone()
    if database == expected or (schema == SCRATCH and path == SCRATCH):
        return None
    return f"the database is {database!r} with search_path {path!r}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("sql", "check"))
    parser.add_argument("--database", help="credentials TOML (check)")
    parser.add_argument("--owner-go", help=f"the owner's go for REPLAY-RUN-1: {GO_FORM}")
    args = parser.parse_args(argv)
    if args.command == "sql":
        print(SQL, end="")
        return 0
    if not args.database:
        parser.error("check needs --database")
    from proxy_index import storage

    with storage.connect(args.database) as conn:
        database, schema, path = conn.execute(STATE).fetchone()
        found = refusal(conn, owner_go=args.owner_go)
    print(f"database {database!r}, schema {schema!r}, search_path {path!r}")
    if found and args.owner_go is not None:
        print(f"refused: {found}")
        return 2
    if found:
        print(f"refused: {found}; the scale scripts write only to {SCRATCH!r} {SCHEMA_RULE}")
        return 2
    print("allowed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
