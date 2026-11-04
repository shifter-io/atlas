#!/usr/bin/env python3
"""SCALE-GEN-1a: fill the scratch database proxy_scale with full-size synthetic data.

Usage:
  generate.py --database <proxy_scale.toml> --days A..B [--seed N] [--networks all|N|a,b]
              [--countries all|N|AD,AE] [--total-days 31] [--end-date YYYY-MM-DD] [--scale 1.0]
              [--formulas formulas]

Every network x country gets one daily test a day at its formula test hour and one pulse an
hour; README.md in this folder describes the data. Only synthetic data is written: no real IP,
proxy traffic or credential is used. The first call records the settings in the settings row
`scale.generator`; later calls continue with them and refuse different ones. Days go in order. A
day's shared rows (partitions, DB-IP release, pulse batches) commit first, then each network's
rows in its own transaction, --jobs networks at a time in worker processes; the settings row
records each finished network, so an interrupted step is resumed by running it again.

Guards: the database must be named proxy_scale, or the session's only schema must be proxy_scale
(target.py); its storage schema must be this framework's, and it must not hold any test_runs row
this generator did not write (each of its runs carries the summary key scale_generator).
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path

from psycopg.types.json import Jsonb
import target
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
    """Refuse any target but the scratch one (target.py), and one holding others' runs."""
    found = target.refusal(conn, expected)
    if found:
        raise Refused(f"{found}; this generator writes only to {expected!r} {target.ALLOWED}")
    if conn.execute("SELECT to_regclass('test_runs')").fetchone()[0] is None:
        return
    foreign = conn.execute(
        f"SELECT count(*) FROM test_runs WHERE NOT (summary ? '{MARKER}')"
    ).fetchone()[0]
    if foreign:
        raise Refused(f"the database holds {foreign} test runs this generator did not write")


def stored(conn) -> tuple[Settings, dict] | None:
    """The recorded settings and progress (days_done, prepared day, its finished networks)."""
    if conn.execute("SELECT to_regclass('settings')").fetchone()[0] is None:
        return None
    row = conn.execute("SELECT value FROM settings WHERE key=%s", (SETTING,)).fetchone()
    return (Settings.from_json(row[0]), row[0]) if row else None


def record(conn, settings: Settings, done: int, prepared: int | None = None) -> None:
    progress = {"days_done": done, "prepared": prepared, "parts": []}
    conn.execute(
        "INSERT INTO settings(key,value) VALUES (%s,%s) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",
        (SETTING, Jsonb(settings.to_json() | progress)),
    )


def part(database: str, formulas: str, settings: dict, regions: dict, step: tuple) -> None:
    """Write one network's day (step: day, network) and record it, in one transaction."""
    day, network = step
    found, names = load(formulas)
    with storage.connect(database) as conn:
        conn.autocommit = True
        writer = Writer(conn, Model(Settings.from_json(settings), found, names), regions)
        with conn.transaction():
            writer.network(day, network)
            conn.execute(
                "UPDATE settings SET value=jsonb_set(value,'{parts}',(value->'parts')||%s) "
                "WHERE key=%s",
                (Jsonb([network]), SETTING),
            )


def parts(args, settings: Settings, regions: dict, day: int, todo: list[int]) -> None:
    """The networks of a day not yet written, --jobs at a time (in this process for 1)."""
    work = [(args.database, args.formulas, settings.to_json(), regions, (day, n)) for n in todo]
    if args.jobs == 1:
        for item in work:
            part(*item)
        return
    # Spawned workers never inherit this process's database connection.
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(min(args.jobs, len(work)) or 1, mp_context=context) as pool:
        for future in [pool.submit(part, *item) for item in work]:
            future.result()


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


def load(directory: str) -> tuple[dict, dict[str, str]]:
    """Every formula by network name, and the country names."""
    formulas = {}
    for path in sorted(Path(directory).glob("*.json")):
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


def resolve(args, previous, formulas, names) -> tuple[Settings, dict]:
    """The settings to use and the progress so far; refuse a change of recorded settings."""
    if previous is None:
        return requested(args, formulas, names), {"days_done": 0, "prepared": None, "parts": []}
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


def one_day(conn, writer: Writer, args, progress: dict, day: int, expected: str) -> dict:
    """Prepare the day once, write its networks not yet done, then close it; the new progress."""
    settings = writer.settings
    if progress["prepared"] != day:
        with conn.transaction():
            guard(conn, expected)
            if day == 1:
                writer.setup()
            writer.prepare(day)
            record(conn, settings, day - 1, day)
        progress = stored(conn)[1]
    todo = [n for n in range(len(settings.networks)) if n not in progress["parts"]]
    parts(args, settings, writer.regions, day, todo)
    with conn.transaction():
        sequences(conn)
        record(conn, settings, day)
    return stored(conn)[1]


def generate(args, expected: str = DATABASE) -> dict:
    """Run one step (args.days); returns the settings and the days it finished."""
    formulas, names = load(args.formulas)
    first, last = args.days
    with storage.connect(args.database) as conn:
        conn.autocommit = True
        guard(conn, expected)
        settings, progress = resolve(args, stored(conn), formulas, names)
        done = progress["days_done"]
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
            progress = one_day(conn, writer, args, progress, day, expected)
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
    parser.add_argument("--jobs", type=int, default=4, help="networks written at once")
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
