"""SCORE-DATA-1: `simulate.py --add-scored-inputs` stores the pulse and load runs simulate_scored.py
plans on a stored load's days (SIMULATE.md, "Score inputs"): only pulse and load batches, their
runs and the days' load_references; the daily and browser runs stay as stored."""

from __future__ import annotations

import json
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import cycle_steps
import replay
import simulate_model
import simulate_scored as model
import simulate_scored_report as report
import writer
from replay import Refused
from replay_report import write_private
from simulate_scored import MARK, Target
from simulate_scored_read import Reader, planned_references, references
from synthetic import key

from proxy_index import storage

STEP = "score_inputs"  # the load state's key
CHUNK = 250  # runs an ingest transaction takes
FOREIGN = (  # pulse and load runs of the days that are not this mode's
    "SELECT count(*) FROM batch_runs br JOIN test_batches b USING(batch_id) "
    "LEFT JOIN test_runs t ON t.run_key=br.run_key WHERE b.kind IN ('pulse','load') "
    "AND b.scheduled_for>=%s AND b.scheduled_for<%s "
    "AND (t.run_key IS NULL OR NOT t.summary ? 'score_inputs')"
)
OPEN = (
    "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,start_at) "
    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (kind,country_code,target_id,scheduled_for) "
    "DO NOTHING"
)
BATCHES = (
    "SELECT kind,trim(country_code),target_id,scheduled_for,batch_id,start_at FROM test_batches "
    "WHERE kind IN ('pulse','load') AND scheduled_for>=%s AND scheduled_for<%s"
)
FINISH = (
    "INSERT INTO batch_runs(batch_id,product_id,status,started_at,finished_at,last_progress_at,"
    "requests,successes,unique_ips,timeouts,bytes_up,bytes_down,stop_reason,run_key) "
    "VALUES (%s,%s,'done',%s,%s,%s,%s,%s,%s,%s,%s,%s,'requests',%s) "
    "ON CONFLICT (batch_id,product_id) DO NOTHING"
)
DONE = (
    "UPDATE test_batches SET status='done',started_at=start_at,finished_at=%s "
    "WHERE batch_id=%s AND status<>'done'"
)


def summary(sim: simulate_model.Summaries, run, product: str, target: Target, ips) -> tuple:
    """A run's summary.json and its ips.jsonl rows, started at its batch's start."""
    started, errors = run_start(run), run.requests - run.successes
    lane = sim.lane(run.target, run.network, product, run.country)
    moment = started.replace(minute=started.minute - started.minute % 5, second=0)
    buckets = [cycle_steps.run_bucket(moment, run.successes, errors, run.bins)]
    body = sim.summary(run.run_id, lane, started, duration(run), buckets)
    concurrency = target.concurrency if run.kind == "pulse" else target.load_requests
    body["config"] |= {"concurrency": concurrency, "timeout_seconds": writer.TIMEOUT}
    body["config"] |= {"requests_per_second": None, "requests": run.requests}
    rows = [sim.ips_row(run.run_id, ip, n, moment) for ip, n in ips]
    body |= {"requested": run.requests, "stop_reason": "requests"} | MARK
    return body, rows


def run_start(run):
    """A pulse starts in its slot's first two minutes; a load wave 75 seconds after it."""
    seconds = 75 if run.kind == "load" else 15 + key(run.country, run.moment) % 105
    return run.moment + timedelta(seconds=seconds)


def duration(run) -> int:
    return (60 if run.kind == "load" else 20) + key(run.run_id) % 30


class Store:
    """Writes the days hour by hour: the hour's batches, its pulse runs, its load runs (through
    storage.ingest_many, CHUNK runs a transaction) and their batch runs with the batches done.
    Every write skips what is stored, so an hour stopped half-way is written again whole."""

    def __init__(self, conn, args, state: dict, reader: Reader, save):
        self.conn, self.args, self.state, self.reader, self.save = conn, args, state, reader, save
        self.inputs, self.work = reader.inputs, state[STEP]
        self.plan = [model.Outage(**o) for o in self.work["plan"]]  # countries: a list
        self.sim = simulate_model.Summaries(state["replay_id"], reader.slugs)
        self.targets = {target.id: target for target in self.inputs.targets}
        self.samples: dict = {}

    def run(self) -> None:
        for index in range(self.inputs.days * 24):
            if index in self.work["hours"]:
                continue
            day, hour = index // 24 + 1, index % 24
            given = (self.inputs, self.plan, self.state["seed"], self.day_references(day))
            runs = model.hour_runs(*given, day, hour)
            self.hour(runs, self.inputs.day_start(day) + timedelta(hours=hour))
            self.work["hours"].append(index)
            self.work["runs"] += len(runs)
            self.save()

    def day_references(self, day: int) -> dict:
        found = self.work["references"]
        if str(day) not in found:
            loading = self.inputs.load and any(t.census for t in self.inputs.targets)
            found[str(day)] = references(self.args.database, self.inputs, day) if loading else {}
            self.save()
        return found[str(day)]

    def hour(self, runs: list, start) -> None:
        if not runs:
            return
        regions = self.reader.region_ids  # a batch's runs share its start (run_start)
        rows = {
            r.batch: (*r.batch[:2], regions[r.country], *r.batch[2:], run_start(r)) for r in runs
        }
        with self.conn.transaction():
            self.conn.cursor().executemany(OPEN, sorted(rows.values()))
        found = self.conn.execute(BATCHES, (start, start + timedelta(hours=1))).fetchall()
        batches = {tuple(row[:4]): row[4] for row in found}
        keys, uniques = {}, {}
        for kind in ("pulse", "load"):
            own = [r for r in runs if r.kind == kind]
            for i in range(0, len(own), CHUNK):
                keys |= self.ingest(own[i : i + CHUNK], batches, uniques)
        self.finish(runs, batches, keys, uniques)

    def ingest(self, runs: list, batches: dict, uniques: dict) -> dict:
        """Ingest runs of one kind (a load's with its IP rows); run id -> run_key."""
        with tempfile.TemporaryDirectory(prefix="score-inputs-") as temporary:
            directories = []
            for run in runs:
                directory = Path(temporary, run.run_id)
                directory.mkdir()
                loading = run.kind == "load"
                ips = model.draw(self.real(run), run.run_id, run.successes) if loading else []
                product = self.reader.product[run.network]
                body, rows = summary(self.sim, run, product, self.targets[run.target], ips)
                unique = len(ips) if loading else self.reader.unique_ips(run)
                uniques[run.run_id] = body["unique_ips"] = unique
                body["artifacts"] = {"ips": cycle_steps.gzip_rows(directory / "ips.jsonl.gz", rows)}
                (directory / "summary.json").write_text(json.dumps(body))
                directories.append(directory)
            ids, loads = [batches[run.batch] for run in runs], runs[0].kind == "load"
            given = {"batch_ids": ids, "ip_rows": loads, "conn": self.conn}
            stored = storage.ingest_many(directories, None, **given)
        return {found["run_id"]: found["run_key"] for found in stored}

    def real(self, run) -> list[str]:  # the day's samples, read once each
        given = (run.network, run.country, self.inputs.day_of(run.moment))
        if given not in self.samples:
            self.samples = {k: v for k, v in self.samples.items() if k[2] == given[2]}
            self.samples[given] = self.reader.ips(*given)
        return self.samples[given]

    def finish(self, runs: list, batches: dict, keys: dict, uniques: dict) -> None:
        """The batch runs done with their counters, then their batches done."""
        rows, done = [], {}
        for run in runs:
            batch, started = batches[run.batch], run_start(run)
            finished = started + timedelta(seconds=duration(run))
            product = self.reader.products[run.network, self.reader.product[run.network]]
            errors = run.requests - run.successes
            counters = (run.requests, run.successes, uniques[run.run_id], errors)
            size = (run.requests * writer.UP, run.successes * writer.DOWN, keys[run.run_id])
            rows.append((batch, product, started, finished, finished, *counters, *size))
            done[batch] = max(done.get(batch, finished), finished)
        with self.conn.transaction():
            cursor = self.conn.cursor()
            cursor.executemany(FINISH, rows)
            cursor.executemany(DONE, [(at, batch) for batch, at in done.items()])


def loaded(path: Path, wanted: dict) -> dict:
    """The load's state: refused unless it is these arguments' load and wholly stored."""
    if not path.exists():
        raise Refused(f"--add-scored-inputs: no load in {path.parent.name} (state.json)")
    state = json.loads(path.read_text())
    if {name: state.get(name) for name in wanted} != wanted:
        raise Refused(f"{path.name} is the load of {[state.get(name) for name in wanted]}")
    if not state["hours"] or not all(h["ingest"]["done"] for h in state["hours"]):
        raise Refused("the load is not finished: run it to its end first")
    return state


def opened(state: dict, args, go: dict, plan: list) -> dict:
    """The mode's record in the state: new, or the same one resumed (same --pulse-days)."""
    days = {network: list(found) for network, found in args.pulse_days}
    new = {"version": model.VERSION, "pulse_days": days, "go": go, "resumed": [], "runs": 0}
    work = state.setdefault(STEP, new | {"plan": [vars(o) for o in plan], "references": {}})
    if work["pulse_days"] != days:
        raise Refused(f"the scored inputs were added with --pulse-days {work['pulse_days']}")
    work.setdefault("hours", [])
    if work["go"] is not go:
        work["resumed"].append(go)
    return work


def add(args, wanted) -> dict:
    """--add-scored-inputs on a stored load: write every hour not written yet."""
    out = Path(args.out)
    with storage.connect(args.database) as conn:
        conn.autocommit = True
        # replay.guard's last hour is the load's extra one, as simulate.Simulation's.
        ended = SimpleNamespace(**vars(args) | {"days": args.days + 1, "hours_per_day": 1})
        go = replay.guard(conn, ended)
        state = loaded(out / "state.json", wanted(go["database"]))
        reader = Reader(conn, args, state)
        inputs = reader.read()
        fresh = STEP not in state
        work = opened(state, args, go, model.outages(inputs, state["seed"]))
        if fresh and conn.execute(FOREIGN, reader.bounds[1:]).fetchone()[0]:
            raise Refused("the days already hold pulse or load runs this mode did not write")

        def save():
            write_private(out / "state.json", json.dumps(state, default=str))

        save()
        began = time.monotonic()
        try:
            Store(conn, args, state, reader, save).run()
        finally:
            save()
    found = {"replay_id": state["replay_id"], "hours_done": len(work["hours"])}
    plan = [model.Outage(**o) for o in work["plan"]]
    chosen = {int(day): own for day, own in work["references"].items()}
    about = report.Given(inputs, plan, state["seed"], chosen, reader.source, work["pulse_days"])
    taken = round((time.monotonic() - began) / 60, 1)
    written = report.build(about, "run", report.load_rate(out), taken)
    return found | {"hours": args.days * 24, "summary": report.write(out, written)}


def plan_only(args, wanted) -> dict:
    """--plan-only: the counts, outage plan, uptimes and pulse days (day -> its IPs' day), read
    in one read-only transaction."""
    out = Path(args.out)
    with storage.connect(args.database) as conn:
        conn.read_only = True
        with conn.transaction():
            database = conn.execute("SELECT current_database()").fetchone()[0]
            state = loaded(out / "state.json", wanted(database))
            reader = Reader(conn, args, state)
            inputs = reader.read()
            given = planned_references(conn, inputs)
    plan = model.outages(inputs, state["seed"])
    found = {"version": model.VERSION, "replay_id": state["replay_id"]}
    found["days"] = model.counts(inputs, plan, state["seed"], given)
    found["outages"] = [vars(o) | {"from": f"{o.span(inputs)[0]:%H:%M}"} for o in plan]
    found["uptime"] = {
        network: {f"{h}h": model.uptime(inputs, plan, network, h) for h in model.WINDOWS}
        for network in inputs.networks
    }
    found["pulse_days"] = {
        network: {day: reader.source[network, day] for day in inputs.pulse_days(network)}
        for network in inputs.networks
    }
    days = {network: list(own) for network, own in args.pulse_days}
    about = report.Given(inputs, plan, state["seed"], given, reader.source, days)
    written = report.build(about, "plan only", report.load_rate(out), None)
    return found | {"summary": report.write(out, written)}


def main(args, wanted) -> int:
    """--add-scored-inputs: its plan (--plan-only), or the runs added."""
    if args.plan_only:
        print(json.dumps(plan_only(args, wanted), indent=2, default=str))
    else:
        print(json.dumps(add(args, wanted), indent=2))
    return 0
