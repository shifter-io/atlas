#!/usr/bin/env python3
"""SCALE-GEN-1a: fill the scratch database proxy_scale with full-size synthetic data.

Usage:
  generate.py --database <proxy_scale.toml> --days A..B [--seed N] [--networks all|N|a,b]
              [--countries all|N|AD,AE] [--total-days 31] [--end-date YYYY-MM-DD] [--scale 1.0]
              [--formulas formulas]

Every network x country gets one daily test a day at its formula test hour and one pulse an
hour; README.md in this folder describes the data. Only synthetic data is written: no real IP,
proxy traffic or credential is used. The first call records the settings in the settings row
`scale.generator`; later calls continue with them and refuse different ones. Each day commits in
its own transaction, in order, so an interrupted step is resumed by running it again.

Guards: the database must be named proxy_scale, its storage schema must be this framework's, and
it must not hold any test_runs row this generator did not write (each of its runs carries the
summary key scale_generator).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from psycopg.types.json import Jsonb
from synthetic import Model, Settings
from writer import MARKER, Writer

from proxy_index import storage
from proxy_index.estimates.compute import load_formula
from proxy_index.regions import load_countries

DATABASE = "proxy_scale"
SETTING = "scale.generator"
FORMULAS = Path(__file__).resolve().parents[2] / "formulas"
SEQUENCES = (
    "ip_addresses.ip_id test_runs.run_key test_batches.batch_id dbip_releases.release_id "
    "products.product_id pool_stats.pool_id scope_stats.scope_id place_names.place_id "
    "asn_names.asn_name_id enrichment_locations.location_id enrichment_networks.network_id"
).split()


class Refused(Exception):
    """The database or the arguments may not be used; nothing was written."""


def guard(conn, expected: str = DATABASE) -> None:
    """Refuse any database but the scratch one, and one holding runs written by anything else."""
    name = conn.execute("SELECT current_database()").fetchone()[0]
    if name != expected:
        raise Refused(f"the database is {name!r}; this generator writes only to {expected!r}")
    if conn.execute("SELECT to_regclass('public.test_runs')").fetchone()[0] is None:
        return
    foreign = conn.execute(
        f"SELECT count(*) FROM test_runs WHERE NOT (summary ? '{MARKER}')"
    ).fetchone()[0]
    if foreign:
        raise Refused(f"the database holds {foreign} test runs this generator did not write")


def stored(conn) -> tuple[Settings, int] | None:
    """The recorded settings and the last generated day, if any."""
    if conn.execute("SELECT to_regclass('public.settings')").fetchone()[0] is None:
        return None
    row = conn.execute("SELECT value FROM settings WHERE key=%s", (SETTING,)).fetchone()
    return (Settings.from_json(row[0]), row[0]["days_done"]) if row else None


def record(conn, settings: Settings, done: int) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES (%s,%s) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",
        (SETTING, Jsonb(settings.to_json() | {"days_done": done})),
    )


def sequences(conn) -> None:
    """Move every identity past the explicit ids written, so later real ingests continue."""
    for name in SEQUENCES:
        table, column = name.split(".")
        conn.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}','{column}'),"
            f"coalesce(max({column}),1),max({column}) IS NOT NULL) FROM {table}"
        )


def parse_days(value: str) -> tuple[int, int]:
    first, _, last = value.partition("..")
    first, last = int(first), int(last or first)
    if not 1 <= first <= last:
        raise argparse.ArgumentTypeError("days are A..B with 1 <= A <= B")
    return first, last


def pick(value: str | None, available: list[str]) -> tuple[str, ...]:
    """all, the first N, or a comma-separated list (each must be available)."""
    if value in (None, "all"):
        return tuple(available)
    if value.isdigit():
        return tuple(available[: int(value)])
    chosen = tuple(value.split(","))
    if set(chosen) - set(available):
        raise Refused(f"unknown: {', '.join(sorted(set(chosen) - set(available)))}")
    return chosen


def inputs(args) -> tuple[dict, dict[str, str]]:
    """Every formula by network name, and the country names."""
    formulas = {}
    for path in sorted(Path(args.formulas).glob("*.json")):
        formula = load_formula(path.parent, path.stem)
        formulas[formula["network"]] = formula
    return formulas, load_countries()


def requested(args, formulas: dict, names: dict) -> Settings:
    end = date.fromisoformat(args.end_date) if args.end_date else datetime.now(UTC).date()
    return Settings(
        args.seed if args.seed is not None else 1,
        pick(args.networks, sorted(formulas)),
        pick(args.countries, sorted(names)),
        args.total_days or 31,
        end,
        args.scale if args.scale is not None else 1.0,
    )


def resolve(args, previous, formulas, names) -> tuple[Settings, int]:
    """The settings to use and the last day done; refuse a change of recorded settings."""
    if previous is None:
        return requested(args, formulas, names), 0
    settings, done = previous
    given = {
        "seed": args.seed,
        "networks": args.networks and pick(args.networks, sorted(formulas)),
        "countries": args.countries and pick(args.countries, sorted(names)),
        "days": args.total_days,
        "end_date": args.end_date and date.fromisoformat(args.end_date),
        "scale": args.scale,
    }
    changed = [k for k, v in given.items() if v is not None and v != getattr(settings, k)]
    if changed:
        raise Refused(f"the database was generated with other settings: {', '.join(changed)}")
    return settings, done


def generate(args, expected: str = DATABASE) -> dict:
    """Run one step (args.days); returns the settings and the days it generated."""
    formulas, names = inputs(args)
    first, last = args.days
    with storage.connect(args.database) as conn:
        conn.autocommit = True
        guard(conn, expected)
        settings, done = resolve(args, stored(conn), formulas, names)
        if last > settings.days:
            raise Refused(f"day {last} is past the {settings.days} days of this data set")
        if first > done + 1:
            raise Refused(f"day {done + 1} must be generated before day {first}")
        try:  # creates the storage schema, or refuses one that differs from this framework's
            storage.initialize(args.database)
        except ValueError as exc:
            raise Refused(str(exc)) from None
        if done == 0:
            storage.seed(args.database)
        regions = {
            code: (region, slug)
            for code, region, slug in conn.execute(
                "SELECT c.country_code,c.region_id,r.slug FROM countries c "
                "JOIN runner_regions r USING(region_id)"
            )
        }
        writer = Writer(conn, Model(settings, formulas, names), regions)
        generated = []
        for day in range(max(first, done + 1), last + 1):
            with conn.transaction():
                guard(conn, expected)
                if day == 1:
                    writer.setup()
                writer.day(day)
                sequences(conn)
                record(conn, settings, day)
            generated.append(day)
    return {"settings": settings.to_json(), "generated": generated, "days_done": max(done, last)}


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", required=True, help="credentials TOML of proxy_scale")
    parser.add_argument("--days", required=True, type=parse_days, help="A..B (1-based)")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--networks", help="all (default), N or a,b")
    parser.add_argument("--countries", help="all (default), N or AD,AE")
    parser.add_argument("--total-days", type=int, help="days in the data set (default 31)")
    parser.add_argument("--end-date", help="UTC date the last day ends at (default today)")
    parser.add_argument("--scale", type=float, help="multiplies every test's unique IPs")
    parser.add_argument("--formulas", default=str(FORMULAS))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = arguments(argv)
    try:
        result = generate(args)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
