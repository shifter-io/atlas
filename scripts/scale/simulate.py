#!/usr/bin/env python3
"""SIM-LOAD-1: load generated days into the staging database through the real ingest paths.

SIMULATE.md ("Loading the generated days") gives the usage, the guards, the hour, the
calculation and the report. The same command resumes from <out>/state.json. Exit 0 done, 2
refused (nothing written), 130 stopped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import cycle
import measure
import replay
import replay_baseline
import simulate_check
import simulate_model
import simulate_report
import simulate_steps
import target
from replay import Refused, now

from proxy_index import storage

SAME = ("database", "first_day", "days", "hours_per_day", "pulses", "profile_sha256")
# Each table of public and site with its partitions: its own bytes (with TOAST) and its indexes'.
BYTES = (
    "SELECT n.nspname||'.'||c.relname,sum(pg_table_size(coalesce(t.relid,c.oid)))::bigint,"
    "sum(pg_indexes_size(coalesce(t.relid,c.oid)))::bigint FROM pg_class c "
    "JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN LATERAL pg_partition_tree(c.oid) t "
    "ON true WHERE n.nspname IN ('public','site') AND c.relkind IN ('r','p') "
    "AND NOT c.relispartition GROUP BY 1"
)


def tables(conn) -> dict:
    """Rows (count(*)), table bytes and index bytes of every table, partitions included, and
    the database's size (measure.sizes)."""
    rows, found = replay_baseline.counts(conn), {}
    for name, table_bytes, index_bytes in conn.execute(BYTES).fetchall():
        found[name] = {"rows": rows.get(name, 0), "table_bytes": table_bytes}
        found[name]["index_bytes"] = index_bytes
    return {"at": now(), "database_bytes": measure.sizes(conn)["database_bytes"], "tables": found}


def read_plan(conn, args):
    """The plan, read in the connection's transaction, for the days and the extra hour."""
    found = (date.fromisoformat(args.first_day), args.formulas, args.browser_settings)
    return replace(simulate_model.read_plan(conn, *found, args.pulses), days=args.days + 1)


class Simulation(replay.Replay):
    """The load of the generated days: replay.py's state, guards, step loop, statement windows
    and calculation child, with SimSteps' hour and the calculations at the end."""

    def __init__(self, conn, args, profile: dict):
        self.conn, self.args, self.out, self.profile = conn, args, Path(args.out), profile
        # replay.guard's last hour is the extra one after the days: one more day of one hour.
        ended = SimpleNamespace(**vars(args) | {"days": args.days + 1, "hours_per_day": 1})
        go = replay.guard(conn, ended)
        self.plan = read_plan(conn, args)
        self.out.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state = self.open(go)
        self.model = simulate_model.Model(profile, self.plan, self.state["seed"])
        self.model.summaries.replay = self.state["replay_id"]  # every run's marker: the run's id
        self.steps = simulate_steps.SimSteps(conn, args, self.state, self.model)
        self.names = ("daily", "browser", *(("pulses",) if args.pulses else ()), "release")
        self.stop, self.pool, self.counters = Event(), ThreadPoolExecutor(1), None

    def open(self, go: dict) -> dict:
        """The state of state.json, or a new one with its baseline. Its days count the extra
        hour's, so wipe_replay.py removes everything the load wrote."""
        path, args = self.out / "state.json", self.args
        wanted = {"database": go["database"], "first_day": args.first_day, "days": args.days + 1}
        wanted |= {"hours_per_day": args.hours_per_day, "pulses": args.pulses}
        wanted["profile_sha256"] = hashlib.sha256(Path(args.profile).read_bytes()).hexdigest()
        if path.exists():
            state = json.loads(path.read_text())
            if {key: state.get(key) for key in SAME} != wanted:
                raise Refused(f"{path.name} is the load of {[state.get(key) for key in SAME]}")
            state["resumed"].append(go)
            return state
        replay_id = uuid.uuid4().hex
        state = {"replay_id": replay_id, "seed": args.seed or replay_id, "go": go, **wanted}
        state |= {"started_at": go["at"], "networks": sorted(self.plan.formulas)}
        state |= {"baseline": replay_baseline.baseline(self.conn), "nodes": [], "hours": []}
        state["baseline"]["sizes"] = tables(self.conn)
        state |= {"sizes": {}, "resumed": [], "counts": {"retries": 0, "pause_seconds": 0}}
        self.state = state
        self.save()
        return state

    def run(self) -> dict:
        """Load every hour not stored yet, calculate, check; disable the nodes however it ends."""
        total = self.total()  # the simulated hours; index total is the extra hour
        records = [self.record(index) for index in range(total + 1)]  # the model's (day, hour):
        slots = [(r["day"] + 1, datetime.fromisoformat(r["start"]).hour) for r in records]
        first = next((i for i, r in enumerate(records) if not r["ingest"]["done"]), total + 1)
        given = (self.profile, self.plan, self.state["seed"], self.state["replay_id"])
        producer = simulate_steps.Producer(self.args.jobs, self.out / "generated", given, slots)
        if measure.has_statements(self.conn):
            self.counters = measure.snapshot(self.conn)
        try:
            for index in range(first, total + 1):
                day, hour = divmod(index, self.args.hours_per_day)
                if index and not hour and str(day) not in self.state["sizes"]:  # a day ended
                    self.state["sizes"][str(day)] = tables(self.conn)
                if index == total:
                    self.calculated(total - 1, incremental=False)
                self.load(index, producer)
            self.calculated(total, incremental=True)
            self.checks(producer)
        finally:
            self.stop.set()  # a calculation still running is killed
            self.pool.shutdown()
            producer.close()
            self.conn.execute(replay.DISABLE, (self.state["nodes"],))
            self.save()
            try:  # a stopped load has its report too
                simulate_report.write(self.out, self.state, self.profile)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                print(f"simulate: no report: {type(exc).__name__}: {exc}", file=sys.stderr)
        hours = sum(self.record(index)["ingest"]["done"] for index in range(total + 1))
        return {"replay_id": self.state["replay_id"], "hours_done": hours, "hours": total + 1}

    def load(self, index: int, producer) -> None:
        """Store one generated hour with the replay's step loop (Replay.ingest)."""
        record, hour = self.record(index), self.model.hour(*producer.hours[index])
        figures = producer.take(index)
        runs = (hour.daily, hour.browser, hour.pulses)
        record["runs"] = {
            kind: len(own) for kind, own in zip(simulate_report.KINDS, runs, strict=True)
        }
        record["figures"] = simulate_report.figures(hour, figures)
        self.steps.current = simulate_steps.Current(hour, figures, producer.directory(index))
        self.ingest(index)
        producer.done(index)
        self.mark(index, {"ingest": [index], "calculate": []})
        self.save()

    def calculated(self, index: int, incremental: bool) -> None:
        """The hour's calculation (full, or incremental), retried until it succeeds; then the
        wait for siteapi and, after the last, the sizes."""
        work = self.record(index)["calculate"]
        if work["done"]:
            return
        work["kind"] = "incremental" if incremental else "full"
        self.settle(index, self.calculate(index, incremental), incremental)
        if self.args.siteapi:
            work["siteapi"] = self.steps.served(work["build_id"])
        work |= {"done": True, "finished": now()}
        self.mark(index, {"calculate": [index], "ingest": []})
        if incremental:
            self.state["sizes"]["final"] = tables(self.conn)
        self.save()

    def checks(self, producer) -> None:
        """simulate_check.size over the stored days and .stats of day 2 (day 1 alone: its own)."""
        if "checks" in self.state:
            return
        day = min(2, self.args.days)
        found = producer.pool.submit(simulate_steps.stats, day, self.args.hours_per_day)
        hours = simulate_report.stored(self.model, self.state["hours"][: self.total()])
        size = simulate_check.size(self.profile, hours)
        self.state |= {"checks": {"size": size, "stats": found.result()}, "stats_day": day}
        self.save()


def plan_only(args, profile: dict) -> dict:
    """The runs, rows and growth each simulated day would add, read in one read-only
    transaction (simulate_report.plan)."""
    with storage.connect(args.database) as conn:
        conn.read_only = True
        with conn.transaction():
            plan = read_plan(conn, args)
    model = simulate_model.Model(profile, plan, "plan-only")
    return simulate_report.plan(model, profile, args.days, args.hours_per_day, args.pulses)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", required=True, help="credentials TOML of the database")
    parser.add_argument("--owner-go", help=f"the owner's go: {target.GO_FORM}")
    parser.add_argument("--profile", required=True, help="the profile (SIM-PROFILE-1)")
    parser.add_argument("--days", required=True, type=int, help="simulated days")
    parser.add_argument("--first-day", required=True, help="date of simulated day 1 (UTC)")
    parser.add_argument("--out", help="directory of the state, the report and the hours")
    parser.add_argument("--jobs", type=int, default=6, help="worker processes generating hours")
    parser.add_argument("--batch-daily", type=int, default=20, help="daily tests a transaction")
    parser.add_argument("--pulses", action="store_true", help="add HTTP pulses (off: none)")
    parser.add_argument("--siteapi", help="base URL of a siteapi reading the database")
    parser.add_argument("--siteapi-timeout", type=float, default=600.0)
    parser.add_argument("--no-scope", action="store_true", help="no systemd memory scope")
    parser.add_argument("--plan-only", action="store_true", help="print the plan; write nothing")
    parser.add_argument("--methodology", default=str(cycle.METHODOLOGY))
    parser.add_argument("--formulas", default=str(simulate_model.FORMULAS))
    parser.add_argument("--browser-settings", default=str(simulate_model.BROWSER_SETTINGS))
    # For tests and smoke runs: hours per simulated day, the first retry's pause, the model's
    # seed (default: the run's id) and a calculation's time limit in seconds.
    parser.add_argument("--hours-per-day", type=int, default=24, help=argparse.SUPPRESS)
    parser.add_argument("--pause", type=float, default=300.0, help=argparse.SUPPRESS)
    parser.add_argument("--seed", help=argparse.SUPPRESS)
    parser.add_argument("--hour-timeout", type=float, default=86400.0, help=argparse.SUPPRESS)
    parser.set_defaults(pulse_concurrency=8, node_api=None, siteapi_pid=None)
    args = parser.parse_args(argv)
    if min(args.days, args.jobs, args.batch_daily) < 1 or not 1 <= args.hours_per_day <= 24:
        parser.error("--days, --jobs and --batch-daily are at least 1, the hours of a day 1..24")
    if not args.plan_only and not (args.owner_go and args.out):
        parser.error("--owner-go and --out are required, except with --plan-only")
    return args


def main(argv=None) -> int:
    args = arguments(argv)
    signal.signal(signal.SIGTERM, signal.default_int_handler)  # a stop, as Ctrl-C
    profile = json.loads(Path(args.profile).read_text())
    if args.plan_only:
        print(simulate_report.plan_table(plan_only(args, profile)), end="")
        return 0
    try:
        with storage.connect(args.database) as conn:
            conn.autocommit = True
            result = Simulation(conn, args, profile).run()
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("stopped: the same command continues the load", file=sys.stderr)
        return 130
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
