#!/usr/bin/env python3
"""PROD-AS-STAGING-1: empty a staging database's measurement and calculated data, keeping its
configuration, catalog and schema versions.

Usage:
  clean_target.py --database <toml> --out <dir>
                  --owner-go "owner-go PRODUCTION-AS-STAGING <YYYY-MM-DD>"
                  --i-know-this-is-production [--apply]
                  (with PROXY_INDEX_ALLOW_PRODUCTION_TARGET=1 in the environment)
  clean_target.py --database <toml> --out <dir> --owner-go "owner-go CLEAN-STAGING <YYYY-MM-DD>"
                  [--apply]                                (staging, proxy_reviews_staging)

Owner decision (2026-11-10): the real-data two-day test uses production's proxy_reviews as a
second staging database; "clean the db and make the db structure up to date". Production takes
target.py's three (production_refusal); staging its own CLEAN-STAGING go; any other database is
refused, and so is one where a job_runs row started in the last 5 minutes (an orchestrator).

Every table of the public schema but KEEP is emptied with one TRUNCATE ... RESTART IDENTITY
(no CASCADE: a kept table that referenced an emptied one would fail the statement, never be
emptied with it), and the emptied tables' partitions are dropped. KEEP and the whole site schema
(the catalog) are kept, row for row. The dry run (the default) does it in one REPEATABLE READ
transaction and rolls it back: it prints per table the rows it would delete and the tables it
keeps, and writes them to <out>/clean-dry-run.json (mode 600). --apply needs that file, of the
same database and from the last hour; it runs the same transaction and commits it only when
every table's deleted rows equal the dry run's. SIMULATE.md ("Cleaning a target") has more.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import replay_baseline
import target
from psycopg import IsolationLevel, errors, sql
from replay import LIVE
from replay_report import write_private
from wipe_replay import LOCKS

from proxy_index.storage import connect

KEEP = ("settings", "countries", "targets", "runner_regions", "methodologies")
KEEP += ("schema_versions", "calculation_schema", "providers", "products")
KEPT_SCHEMAS = ("site",)
GO = re.compile(r"owner-go CLEAN-STAGING (\d{4}-\d{2}-\d{2})")
GO_FORM = "'owner-go CLEAN-STAGING <YYYY-MM-DD>'"
DRY_RUN = "clean-dry-run.json"
FRESH = timedelta(hours=1)
TABLES = (
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
    "WHERE n.nspname='public' AND c.relkind IN ('r','p') AND NOT c.relispartition ORDER BY 1"
)
PARTITIONS = (
    "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid "
    "JOIN pg_class p ON p.oid=i.inhparent JOIN pg_namespace n ON n.oid=p.relnamespace "
    "WHERE n.nspname='public' AND p.relname=ANY(%s) ORDER BY 1"
)


class Refused(Exception):
    """The database, the go, the dry run or an orchestrator does not allow it; nothing changed."""


def guard(conn, args) -> dict:
    """The accepted go's record: production's with its three, staging's with its own go."""
    database, role = conn.execute("SELECT current_database(),current_user").fetchone()
    if database == target.PRODUCTION:
        acknowledged = args.i_know_this_is_production
        found, record = target.owner_go_refusal(conn, args.owner_go, acknowledged, runs=False)
    elif database == target.STAGING:
        now = datetime.now(UTC)
        found = target.go_date_refusal(args.owner_go or "", now.date(), GO, GO_FORM)
        record = {"go": args.owner_go, "database": database, "role": role}
        record["at"] = now.isoformat()
    else:
        found = f"cleans only {target.STAGING!r} or {target.PRODUCTION!r}, not {database!r}"
    if found:
        raise Refused(found)
    return record


def clean(conn, doomed: list[str], drop: bool) -> dict:
    """Empty the doomed tables in the open transaction, their partitions dropped when `drop`:
    the rows deleted per table, the kept tables' rows and the partitions."""
    names = sql.SQL(",").join(map(sql.Identifier, doomed))
    conn.execute("SET LOCAL lock_timeout='60s'")
    conn.execute("SET LOCAL statement_timeout=0")
    # Before any query: the snapshot below is taken with no writer left on an emptied table.
    conn.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(names))
    for lock in LOCKS:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
    if conn.execute(LIVE).fetchone()[0]:
        raise Refused("a job_runs row started in the last 5 minutes: an orchestrator writes here")
    before = replay_baseline.counts(conn)
    partitions = [row[0] for row in conn.execute(PARTITIONS, (doomed,)).fetchall()]
    conn.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY").format(names))
    for name in partitions if drop else ():
        conn.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(name)))
    after = replay_baseline.counts(conn)
    emptied = {f"public.{name}" for name in doomed}
    kept = {name: rows for name, rows in before.items() if name not in emptied}
    if changed := sorted(name for name in kept if after.get(name) != kept[name]):
        raise Refused(f"kept tables changed: {changed}")
    deleted = {name: before[name] - after[name] for name in sorted(emptied)}
    return {"deleted": deleted, "kept": kept, "partitions": partitions}


def dry_run_refusal(path: Path, database: str, deleted: dict | None = None) -> str | None:
    """None when the dry run is this database's, from the last hour (and, given the counts,
    deleted as many rows per table)."""
    if not path.exists():
        return f"no dry run ({path.name}): run without --apply first"
    found = json.loads(path.read_text())
    if found["database"] != database:
        return f"the dry run is of {found['database']!r}, not of {database!r}"
    if datetime.now(UTC) - datetime.fromisoformat(found["at"]) > FRESH:
        return f"the dry run is from {found['at']}, more than an hour ago"
    if deleted is not None and found["deleted"] != deleted:
        return f"rolled back: the counts differ from the dry run's {found['deleted']}"
    return None


def run(args) -> dict:
    dry = Path(args.out) / DRY_RUN
    with connect(args.database) as conn:
        go = guard(conn, args)
        if args.apply and (found := dry_run_refusal(dry, go["database"])):
            raise Refused(found)
        doomed = [row[0] for row in conn.execute(TABLES).fetchall() if row[0] not in KEEP]
        conn.commit()  # the cleaning transaction starts with its locks
        conn.isolation_level = IsolationLevel.REPEATABLE_READ
        try:
            result = clean(conn, doomed, args.apply)
        except (errors.LockNotAvailable, errors.DeadlockDetected) as exc:
            raise Refused(f"the tables are busy, nothing changed: {exc}") from None
        result = {"database": go["database"], "at": go["at"], "go": go} | result
        result |= {"keep": [*KEEP, *(f"{schema}.*" for schema in KEPT_SCHEMAS)]}
        if not args.apply:
            conn.rollback()
            Path(args.out).mkdir(mode=0o700, parents=True, exist_ok=True)
            write_private(dry, json.dumps(result, indent=2))
            return result | {"dry_run": True}
        if found := dry_run_refusal(dry, go["database"], result["deleted"]):
            conn.rollback()
            raise Refused(found)
        conn.commit()
    return result | {"dry_run": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", required=True, help="credentials TOML of the database")
    parser.add_argument("--out", required=True, help=f"the directory of {DRY_RUN}")
    parser.add_argument("--owner-go", help=f"{target.PRODUCTION_GO_FORM} or {GO_FORM}")
    parser.add_argument(target.ACKNOWLEDGE, action="store_true", help="the database is production")
    parser.add_argument("--apply", action="store_true", help="delete (after a dry run)")
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
