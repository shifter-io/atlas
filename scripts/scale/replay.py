#!/usr/bin/env python3
"""REPLAY-1: replay simulated days of real-data hours back to back, in the staging database.

Usage:
  replay.py --database <toml> --owner-go "owner-go REPLAY-RUN-1 <YYYY-MM-DD>" --slices <dir>
            --days 7 --first-day <YYYY-MM-DD> --out <dir> [--hour-timeout 3000]
            [--siteapi URL] [--siteapi-timeout S] [--no-scope]

REPLAY.md describes the steps of an hour, the guards, the order, the retries and the outputs.
"""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from threading import Event

import cycle
import generate
import measure
import replay_baseline
import replay_report
import target
from cycle_steps import HOUR, MEASURE
from replay_report import private, write_private
from replay_steps import ERRORS, ReplaySteps, networks

from proxy_index import storage

INGEST = ("daily", "pulses", "browser", "release")
FLAG_AFTER = 3  # retries of an hour before stderr flags it for the operator
LIVE = "SELECT count(*) FROM job_runs WHERE started_at>now()-interval '5 minutes'"
SIZES = (
    "SELECT pg_database_size(current_database()),(SELECT sum(pg_database_size(datname))::bigint "
    "FROM pg_database WHERE datallowconn AND has_database_privilege(datname,'CONNECT'))"
)
# A disabled node keeps its row (its runs reference it) but no token can match its hash.
DISABLE = (
    "UPDATE nodes SET status='offline',token_hash='disabled:'||node_id||':'||md5(random()::text) "
    "WHERE node_id=ANY(%s)"
)
SCOPE = ["systemd-run", "--scope", "--quiet", "-p", "MemoryMax=10G", "-p", "MemorySwapMax=0"]
SAME = ("database", "first_day", "days", "hours_per_day")


class Refused(Exception):
    """The database, the go or the days may not be used; nothing was written."""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def memory_scope(enabled: bool = True) -> list[str]:
    """The systemd scope that caps the calculation, where systemd-run exists."""
    return [*SCOPE, "--"] if enabled and shutil.which("systemd-run") else []


def guard(conn, args) -> dict:
    """The accepted go's record; refuse another database, a live scheduler or unended days."""
    found = target.refusal(conn, owner_go=args.owner_go)
    if found:
        raise Refused(found)
    if conn.execute(LIVE).fetchone()[0]:
        found = "a job_runs row started in the last 5 minutes"
        raise Refused(f"{found}: an orchestrator runs against this database")
    last = first_start(args) + timedelta(days=args.days - 1, hours=args.hours_per_day)
    if last > datetime.now(UTC):
        raise Refused(f"the last simulated hour ends at {last.isoformat()}, not before now")
    database = conn.execute("SELECT current_database()").fetchone()[0]
    return {"go": args.owner_go, "database": database, "at": now()}


def first_start(args) -> datetime:
    return datetime.combine(date.fromisoformat(args.first_day), clock(), UTC)


class Replay:
    """The replay of one database: its state, its steps and the calculation pipeline."""

    def __init__(self, conn, args):
        self.conn, self.args, self.out = conn, args, Path(args.out)
        self.out.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state = self.open(guard(conn, args))
        self.steps = ReplaySteps(conn, args, self.state)
        self.stop, self.pool = Event(), ThreadPoolExecutor(1)

    def open(self, go: dict) -> dict:
        """The state of state.json, or a new one with its baseline."""
        path, args = self.out / "state.json", self.args
        wanted = {key: getattr(args, key) for key in SAME[1:]} | {"database": go["database"]}
        if path.exists():
            state = json.loads(path.read_text())
            if {key: state[key] for key in SAME} != wanted:
                raise Refused(f"{path} is the replay of {[state[key] for key in SAME]}")
            state["resumed"].append(go)
            return state
        state = {"replay_id": uuid.uuid4().hex, "go": go, "started_at": go["at"], **wanted}
        slices = Path(args.slices)
        state |= {"slices": str(slices.resolve()), "networks": networks(slices)}
        state |= {"baseline": replay_baseline.baseline(self.conn), "nodes": [], "hours": []}
        state["baseline"]["sizes"] = self.sizes()
        state |= {"days_counted": {}, "resumed": [], "counts": {"retries": 0, "pause_seconds": 0}}
        self.state = state
        self.save()
        return state

    def save(self) -> None:
        write_private(self.out / "state.json", json.dumps(self.state, default=str))

    def sizes(self) -> dict:
        database, every = self.conn.execute(SIZES).fetchone()
        return {"database_bytes": database, "all_databases_bytes": every}

    def total(self) -> int:
        return self.args.days * self.args.hours_per_day

    def record(self, index: int) -> dict:
        hours = self.state["hours"]
        while len(hours) <= index:
            day, hour = divmod(len(hours), self.args.hours_per_day)
            start = first_start(self.args) + timedelta(days=day, hours=hour)
            hours.append({"index": len(hours), "day": day, "start": start.isoformat()})
            hours[-1] |= {"ingest": {"done": False, "steps": {}}, "retries": 0}
            hours[-1]["calculate"] = {"done": False, "attempts": []}
        return hours[index]

    def run(self) -> dict:
        """Replay every hour not calculated yet; disable the replay's nodes however it ends."""
        done = [r["calculate"]["done"] for r in self.state["hours"]]
        first = done.index(False) if False in done else len(done)
        pending = None
        try:
            for index in range(first, self.total()):
                self.ingest(index)
                if pending:
                    self.finish(*pending)
                pending = (index, self.calculate(index))
            if pending:
                self.finish(*pending)
                pending = None
        finally:
            if pending:
                self.stop.set()
                pending[1].result()
            self.conn.execute(DISABLE, (self.state["nodes"],))
            self.save()
            self.pool.shutdown()
        hours = sum(record["calculate"]["done"] for record in self.state["hours"])
        return {"replay_id": self.state["replay_id"], "hours_done": hours, "hours": self.total()}

    # Steps 1 to 4: the hour's ingest, in this process.

    def ingest(self, index: int) -> None:
        record = self.record(index)
        work = record["ingest"]
        if work["done"]:
            return
        work.setdefault("started", now())
        start = datetime.fromisoformat(record["start"])
        with measure.Tracer() as tracer:
            for name in INGEST:
                self.step(index, name, record["day"], start)
        work |= {"done": True, "finished": now(), "statements": tracer.summary()}
        self.save()

    def step(self, index: int, name: str, day: int, start: datetime) -> None:
        """Run one ingest step until it is done; a failed part pauses and retries."""
        step = self.record(index)["ingest"]["steps"].setdefault(name, {"done": False, "parts": []})
        while not step["done"]:
            began = time.monotonic()
            try:
                part, done = getattr(self.steps, name)(day, start, step)
            except ERRORS as exc:
                part, done = {"error": f"{type(exc).__name__}: {exc}"[:300]}, False
            part["seconds"] = round(time.monotonic() - began, 3)
            step["parts"].append(part)
            step["done"] = done
            self.save()
            if not done:
                reason = part.get("error") or "; ".join(part.get("failures") or []) or "unfinished"
                self.pause(index, name, reason[:300])

    def pause(self, index: int, step: str, reason: str) -> None:
        """Log the retry and its delay (events.jsonl, the counts), then wait it out."""
        record, counts = self.record(index), self.state["counts"]
        pause = min(self.args.pause * 2 ** record["retries"], self.args.pause * 12)
        record["retries"] += 1
        counts["retries"] += 1
        counts["pause_seconds"] = round(counts["pause_seconds"] + pause, 3)
        counts[step] = counts.get(step, 0) + 1
        event = {"time": now(), "hour": record["start"], "step": step, "reason": reason}
        with open(self.out / "events.jsonl", "a", opener=private) as events:
            events.write(json.dumps(event | {"pause": pause}) + "\n")
        if record["retries"] > FLAG_AFTER:
            flag = f"replay: hour {record['start']} retried {record['retries']} times ({step})"
            print(flag, file=sys.stderr)
        self.save()
        time.sleep(pause)

    # Step 5: the calculation, a child process beside the next hour's ingest.

    def calculate(self, index: int):
        self.record(index)["calculate"].setdefault("started", now())
        return self.pool.submit(self.child, index)

    def child(self, index: int) -> dict:
        """`proxy-index calculate` of the hour under measure.py trace (in the worker thread)."""
        end = datetime.fromisoformat(self.record(index)["start"]) + HOUR
        with tempfile.TemporaryDirectory(prefix="scale-replay-calc-") as temporary:
            trace = Path(temporary, "statements.json")
            command = [*memory_scope(not self.args.no_scope), sys.executable, str(MEASURE)]
            command += ["trace", str(trace), "proxy_index.cli", "calculate", "--database-file"]
            command += [str(self.args.database), "--methodology", str(self.args.methodology)]
            command += ["--as-of", end.isoformat(), "--publish", "--incremental"]
            timeout = self.args.hour_timeout
            result = measure.run_child(command, 0.2, timeout=timeout, stop=self.stop)
            if trace.exists():
                result["statements"] = json.loads(trace.read_text())
        return result

    def finish(self, index: int, future) -> None:
        """Wait for the hour's calculation, retried until it succeeds; then siteapi and sizes."""
        record = self.record(index)
        work = record["calculate"]
        while True:
            result = future.result()
            work["attempts"].append({k: v for k, v in result.items() if k != "statements"})
            if result["exit"] == 0 and not result.get("killed"):
                break
            if result.get("killed") == "stopped":
                raise KeyboardInterrupt
            killed = f"over {self.args.hour_timeout:g} s" if result.get("killed") else ""
            self.pause(index, "calculate", killed or f"exit {result['exit']}")
            future = self.calculate(index)
        work["statements"] = result.get("statements", [])
        build = self.conn.execute("SELECT build_id FROM current_calculation").fetchone()
        work["build_id"] = build[0] if build else None
        if self.args.siteapi:
            work["siteapi"] = self.steps.served(work["build_id"])
        work |= {"done": True, "finished": now()}
        record["sizes"] = self.sizes()
        if (index + 1) % self.args.hours_per_day == 0:
            before = self.state["baseline"]["counts"]
            counted = replay_baseline.day_counts(self.conn, before, self.state["nodes"])
            counted["table_bytes"] = measure.sizes(self.conn)["tables"]
            self.state["days_counted"][str(record["day"])] = counted
        self.save()
        try:  # after every hour, so a stopped run has its report
            replay_report.write(self.out)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"replay: no report after hour {record['start']}: {exc}", file=sys.stderr)


def run(args) -> dict:
    with storage.connect(args.database) as conn:
        conn.autocommit = True
        return Replay(conn, args).run()


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", required=True, help="credentials TOML of the database")
    parser.add_argument("--owner-go", required=True, help=f"the owner's go: {target.GO_FORM}")
    parser.add_argument("--slices", required=True, help="SLICES-1's output directory")
    parser.add_argument("--days", required=True, type=int, help="simulated days")
    parser.add_argument("--first-day", required=True, help="date of simulated day 0 (UTC)")
    parser.add_argument("--out", required=True, help="directory of the state and the events")
    parser.add_argument("--hour-timeout", type=float, default=3000.0, help="seconds")
    parser.add_argument(
        "--siteapi", help="base URL of a siteapi reading the target database, to wait for builds"
    )
    parser.add_argument("--siteapi-timeout", type=float, default=600.0)
    parser.add_argument("--no-scope", action="store_true", help="no systemd memory scope")
    parser.add_argument("--methodology", default=str(cycle.METHODOLOGY))
    parser.add_argument("--formulas", default=str(generate.FORMULAS))
    # For tests and smoke runs: hours per simulated day, the first retry's pause in seconds.
    parser.add_argument("--hours-per-day", type=int, default=24, help=argparse.SUPPRESS)
    parser.add_argument("--pause", type=float, default=300.0, help=argparse.SUPPRESS)
    parser.set_defaults(pulse_concurrency=8, node_api=None, siteapi_pid=None)
    args = parser.parse_args(argv)
    if args.days < 1 or not 1 <= args.hours_per_day <= 24:
        parser.error("--days is at least 1 and the hours of a day 1..24")
    return args


def main(argv=None) -> int:
    args = arguments(argv)
    signal.signal(signal.SIGTERM, signal.default_int_handler)  # a stop, as Ctrl-C
    try:
        result = run(args)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("stopped: the same command continues the replay", file=sys.stderr)
        return 130
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
