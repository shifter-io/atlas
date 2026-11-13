#!/usr/bin/env python3
"""SCALE-SCHEMA-1: where the scale scripts may write, and the operator's schema SQL.

Usage:
  target.py sql                       the SQL the admin role runs to create and drop the schema
  target.py check --database <toml>   read-only: may this session be written to?
  target.py check --database <toml> --owner-go "owner-go REPLAY-RUN-1 <YYYY-MM-DD>"
  target.py check --database <toml> --owner-go "owner-go PRODUCTION-AS-STAGING <YYYY-MM-DD>"
                  --i-know-this-is-production       (and ATLAS_ALLOW_PRODUCTION_TARGET=1)

The scripts write to a database named proxy_scale, or to the schema proxy_scale of another
database (production's proxy_reviews) when every process of the run has
PGOPTIONS='-c search_path=proxy_scale'. The search path must be exactly proxy_scale: with
public on it, a name the scratch schema lacks would reach production's table. The scripts never
create or drop a schema; the admin role does, with the SQL `target.py sql` prints.

The one exception is the owner's go for REPLAY-RUN-1 (#614): with it, the replay alone may write
to the staging database proxy_reviews_staging (same server as production, created empty) while it
holds no test run but the replay's (owner_go_refusal). Production's proxy_reviews stays refused,
except as a second staging database (PROD-AS-STAGING-1, #817): simulate.py, wipe_replay.py and
clean_target.py accept it only with all three of --owner-go "owner-go PRODUCTION-AS-STAGING
<YYYY-MM-DD>", --i-know-this-is-production and ATLAS_ALLOW_PRODUCTION_TARGET=1 in the
environment (production_refusal, in atlas.production_guard). generate.py, cycle.py and
replay.py never pass them.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, date, datetime

from atlas import production_guard as guard
from atlas.production_guard import (
    ACKNOWLEDGE,
    PRODUCTION,
    PRODUCTION_GO_FORM,
    production_refusal,
)
from atlas.production_guard import ALLOW as ALLOW  # the scripts' and tests' name

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
STAGING = "proxy_reviews_staging"
REPLAY_MARKER = "scale_replay"
GO = re.compile(r"owner-go REPLAY-RUN-1 (\d{4}-\d{2}-\d{2})")
GO_FORM = "'owner-go REPLAY-RUN-1 <YYYY-MM-DD>'"
BANNER = "*** PRODUCTION DATABASE {!r} AS STAGING: owner go {!r}, role {!r}, {} ***"
IDENTITY = "SELECT current_database(),current_user,to_regclass('public.test_runs') IS NOT NULL"
FOREIGN_RUNS = f"SELECT count(*) FROM public.test_runs WHERE NOT (summary ? '{REPLAY_MARKER}')"


def go_date_refusal(owner_go: str, today: date, pattern=GO, form=GO_FORM) -> str | None:
    """None when the go has the owner's form (`pattern`) and a date that is not after `today`."""
    return guard.go_date_refusal(owner_go, today, pattern, form)


def owner_go_refusal(
    conn, owner_go: str | None, acknowledged: bool = False, runs: bool = True
) -> tuple[str | None, dict | None]:
    """(refusal, None), or (None, the accepted go's record) when the go is the owner's for the
    database: staging's with the REPLAY-RUN-1 go, or production's with all three of
    production_refusal; and, with `runs`, it holds no test run without the replay's marker.
    The record is printed to stderr (production's with a banner) and kept in the state and
    report."""
    now = datetime.now(UTC)
    database, role, has_runs = conn.execute(IDENTITY).fetchone()
    if database == PRODUCTION:
        found = production_refusal(owner_go, acknowledged, now.date())
    elif database != STAGING:
        found = f"the owner's go is for {STAGING!r}, not the database {database!r}"
    else:
        found = go_date_refusal(owner_go or "", now.date())
    if found:
        return found, None
    foreign = conn.execute(FOREIGN_RUNS).fetchone()[0] if runs and has_runs else 0
    if foreign:
        return f"{database!r} holds {foreign} test runs without the {REPLAY_MARKER!r} marker", None
    record = {"go": owner_go, "database": database, "role": role, "at": now.isoformat()}
    if database == PRODUCTION:
        record["production"] = True
        print(BANNER.format(database, owner_go, role, record["at"]), file=sys.stderr)
    print(f"owner go accepted: {record}", file=sys.stderr)
    return None, record


def refusal(
    conn, expected: str = SCRATCH, owner_go: str | None = None, acknowledged: bool = False
) -> str | None:
    """None when the session may be written to: the database `expected`, or the schema
    proxy_scale alone on the search path; otherwise what the session is, for the refusal.
    With the owner's go (the replay, the load), the staging database instead, or production's
    with its acknowledgement (owner_go_refusal)."""
    if owner_go is not None:
        return owner_go_refusal(conn, owner_go, acknowledged)[0]
    database, schema, path = conn.execute(STATE).fetchone()
    if database == expected or (schema == SCRATCH and path == SCRATCH):
        return None
    return f"the database is {database!r} with search_path {path!r}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("sql", "check"))
    parser.add_argument("--database", help="credentials TOML (check)")
    parser.add_argument("--owner-go", help=f"the owner's go: {GO_FORM} or {PRODUCTION_GO_FORM}")
    parser.add_argument(ACKNOWLEDGE, action="store_true", help="the go is for production")
    args = parser.parse_args(argv)
    if args.command == "sql":
        print(SQL, end="")
        return 0
    if not args.database:
        parser.error("check needs --database")
    from atlas import storage

    with storage.connect(args.database) as conn:
        database, schema, path = conn.execute(STATE).fetchone()
        acknowledged = args.i_know_this_is_production
        found = refusal(conn, owner_go=args.owner_go, acknowledged=acknowledged)
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
