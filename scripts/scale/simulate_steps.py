"""SIM-LOAD-1: the worker processes that generate hours ahead (Producer), and the steps that
store a generated hour in the staging database with replay.py's step loop: daily, browser,
pulses (only with --pulses) and release (SIMULATE.md, "One simulated hour")."""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import shutil
import threading
import time
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
from proxy_index.onboarding.files import atomic_json, read_json
from proxy_index.orchestrator.browser import browser_plan
from proxy_index.orchestrator.db import Database
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.stats import LOCATION_FIELDS, NETWORK_FIELDS

AHEAD = 2  # hours generated ahead of the one being stored (--ahead)
SPOOL = 20 << 30  # bytes the generated hours not stored yet may take (--spool-gb)
STORE_AHEAD = 6  # hours whose daily groups the store takes at once (--store-ahead)
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


def generate(day: int, hour: int, part: int, parts: int, directory: str) -> dict:
    """Write part `part` of `parts` of the hour's daily tests, each in directory/<run id>, and
    its manifest: each file's size and SHA-256, and the tests' figures by run id (rows per table
    and requests: successes and timeouts). A part whose manifest finds its files complete (a
    resumed load's spool) is not written again. Returns the figures, the files' bytes, the CPU
    seconds it took and whether it was reused."""
    began, manifest = time.process_time(), Path(directory, f"part-{part}-of-{parts}.json")
    found = _complete(manifest)
    if found is not None:
        return found | {"cpu": time.process_time() - began, "reused": True}
    figures, files = {}, {}
    for test in WORKER["model"].hour(day, hour).daily[part::parts]:
        path = Path(directory, test.run_id)
        path.mkdir(parents=True, exist_ok=True)
        test.write(path)
        requests = sum(test.successes[0]) + test.errors
        figures[test.run_id] = {"tables": test.tables(), "requests": requests}
        for file in sorted(f for f in path.iterdir() if f.is_file()):
            files[f"{test.run_id}/{file.name}"] = [file.stat().st_size, _sha256(file)]
    atomic_json(manifest, {"figures": figures, "files": files}, indent=None)
    found = {"figures": figures, "bytes": sum(size for size, _ in files.values())}
    return found | {"cpu": time.process_time() - began, "reused": False}


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _complete(manifest: Path) -> dict | None:
    """The part's figures and bytes when every file its manifest names has its size and
    SHA-256; else None."""
    try:
        found = json.loads(manifest.read_text())
        for name, (size, sha256) in found["files"].items():
            path = manifest.parent / name
            if path.stat().st_size != size or _sha256(path) != sha256:
                return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return {"figures": found["figures"], "bytes": sum(s for s, _ in found["files"].values())}


def stats(day: int, hours: int) -> dict:
    """simulate_check.stats of the first `hours` hours of a generated day."""
    model = WORKER["model"]
    return simulate_check.stats(model.profile, [model.hour(day, h) for h in range(hours)])


def _generated(future) -> bool:
    """Whether a part is written (not cancelled by a stop, not failed)."""
    return future.done() and not future.cancelled() and future.exception() is None


class Producer:
    """The worker processes, generating hours (index -> (day, hour)) up to `ahead` hours ahead
    of the one being stored while the generated hours not stored yet take less than `spool`
    bytes on disk (the hour being stored is always generated). A stopped load keeps them: the
    next one reuses the complete parts (generate)."""

    def __init__(self, jobs: int, root: Path, initargs: tuple, hours: list, limits=(AHEAD, SPOOL)):
        context = multiprocessing.get_context("spawn")  # never a fork of a threaded process
        self.pool = ProcessPoolExecutor(jobs, context, initializer=start, initargs=initargs)
        self.jobs, self.root, self.hours, self.pending = jobs, root, hours, {}
        (self.ahead, self.spool), self.sizes, self.current = limits, {}, 0
        self.lock, self.wake, self.closed = threading.RLock(), threading.Event(), False
        self.counts = {"generated_tests": 0, "generation_cpu_seconds": 0.0, "reused_parts": 0}
        # Submits from a thread of its own: never from the pool's thread that runs callbacks.
        threading.Thread(target=self.feed, daemon=True).start()

    def directory(self, index: int) -> Path:
        return self.root / f"hour-{index:04d}"

    def submit(self, index: int) -> None:
        if index in self.pending or index >= len(self.hours) or self.closed:
            return
        (day, hour), target = self.hours[index], str(self.directory(index))
        parts = range(self.jobs)
        found = [self.pool.submit(generate, day, hour, k, self.jobs, target) for k in parts]
        self.pending[index] = found
        for future in found:
            future.add_done_callback(lambda _, index=index: self.generated(index))

    def generated(self, index: int) -> None:
        """Once all parts of an hour are written: its bytes and costs; then more hours."""
        with self.lock:
            found = self.pending.get(index, [])
            if index in self.sizes or not all(_generated(future) for future in found):
                return
            parts = [future.result() for future in found]
            self.sizes[index] = sum(part["bytes"] for part in parts)
            written = [part for part in parts if not part["reused"]]  # its CPU is generation's
            self.counts["generated_tests"] += sum(len(part["figures"]) for part in written)
            self.counts["generation_cpu_seconds"] += sum(part["cpu"] for part in written)
            self.counts["reused_parts"] += len(parts) - len(written)
        self.wake.set()

    def feed(self) -> None:
        while not self.closed:
            self.wake.wait()
            self.wake.clear()
            self.top_up()

    def spooled(self) -> float:
        """Bytes of the hours generated or generating and not stored yet: a generating hour
        counts as large as the generated ones on average."""
        known = [self.sizes[index] for index in self.pending if index in self.sizes]
        average = sum(known) / len(known) if known else 0
        return sum(known) + average * (len(self.pending) - len(known))

    def top_up(self) -> None:
        """Submit the next hours, two generating at a time (each keeps every job busy)."""
        with self.lock:
            for later in range(self.current, self.current + self.ahead + 1):
                busy = len(self.pending) - len(self.sizes) >= 2 or self.spooled() >= self.spool
                if later > self.current and later not in self.pending and busy:
                    return  # the generator pauses at the cap
                self.submit(later)

    def ready(self, index: int) -> bool:
        with self.lock:
            found = self.pending.get(index)
            return bool(found) and all(_generated(future) for future in found)

    def take(self, index: int) -> dict[str, dict]:
        """The hour's figures by run id once its parts are written; the next hours start."""
        self.current = index
        self.top_up()
        with self.lock:
            found = list(self.pending[index])
        return {k: v for future in found for k, v in future.result()["figures"].items()}

    def done(self, index: int) -> None:
        with self.lock:
            self.pending.pop(index, None)
            self.sizes.pop(index, None)
            self.current = max(self.current, index + 1)  # never generated again
        shutil.rmtree(self.directory(index), ignore_errors=True)
        self.wake.set()

    def close(self, finished: bool) -> None:
        """Stop generating; the generated hours go once every hour is stored."""
        with self.lock:
            self.closed = True
        self.wake.set()
        self.pool.shutdown(cancel_futures=True)
        if finished:
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
        self.pool: storage_parallel.Pool | None = None  # the load's, open over its hours

    # Step 1: the daily tests, batched as the daily ingest stores them, --connections batches
    # at once over --processes processes, with the next hours' (hand); state.json records each
    # batch's run ids as its hour's step sees it committed (step["groups"]).

    def hand(self, hour, directory: Path) -> None:
        """Give the pool a later hour's daily groups, as its step builds them, to store while
        this hour's are stored (--store-ahead); its step then waits for them."""
        tests = {(t.lane.network, t.lane.country): t for t in hour.daily}
        cells = []
        for cell in hour.cells:
            countries = self.countries(cell, tests, directory)
            cells.append(ingest.Cell(cell.network, cell.network, {}, {}, countries))
        groups = ingest.plan_groups(cells, self.args.batch_daily)[1]
        self.pool.submit(batches.groups_of(groups)[1])

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

        args = self.args
        options = storage_parallel.Options(args.connections, args.memory_budget, args.processes)
        options.pool = self.pool

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
        countries = self.countries(cell, tests, self.current.directory)
        product = self.formulas[cell.network].get("product", "residential")
        manifest = {"provider": cell.network, "product": product}
        key = f"{cell.network}/{cell.region}"
        return ingest.Cell(key, cell.network, manifest, report, countries, complete=True)

    @staticmethod
    def countries(cell, tests: dict, directory: Path) -> list[batches.Country]:
        """A planner cell's countries, each with its run directory under the hour's."""
        found = []
        for code in cell.countries:
            test = tests[cell.network, code]
            country = batches.Country(code, {"unique_ips": test.size}, test.lane.formula)
            country.directory = directory / test.run_id
            found.append(country)
        return found

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
