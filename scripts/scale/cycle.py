#!/usr/bin/env python3
"""SCALE-GEN-1b: run simulated hours of the real hourly cycle on the synthetic data set.

Usage:
  cycle.py --database <proxy_scale.toml> --hours N [--out DIR] [--max-minutes 25]
           [--first-hour H] [--pulse-rate R] [--pulse-concurrency C] [--node-api URL]
           [--siteapi URL] [--siteapi-pid PID] [--siteapi-timeout S] [--methodology PATH]

generate.py writes days 1..D-1 of a D-day data set; the cycle drives day D (and later days the
set has) hour by hour from --first-hour. Each hour: ingest its daily tests (storage.ingest, then
their estimated hours), plan its pulse slot and upload every pulse run to the node API
(POST /v1/results), release the ended estimated hours, `proxy-index calculate --publish` in a
child process, and wait for siteapi (--siteapi) to serve the new build; cycle_steps.py holds the
steps. README.md describes them, the measurements (measure.py) and the report. Progress is kept
in the settings row scale.cycle: past --max-minutes a run stops between steps (or inside the
ingest and pulse steps), and the same command continues it until N hours are done.

Guards: the database must be named proxy_scale, or the session's only schema must be proxy_scale
(target.py); it must hold a whole-day generator data set, and no test_runs row written by anything
but the generator and this driver (summary key scale_cycle).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import generate
import measure
import target
import writer
from cycle_steps import HOUR, MARKER, Failed, Refused, Steps
from psycopg.types.json import Jsonb
from synthetic import Model
from writer import TARGET, Writer

from proxy_index import storage
from proxy_index.calculated import store
from proxy_index.stats import LOCATION_FIELDS, NETWORK_FIELDS

DATABASE, SETTING = "proxy_scale", "scale.cycle"
STEPS = ("ingest", "pulses", "release", "calculate", "siteapi")
METHODOLOGY = Path(__file__).resolve().parents[2] / "configs" / "methodology.toml"
# The generator's pulse schedule: generic_http hourly in the data set's countries, nothing else.
SCHEDULE = (
    "UPDATE targets SET pulse_enabled=(target_id=%(target)s),pulse_interval_minutes=CASE "
    "WHEN target_id=%(target)s THEN 60 ELSE pulse_interval_minutes END",
    "UPDATE countries SET enabled=(country_code=ANY(%(countries)s))",
    "INSERT INTO settings(key,value) VALUES ('load.enabled','false') "
    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",
)
LISTS = (
    "SELECT l.location_id,p.country_code,p.country,p.region_code,p.region,p.city,l.latitude,"
    "l.longitude FROM enrichment_locations l JOIN place_names p USING(place_id) "
    "WHERE p.country_code IS NOT NULL",
    "SELECT n.network_id,n.isp,n.organization,a.asn,a.name,n.connection_type,n.usage_type "
    "FROM enrichment_networks n JOIN asn_names a USING(asn_name_id) WHERE a.asn IS NOT NULL",
)


def guard(conn, expected: str = DATABASE):
    """The generator's settings and progress; refuse another database or foreign runs."""
    found = target.refusal(conn, expected)
    if found:
        raise Refused(f"{found}; this driver uses only {expected!r} {target.SCHEMA_RULE}")
    found = generate.stored(conn)
    if found is None:
        raise Refused("the database holds no synthetic data set; run generate.py first")
    foreign = conn.execute(
        "SELECT count(*) FROM test_runs WHERE NOT (summary ? %s OR summary ? %s)",
        (writer.MARKER, MARKER),
    ).fetchone()[0]
    if foreign:
        raise Refused(f"the database holds {foreign} test runs neither generator wrote")
    return found


def reduce(name: str, parts: list[dict]) -> dict:
    """A step's parts (one per resumed run) as one measurement."""
    seconds = round(sum(part["seconds"] for part in parts), 3)
    result = {"seconds": seconds, "parts": len(parts)}
    result["statements"] = measure.merge(part["statements"] for part in parts)
    if name == "ingest":
        timings = [value for part in parts for value in part["per_test"]]
        rows = sum(part["observation_rows"] for part in parts)
        result |= {"tests": len(timings), "observation_rows": rows}
        return result | {"per_test": measure.percentiles(timings)}
    if name == "pulses":
        latencies = [value for part in parts for value in part["latencies"]]
        result |= {"planned": parts[-1]["planned"], "uploads": len(latencies)}
        result["statuses"] = parts[-1]["statuses"]
        result["failed"] = sum(part["failed"] for part in parts)
        result["failures"] = [text for part in parts for text in part["failures"]][:20]
        result["rate"] = round(len(latencies) / seconds, 3) if seconds else None
        return result | {"latency": measure.percentiles(latencies)}
    return {key: value for key, value in parts[-1].items() if key not in result} | result


class Cycle(Steps):
    """The driver of one database: its state, the model and the five steps."""

    def __init__(self, conn, args, expected: str = DATABASE):
        self.conn, self.args = conn, args
        self.deadline = time.monotonic() + args.max_minutes * 60
        settings, progress = guard(conn, expected)
        formulas, names = generate.load(args.formulas)
        self.model = Model(settings, formulas, names)
        regions = conn.execute(
            "SELECT c.country_code,c.region_id,r.slug FROM countries c "
            "JOIN runner_regions r USING(region_id)"
        )
        self.writer = Writer(conn, self.model, {c: (r, slug) for c, r, slug in regions})
        self.positions = {code: c for c, code in enumerate(settings.countries)}
        self.state = self.load(progress)
        self.pg_stat = measure.has_statements(conn)
        self.locations, self.networks = (
            {row[0]: dict(zip(fields, row[1:], strict=True)) for row in conn.execute(query)}
            for query, fields in zip(LISTS, (LOCATION_FIELDS, NETWORK_FIELDS), strict=True)
        )
        rate = len(self.model.lanes) / 3600 if args.pulse_rate is None else args.pulse_rate
        self.rate = rate or None

    def load(self, progress: dict) -> dict:
        """The stored state; on the first run, check the data set and set up the database."""
        row = self.conn.execute("SELECT value FROM settings WHERE key=%s", (SETTING,)).fetchone()
        if row:
            return row[0]
        first = progress["days_done"] + 1
        if progress["prepared"] not in (None, progress["days_done"]):
            raise Refused(f"day {progress['prepared']} is half generated; finish generate.py")
        if first > self.model.settings.days:
            raise Refused("every day of the data set is generated; the cycle needs a later one")
        if not 0 <= self.args.first_hour < 24:
            raise Refused("--first-hour is 0..23")
        self.state = {"first_day": first, "first_hour": self.args.first_hour}
        self.state |= {"hours": [], "current": None}
        for statement in SCHEDULE:
            self.conn.execute(statement, {"target": TARGET, "countries": list(self.positions)})
        store.initialize(self.args.database)
        self.save()
        return self.state

    def save(self) -> None:
        self.conn.execute(
            "INSERT INTO settings(key,value) VALUES (%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",
            (SETTING, Jsonb(self.state)),
        )

    def late(self) -> bool:
        return time.monotonic() >= self.deadline

    def bounds(self, index: int) -> tuple[int, datetime]:
        """The day and start of the cycle's hour `index`; refuse one not available."""
        offset = self.state["first_hour"] + index
        day = self.state["first_day"] + offset // 24
        if day > self.model.settings.days:
            raise Refused(f"the data set ends before day {day}; generate a longer one")
        start = self.model.settings.day_start(day) + timedelta(hours=offset % 24)
        if start + HOUR > datetime.now(UTC):
            raise Refused(f"the hour from {start.isoformat()} has not ended yet")
        return day, start

    def hour(self) -> dict | None:
        """Run (or continue) the next hour; its record, or None when the budget ran out."""
        index = len(self.state["hours"])
        day, start = self.bounds(index)
        current = self.state["current"] = self.state["current"] or {"index": index, "steps": {}}
        for name in STEPS:
            step = current["steps"].setdefault(name, {"done": False, "parts": []})
            if step["done"]:
                continue
            if self.late():
                return None
            part, step["done"] = self.measured(name, day, start, step)
            step["parts"].append(part)
            self.save()
            if not step["done"]:
                return None
        steps = {name: reduce(name, current["steps"][name]["parts"]) for name in STEPS}
        record = {"index": index, "day": day, "start": start.isoformat(), "steps": steps}
        record["sizes"] = measure.sizes(self.conn)
        self.state["hours"].append(record)
        self.state["current"] = None
        self.save()
        return record

    def measured(self, name: str, day: int, start: datetime, step: dict) -> tuple[dict, bool]:
        """Run one step (or a part of it) with its time and statement timings."""
        before = measure.snapshot(self.conn) if self.pg_stat else None
        tracer, began = measure.Tracer(), time.monotonic()
        with contextlib.nullcontext() if self.pg_stat else tracer:
            part, done = getattr(self, name)(day, start, step)
        part["seconds"] = round(time.monotonic() - began, 3)
        if before is not None:
            part["statements"] = measure.delta(before, measure.snapshot(self.conn))
        elif "statements" not in part:
            part["statements"] = tracer.report()
        return part, done

    def report(self) -> dict:
        current, state = self.state["current"], self.state
        if current:
            done = [name for name, step in current["steps"].items() if step["done"]]
            current = {"index": current["index"], "steps_done": done}
        database, schema, _ = self.conn.execute(target.STATE).fetchone()
        found = {"database": database, "schema": schema}
        found |= {"settings": self.model.settings.to_json(), "current": current}
        found |= {key: state[key] for key in ("first_day", "first_hour", "hours")}
        found["statements_from"] = "pg_stat_statements" if self.pg_stat else "the driver"
        found["pulse_rate"] = round(self.rate, 4) if self.rate else "unpaced"
        return found | {"written_at": datetime.now(UTC).isoformat(timespec="seconds")}


def run(args, expected: str = DATABASE) -> dict:
    """Run or continue the cycle until args.hours hours are done or the budget runs out."""
    with storage.connect(args.database) as conn:
        conn.autocommit = True
        cycle, finished = Cycle(conn, args, expected), []
        try:
            while len(cycle.state["hours"]) < args.hours and (record := cycle.hour()):
                finished.append(record["start"])
        finally:
            path = measure.write_report(args.out, cycle.report())
    done = len(cycle.state["hours"])
    result = {"finished": finished, "hours_done": done, "complete": done >= args.hours}
    return result | {"report": str(path)}


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", required=True, help="credentials TOML of proxy_scale")
    parser.add_argument("--hours", required=True, type=int, help="hours the cycle runs in all")
    parser.add_argument("--out", default="scale-report", help="directory of the reports")
    parser.add_argument("--max-minutes", type=float, default=25.0, help="budget of this run")
    parser.add_argument("--first-hour", type=int, default=0, help="UTC hour the cycle starts at")
    parser.add_argument("--pulse-rate", type=float, help="uploads a second (0: unpaced)")
    parser.add_argument("--pulse-concurrency", type=int, default=32)
    parser.add_argument(
        "--node-api",
        help="node API of an orchestrator on this database run WITHOUT its scheduler "
        "(its reaper would fail the past hour's runs); default: in-process, loopback",
    )
    parser.add_argument("--siteapi", help="base URL of a siteapi serving this database")
    parser.add_argument("--siteapi-pid", type=int, help="its process id, for its peak RSS")
    parser.add_argument("--siteapi-timeout", type=float, default=600.0)
    parser.add_argument("--methodology", default=str(METHODOLOGY))
    parser.add_argument("--formulas", default=str(generate.FORMULAS))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = arguments(argv)
    try:
        result = run(args)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Failed as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
