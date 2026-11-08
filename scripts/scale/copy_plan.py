#!/usr/bin/env python3
"""REPLAY-FIX-2: copy production's scheduling inputs into staging, so the replay plans
production's real volume (REPLAY.md, "Before the run").

Usage:
  copy_plan.py --from <production toml> --to <staging toml>

It reads, in one read-only transaction of --from: the countries (enabled, census_time, region),
the schedule fields of the targets (SCHEDULE) and the settings rows scheduler.*, census.*,
load.* and pulse.*. It writes them, in one transaction, to --to, which must be the staging
database (target.STAGING); any other database is refused. Staging then plans what production
plans: a country or target production does not have is switched off there, and a settings row
of those groups production does not have is deleted (production uses its default). A country
or target production schedules and staging lacks is refused (seed staging first).

It prints every row it changed as a JSON line ({table, key, before, after}; None: no row).
A second run changes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys

import target
from psycopg import IsolationLevel
from psycopg.types.json import Jsonb

from proxy_index.storage import connect

SCHEDULE = (
    "census",
    "census_concurrency",
    "pulse_enabled",
    "pulse_interval_minutes",
    "pulse_requests",
    "pulse_concurrency",
)
GROUPS = ["scheduler", "census", "load", "pulse"]
QUERIES = {
    "countries": "SELECT trim(c.country_code),c.enabled,c.census_time,r.slug AS region "
    "FROM countries c JOIN runner_regions r USING(region_id) ORDER BY 1",
    "targets": f"SELECT target_id,{','.join(SCHEDULE)} FROM targets ORDER BY 1",
    "settings": "SELECT key,value FROM settings WHERE split_part(key,'.',1)=ANY(%s) ORDER BY 1",
}
# What a row production does not have becomes in staging: not scheduled.
OFF = {"countries": {"enabled": False}, "targets": {"census": False, "pulse_enabled": False}}
REGIONS = "SELECT slug FROM runner_regions"
COUNTRY = (
    "UPDATE countries SET enabled=%(enabled)s,census_time=%(census_time)s,region_id="
    "(SELECT region_id FROM runner_regions WHERE slug=%(region)s) WHERE country_code=%(key)s"
)
TARGET = f"UPDATE targets SET {','.join(f'{f}=%({f})s' for f in SCHEDULE)} WHERE target_id=%(key)s"
UPDATES = {"countries": COUNTRY, "targets": TARGET}
PUT = (
    "INSERT INTO settings(key,value) VALUES (%s,%s) "
    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()"
)


class Refused(Exception):
    """The target is not staging, or staging lacks what production schedules; nothing written."""


def read(conn) -> dict[str, dict[str, dict]]:
    """table -> key -> its scheduling fields (a settings row: {"value": ...})."""
    found = {}
    for table, statement in QUERIES.items():
        cursor = conn.execute(statement, (GROUPS,) if table == "settings" else None)
        names = [column.name for column in cursor.description[1:]]
        found[table] = {row[0]: dict(zip(names, row[1:], strict=True)) for row in cursor}
    return found


def production(path) -> dict[str, dict[str, dict]]:
    """The scheduling inputs of --from, in one read-only transaction."""
    with connect(path) as conn:
        conn.read_only, conn.isolation_level = True, IsolationLevel.REPEATABLE_READ
        with conn.transaction():
            return read(conn)


def changed(table: str, source: dict, mine: dict) -> tuple[list[dict], list[str]]:
    """A table's changes in staging (`mine`), and the scheduled rows staging lacks."""
    found, absent = [], []
    for key in sorted(set(source) | set(mine)):
        before, after = mine.get(key), source.get(key)
        if table in OFF and after is None:  # production does not schedule it
            after = before | OFF[table]
        elif table in OFF and before is None:
            absent += [f"{table} {key}"] if any(after[f] for f in OFF[table]) else []
            continue
        if before != after:
            found.append({"table": table, "key": key, "before": before, "after": after})
    return found, absent


def changes(source: dict, mine: dict) -> tuple[list[dict], list[str]]:
    """The rows staging (`mine`) changes to plan as production (`source`) does, and what
    production schedules that staging lacks."""
    found, absent = [], []
    for table in QUERIES:
        rows, lacking = changed(table, source[table], mine[table])
        found, absent = found + rows, absent + lacking
    return found, absent


def apply(conn, found: list[dict]) -> None:
    for change in found:
        after, key, table = change["after"], change["key"], change["table"]
        if table in UPDATES:
            conn.execute(UPDATES[table], after | {"key": key})
        elif after is None:
            conn.execute("DELETE FROM settings WHERE key=%s", (key,))
        else:
            conn.execute(PUT, (key, Jsonb(after["value"])))


def copy(source: dict, path) -> list[dict]:
    """Write `source` into the staging database at path, in one transaction; the changes."""
    with connect(path) as conn, conn.transaction():
        database = conn.execute("SELECT current_database()").fetchone()[0]
        if database != target.STAGING:
            raise Refused(f"copy_plan writes only to {target.STAGING!r}, not {database!r}")
        found, absent = changes(source, read(conn))
        regions = {row[0] for row in conn.execute(REGIONS)}
        wanted = {row["region"] for row in source["countries"].values()}
        absent += [f"region {region}" for region in sorted(wanted - regions)]
        if absent:
            raise Refused(f"staging lacks what production schedules: {', '.join(absent[:20])}")
        apply(conn, found)
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from", dest="source", required=True, help="production's TOML")
    parser.add_argument("--to", required=True, help="staging's TOML")
    args = parser.parse_args(argv)
    try:
        found = copy(production(args.source), args.to)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    for change in found:
        print(json.dumps(change, default=str))
    print(f"{len(found)} rows changed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
