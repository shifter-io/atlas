"""SIM-LOAD-1: the worker processes that generate hours ahead (Producer), and the steps that
store a generated hour in the staging database with replay.py's step loop: daily, browser,
pulses (only with --pulses) and release (SIMULATE.md, "One simulated hour")."""

from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import shutil
from collections import namedtuple
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import browser_model
import simulate_check
import simulate_model
from replay_steps import NODE, REGIONS, SLUGS, ReplaySteps, ReplayWriter

from proxy_index import storage_parallel
from proxy_index.browser.contracts import BROWSER_REGION, BROWSER_REGION_ID
from proxy_index.browser.settings import load_settings
from proxy_index.daily import batches, ingest
from proxy_index.onboarding.files import read_json
from proxy_index.orchestrator.browser import browser_plan
from proxy_index.orchestrator.db import Database
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.stats import LOCATION_FIELDS, NETWORK_FIELDS

AHEAD = 2  # hours generated ahead of the one being stored
DAILY_REPORT = "daily-ingest.json"  # the hour's cells as store_cells saves them (events once)
WORKER: dict[str, Any] = {}  # a worker process's model (start)
Current = namedtuple("Current", "hour figures directory")  # the hour being stored


class SettingsChanged(Exception):
    """The browser settings file is not the one the plan was read with."""


@dataclass(frozen=True)
class SettingsFile:
    """The browser settings as the workers' plan carries them: the file's path and SHA-256.
    Loaded settings do not pickle (their dict inputs are mappingproxies): each worker loads the
    file itself, once (start)."""

    path: str
    sha256: str

    @classmethod
    def of(cls, path) -> SettingsFile:
        path = Path(path).resolve()
        return cls(str(path), hashlib.sha256(path.read_bytes()).hexdigest())

    def load(self):
        """load_settings of the file; SettingsChanged if it is not the plan's any more."""
        found = hashlib.sha256(Path(self.path).read_bytes()).hexdigest()
        if found != self.sha256:
            name, wanted = Path(self.path).name, self.sha256
            raise SettingsChanged(f"{name} changed since the plan: SHA-256 {found}, not {wanted}")
        return load_settings(self.path)


def start(profile: dict, plan, seed, replay_id: str) -> None:
    """A worker's model, from a plan whose browser settings are a SettingsFile; every summary it
    writes carries the run's own id as its marker."""
    plan = replace(plan, browser=plan.browser.load())
    WORKER["model"] = simulate_model.Model(profile, plan, seed)
    WORKER["model"].summaries.replay = replay_id


def generate(day: int, hour: int, part: int, parts: int, directory: str) -> dict[str, dict]:
    """Write part `part` of `parts` of the hour's daily tests, each in directory/<run id>; their
    figures by run id: rows per table and requests (successes and timeouts)."""
    found = {}
    for test in WORKER["model"].hour(day, hour).daily[part::parts]:
        path = Path(directory, test.run_id)
        path.mkdir(parents=True, exist_ok=True)
        test.write(path)
        requests = sum(test.successes[0]) + test.errors
        found[test.run_id] = {"tables": test.tables(), "requests": requests}
    return found


def stats(day: int, hours: int) -> dict:
    """simulate_check.stats of the first `hours` hours of a generated day."""
    model = WORKER["model"]
    return simulate_check.stats(model.profile, [model.hour(day, h) for h in range(hours)])


class Producer:
    """The worker processes, generating hours (index -> (day, hour)) ahead of the store."""

    def __init__(self, jobs: int, root: Path, initargs: tuple, hours: list[tuple[int, int]]):
        context = multiprocessing.get_context("spawn")  # never a fork of a threaded process
        self.pool = ProcessPoolExecutor(jobs, context, initializer=start, initargs=initargs)
        self.jobs, self.root, self.hours, self.pending = jobs, root, hours, {}

    def directory(self, index: int) -> Path:
        return self.root / f"hour-{index:04d}"

    def submit(self, index: int) -> None:
        if index in self.pending or index >= len(self.hours):
            return
        (day, hour), target = self.hours[index], self.directory(index)
        shutil.rmtree(target, ignore_errors=True)  # what a stopped run left is written again
        parts = range(self.jobs)
        found = [self.pool.submit(generate, day, hour, k, self.jobs, str(target)) for k in parts]
        self.pending[index] = found

    def take(self, index: int) -> dict[str, dict]:
        """The hour's figures by run id once its parts are written; the next hours start."""
        for ahead in range(index, index + AHEAD + 1):
            self.submit(ahead)
        return {k: v for future in self.pending.pop(index) for k, v in future.result().items()}

    def done(self, index: int) -> None:
        shutil.rmtree(self.directory(index), ignore_errors=True)

    def close(self) -> None:
        self.pool.shutdown(cancel_futures=True)
        shutil.rmtree(self.root, ignore_errors=True)


class SimSteps(ReplaySteps):
    """The replay's steps on the loader's database, the model's hour given (current)."""

    def __init__(self, conn, args, state: dict, model, save):
        self.conn, self.args, self.state, self.rate = conn, args, state, None
        self.save = save  # writes state.json
        self.replay, self.sim = state["replay_id"], model
        self.formulas = model.plan.formulas
        self.model = self.of(sorted(self.formulas))
        self.writer = ReplayWriter(self.replay)
        self.regions = dict(conn.execute(REGIONS).fetchall())
        self.slugs = dict(conn.execute(SLUGS).fetchall())
        self.current: Current | None = None
        self.tokens: dict[int, dict] | None = None  # the browser nodes, issued once a run

    # Step 1: the daily tests, batched as the daily ingest stores them, --connections batches
    # at once; state.json records each batch's run ids as it commits (step["groups"]).

    def daily(self, day: int, start, step: dict) -> tuple[dict, bool]:
        hour, path = self.current.hour, Path(self.args.out) / DAILY_REPORT
        previous = read_json(path) if path.exists() else {}
        if previous.get("hour") != start.isoformat():
            previous = {"cells": {}}
        report = {"hour": start.isoformat(), "cells": {}}
        tests = {(t.lane.network, t.lane.country): t for t in hour.daily}
        cells = [self.cell(cell, tests) for cell in hour.cells]
        groups = step.setdefault("groups", [])

        def committed(group, outcome) -> None:
            groups.append([result["run_id"] for result in outcome.results])
            self.save()

        options = storage_parallel.Options(self.args.connections, self.args.memory_budget)

        async def store():
            db = Database(str(self.args.database), size=2)
            try:
                found = (db, self.args.database, None, path, previous, report, start)
                own = ingest.CampaignHour(*found, options=options, on_commit=committed)
                await ingest.store_cells(own, cells, self.args.batch_daily)
            finally:
                await db.close()

        asyncio.run(store())
        done = list(report["cells"].values())
        failures = [issue for cell in done for issue in cell["issues"]]
        failures += [f"{c['network']}/{c['region']}: no events" for c in done if not c["events"]]
        part = {"tests": len(hour.daily), "cells": len(cells), "failed": len(failures)}
        part["stored"] = sum(len(cell["ingested"]) for cell in done)
        part["not_estimated"] = sum(len(cell["estimate_issues"]) for cell in done)
        return part | {"failures": failures[:20]}, not failures

    def cell(self, cell, tests: dict) -> ingest.Cell:
        """A planner cell as prepare_cell leaves it: its report so far and its countries' run
        directories, complete (the model's rounds always are)."""
        own = [tests[cell.network, code] for code in cell.countries]
        report = {"network": cell.network, "region": cell.region, "status": "failed"}
        report |= {"countries": list(cell.countries), "unique_ips": 0}
        report["requests"] = sum(self.current.figures[t.run_id]["requests"] for t in own)
        report["started_at"] = min(t.started_at for t in own).isoformat()
        report["finished_at"] = max(t.finished_at for t in own).isoformat()
        report |= {name: [] for name in ("ingested", "issues", "estimated", "estimate_issues")}
        report["spool_issues"] = []
        directory, countries = self.current.directory, []
        for t in own:
            found = batches.Country(t.lane.country, {"unique_ips": t.size}, t.lane.formula)
            countries.append(found)
            found.directory = directory / t.run_id
        product = self.formulas[cell.network].get("product", "residential")
        manifest = {"provider": cell.network, "product": product}
        key = f"{cell.network}/{cell.region}"
        return ingest.Cell(key, cell.network, manifest, report, countries, complete=True)

    # Step 2: the browser runs of the hosts' queues.

    def browser(self, day: int, start, step: dict) -> tuple[dict, bool]:
        runs = {(run.target, run.country, run.start): run for run in self.current.hour.browser}
        if not runs:
            return {"runs": 0, "planned": 0, "failed": 0}, True
        hour = Hour(self.args.database, self.replay, start, step.setdefault("uploaded", []))
        hour.runs, hour.settings = runs, self.sim.plan.browser
        part = asyncio.run(hour.drive("", self.browser_nodes()))
        return part, part["failed"] == 0

    def browser_nodes(self) -> dict[int, dict]:
        """A browser node of every network of the settings' hosts, bound to its host's name,
        where the database has none but the run's (another's node gets no upload): id -> its
        token, provider, product and host. Issued once a process; the tokens are never saved."""
        if self.tokens is not None:
            return self.tokens
        self.tokens, mine = {}, self.state["nodes"]
        for host in self.sim.plan.browser.hosts:
            for network in host.networks:
                provider, product = network.split("/", 1)
                found = self.conn.execute(NODE, (BROWSER_REGION_ID, provider, product)).fetchone()
                if found and found[0] not in mine:
                    continue
                who = {"provider": provider, "product": product, "region": BROWSER_REGION}
                node = issue_token(self.args.database, **who, host=host.name)
                mine.extend([node["node_id"]] if node["node_id"] not in mine else [])
                self.tokens[node["node_id"]] = node | {"host": host.name}
        return self.tokens

    # Step 3: pulses, only with --pulses (the replay's, with the model's geo rows).

    def country_rows(self, hour: int, country: str) -> list[dict]:
        return []  # no slice: a load wave's IP rows are synthetic addresses

    def pulse_geo(self, slot, run: dict) -> tuple:
        lane = self.lane(run["target_id"], run["provider"], run["product"], run["country"])
        found = self.sim.pulse(run["provider"], run["country"], run["target_id"], run["start_at"])
        rows = []
        for place, completed, successes, ms in found.geo:
            location = {key: place.get(key) for key in LOCATION_FIELDS}
            network = {key: place.get(key) for key in NETWORK_FIELDS}
            where = (location, network) if place.get("matched") else (None, None)
            rows.append((*where, completed, successes, ms))
        return lane, found.errors, rows


@dataclass
class Hour(browser_model.Hour):
    """A generated browser hour: the queue planner at each run's start (its settings given, no
    settings row written), and each run's own bundle from its node's host."""

    runs: dict = field(default_factory=dict)  # (target, country, start) -> simulate_browser run
    settings: Any = None

    async def plan(self, db: Database, text: str, work: Path) -> dict:
        created = 0
        for moment in sorted({run.start for run in self.runs.values()}):
            created += (await browser_plan(db, moment, self.settings)).get("created", 0)
        return {"created": created}

    def run(self, row: dict):
        return self.runs[row["target_id"], row["country"], row["scheduled_for"]]

    def bundle(self, row: dict, outbox: Path) -> Path:
        run = self.run(row)
        return run.bundle(row | {"host": run.host}, self.seed, outbox)

    def size(self, row: dict) -> tuple[int, int]:
        tries = self.run(row).tries
        return len(tries), sum(size for _, size, *_ in tries)
