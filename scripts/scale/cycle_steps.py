"""SCALE-GEN-1b: the five steps of one simulated hour of the cycle (cycle.py runs them).

ingest stores the hour's daily tests through storage.ingest and their estimated hours; pulses plans
the hour's pulse slot and uploads every run to the node API (POST /v1/results); release, calculate
and siteapi release the ended estimated hours, publish a build in a child process and wait until
siteapi serves it. Each step returns (part, done): its measurement and whether it finished.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiohttp
import measure
import synthetic
import writer
from aiohttp import web
from psycopg.rows import dict_row
from synthetic import address, apportion
from writer import BODY, DOWN, TARGET

from proxy_index import daily, storage
from proxy_index.estimates import compute
from proxy_index.estimates.release import release_due
from proxy_index.orchestrator import batches
from proxy_index.orchestrator.app import build_app
from proxy_index.orchestrator.archive import pack
from proxy_index.orchestrator.db import Database
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.stats import Histogram
from proxy_index.storage import GEO_COUNTERS

MARKER = "scale_cycle"
HOUR = timedelta(hours=1)
MEASURE = Path(__file__).resolve().with_name("measure.py")
# The scope settings the generator gives daily tests and pulses (Writer.scope).
DAILY_SCOPE = {"concurrency": writer.DAILY_CONCURRENCY, "timeout_seconds": writer.TIMEOUT}
DAILY_SCOPE["requests_per_second"] = None
PULSE_SCOPE = DAILY_SCOPE | {"concurrency": writer.PULSE_CONCURRENCY}
DAILY_LENGTH = {"requests": None, "stop_rule": "unique_ratio", "min_sample": 1000}
DAILY_LENGTH["stall_seconds"] = 600
RUNS = (
    "SELECT br.batch_id,br.product_id,br.node_id,br.status,trim(b.country_code) AS country,"
    "b.start_at FROM batch_runs br JOIN test_batches b USING(batch_id) WHERE b.kind='pulse' "
    "AND b.target_id=%s AND b.scheduled_for=%s ORDER BY b.start_at,br.batch_id"
)
PENDING = ("queued", "running", "uploading")


class Refused(Exception):
    """The database or the hour may not be used; nothing was written."""


class Failed(Exception):
    """A step failed; the progress before it is kept."""


def gzip_rows(path: Path, rows: list) -> dict:
    """Write JSON lines as a reproducible gzip file; its artifact entry."""
    with path.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as out:
        for row in rows:
            out.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"file": path.name, "rows": len(rows), "sha256": sha}


def bucket(moment: datetime, successes: int, errors: int, bins) -> dict:
    """Counters of a summary bucket or geo row; timeouts are the only errors, as generated."""
    completed, found = successes + errors, [[ms, n] for ms, n in sorted(bins) if n]
    values = (completed, successes, 0, 0, errors, completed * writer.UP, successes * DOWN)
    return (
        {"bucket_start": moment.isoformat(timespec="microseconds")}
        | dict(zip(GEO_COUNTERS, values, strict=True))
        | {"latency_histogram": {"format": "ceil_ms_v1", "bins": found}}
    )


def run_bucket(moment: datetime, successes: int, errors: int, bins) -> dict:
    extra = {"response_body_bytes": successes * BODY, "proxy_errors": 0}
    extra["errors"] = {"timeout": errors} if errors else {}
    extra["status_codes"] = {"200": successes} if successes else {}
    return bucket(moment, successes, errors, bins) | extra


def daily_buckets(started: datetime, successes, errors, bins) -> list[dict]:
    """A daily test's 5-minute buckets, latency modelled from the network's bins."""
    found = []
    for b, (good, bad) in enumerate(zip(successes, errors, strict=True)):
        latency = zip(bins, apportion(good, synthetic.WEIGHTS), strict=True)
        found.append(run_bucket(started + timedelta(minutes=5 * b), good, bad, latency))
    return found


def daily_files(directory: Path, run_id: str, rows, release: str, metadata: dict) -> dict:
    """A daily test's ips.jsonl.gz and DB-IP files from (ips row, enrichment); the ips entry."""
    ips = gzip_rows(directory / "ips.jsonl.gz", [row for row, _ in rows])
    enriched = [row | {"dbip_release": release, "enrichment": place} for row, place in rows]
    report = gzip_rows(directory / f"enriched-{release}.jsonl.gz", enriched)
    report |= {"run_id": run_id, "release": {"sha256": release} | metadata}
    (directory / f"enrichment-{release}.json").write_text(json.dumps(report))
    return ips


def observation(run_id: str, line: str) -> tuple[int, dict]:
    """(IP index, ips.jsonl row) of one of the generator's ip_observations COPY lines."""
    moment, _, ip_id, count, first, last = line.split("\t")
    times = [datetime.fromisoformat(value).isoformat() for value in (moment, first, last)]
    row = {"schema_version": "1", "run_id": run_id, "ip": address(int(ip_id) - 1)}
    row |= dict(zip(("bucket_start", "first_seen", "last_seen"), times, strict=True))
    return int(ip_id) - 1, row | {"count": int(count)}


def healthz(base: str) -> dict:
    """siteapi's /healthz (also the 503 while starting); {} when it cannot be read."""
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/healthz", timeout=10) as reply:
            return json.loads(reply.read())
    except urllib.error.HTTPError as exc:
        with contextlib.suppress(ValueError):
            return json.loads(exc.read())
    except (OSError, ValueError):
        pass
    return {}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def work(session, base: str, token: str) -> None:
    """GET /v1/work as a node does: its queued runs start."""
    async with session.get(f"{base}/v1/work", headers=bearer(token)) as reply:
        reply.raise_for_status()


@contextlib.asynccontextmanager
async def serve(database, db: Database):
    """An orchestrator app on db, served on loopback for the block; its base URL."""
    app = build_app(str(database), with_scheduler=False, db=db, admin_credential=None)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


class Steps:
    """The steps of a Cycle: it holds conn, args, model, writer, positions, locations, networks,
    pg_stat, rate and state, and late() tells when the run's budget is spent."""

    def summary(self, run_id: str, lane, started: datetime, duration: int, buckets) -> dict:
        """A run's summary.json (without its kind's keys), as the runner writes one."""
        bins, successes, errors = Counter(), 0, 0
        for item in buckets:
            bins.update(dict(item["latency_histogram"]["bins"]))
            successes, errors = successes + item["successes"], errors + item["timeouts"]
        body = self.writer.summary(successes, errors, bins, duration, {}, {})
        del body["config_overrides"], body[writer.MARKER]
        finished = (started + timedelta(seconds=duration)).isoformat()
        body |= {"run_id": run_id, "status": "completed"}
        body |= {"started_at": started.isoformat(), "finished_at": finished, "buckets": buckets}
        body["latency_histogram"] = Histogram(bins).export()
        return body | self.identity(lane)

    def identity(self, lane) -> dict:
        """A lane's target, config and marker in its runs' summaries."""
        config = self.writer.config(lane)
        return {"target": self.writer.target, "config": config, MARKER: self.model.settings.seed}

    def daily_summary(self, run_id: str, lane, started: datetime, buckets, ips, unique) -> dict:
        """A daily test's summary.json: the run's, with the daily scope, length and marker."""
        summary = self.summary(run_id, lane, started, len(buckets) * 300 - 1, buckets)
        summary["config"] |= DAILY_SCOPE | DAILY_LENGTH | {"target_url": summary["target"]["url"]}
        summary |= {"requested": None, "unique_ips": unique, "stop_reason": "unique_ratio"}
        return summary | {"artifacts": {"ips": ips}, daily.MARKER: daily.DAILY}

    def run_id(self, *parts) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{MARKER}:{self.model.settings.seed}:{parts}"))

    # Step 1: the hour's daily tests.

    def ingest(self, day: int, start: datetime, step: dict) -> tuple[dict, bool]:
        done = step.setdefault("ingested", [])
        todo = [lane for lane in self.model.lanes if lane.hour == start.hour]
        todo = [lane for lane in todo if lane.position not in done]
        timings, rows = [], 0
        for lane in todo:
            if self.late():
                break
            began = time.monotonic()
            rows += self.daily(self.model.test(lane, day))
            timings.append(round(time.monotonic() - began, 3))
            done.append(lane.position)
        part = {"tests": len(timings), "observation_rows": rows, "per_test": timings}
        return part, len(timings) == len(todo)

    def daily(self, test) -> int:
        """Ingest one daily test and store its estimated hours; its ip_observations rows."""
        run_id = self.run_id("daily", test.day, test.lane.position)
        with tempfile.TemporaryDirectory(prefix="scale-cycle-") as temporary:
            self.daily_directory(test, run_id, Path(temporary))
            storage.ingest(temporary, self.args.database)
        country = self.model.settings.countries[test.lane.country]
        compute.estimate_run(self.args.database, run_id, country, self.model.formula(test.lane))
        return test.rows

    def daily_directory(self, test, run_id: str, directory: Path) -> None:
        """summary.json, ips.jsonl.gz and the DB-IP enrichment files of one daily test."""
        lines, successes, _ = self.writer.observations(test)
        rows = [observation(run_id, line) for line in lines]
        release, places = self.writer.release(test.day), self.attributes(test)
        observed = [(row, places[n]) for n, row in rows]
        ips = daily_files(directory, run_id, observed, release, {"synthetic": True})
        errors = apportion(test.errors, [1] * test.buckets)
        bins = self.model.latency(test.lane.network)
        buckets = daily_buckets(test.started_at, successes, errors, bins)
        summary = self.daily_summary(run_id, test.lane, test.started_at, buckets, ips, test.ips)
        (directory / "summary.json").write_text(json.dumps(summary))

    def attributes(self, test) -> dict[int, dict]:
        """DB-IP attributes of each IP of a test, from its owner country's synthetic lists."""
        found, other = {}, self.model.neighbour(test.lane)
        for i, n in enumerate(test.indexes()):
            owner = test.lane if i < test.size else other
            location, network, matched = self.model.enrichment(n, self.model.places[owner.country])
            found[n] = {"matched": matched}
            if matched:
                found[n] |= self.locations[location] | self.networks[network]
        return found

    # Step 2: the hour's pulses, through the node API.

    def pulse_nodes(self) -> dict[int, str]:
        """A node of every network in every region of the data set's countries: id -> token."""
        tokens = {}
        slugs = {self.writer.regions[code][1] for code in self.positions}
        for n, network in enumerate(self.model.settings.networks):
            product = self.model.formulas[n].get("product", "residential")
            for slug in sorted(slugs):
                node = issue_token(
                    self.args.database, provider=network, product=product, region=slug
                )
                tokens[node["node_id"]] = node["token"]
        return tokens

    def pulses(self, day: int, start: datetime, step: dict) -> tuple[dict, bool]:
        part = asyncio.run(self.upload_hour(day, start, self.pulse_nodes()))
        runs = self.pulse_runs(start)
        part["planned"] = len(runs)
        part["statuses"] = dict(Counter(run["status"] for run in runs))  # the runs' final states
        return part, not any(run["status"] in PENDING for run in runs)

    def pulse_runs(self, start: datetime) -> list[dict]:
        """The pulse runs of the hour's slot that this driver uploads."""
        return self.conn.cursor(row_factory=dict_row).execute(RUNS, (TARGET, start)).fetchall()

    def plan_times(self, start: datetime) -> list[datetime]:
        """The moments pulse_plan runs at in the hour: its start (the generator's hourly slot)."""
        return [start]

    @contextlib.asynccontextmanager
    async def node_api(self, db: Database):
        """The node API's base URL: --node-api, or an orchestrator app served on loopback."""
        if self.args.node_api:
            yield self.args.node_api.rstrip("/")
            return
        async with serve(self.args.database, db) as base:
            yield base

    async def upload_hour(self, day: int, start: datetime, tokens: dict[int, str]) -> dict:
        """Plan the hour's slot as the scheduler does, then upload its runs at the pulse rate."""
        db = Database(str(self.args.database), size=4)
        try:
            for moment in self.plan_times(start):
                await batches.pulse_plan(db, moment)
            # Concurrent first events of a month can race on CREATE TABLE IF NOT EXISTS of
            # the test_events partition; the app shares this pool, which creates it once here.
            await db.ensure_partition("test_events", datetime.now(UTC))
            runs = [run for run in self.pulse_runs(start) if run["status"] in PENDING]
            async with self.node_api(db) as base, aiohttp.ClientSession() as session:
                await asyncio.gather(*(work(session, base, token) for token in tokens.values()))
                return await self.upload_runs(session, base, (day, start), runs, tokens)
        finally:
            await db.close()

    async def upload_runs(self, session, base: str, slot, runs: list[dict], tokens) -> dict:
        gate, began = asyncio.Semaphore(self.args.pulse_concurrency), time.monotonic()
        latencies, failures, tasks = [], [], []

        async def one(run):
            async with gate:
                try:
                    latencies.append(await self.upload(session, base, slot, run, tokens))
                # A timeout (aiohttp's, or a slow ingest) or a 200 that is not JSON is a
                # failed upload like a rejected one: recorded, and the run stays open.
                except (Failed, aiohttp.ClientError, TimeoutError, ValueError) as exc:
                    failures.append(f"batch {run['batch_id']}: {exc}"[:300])

        for i, run in enumerate(runs):
            if self.rate:
                await asyncio.sleep(max(0.0, began + i / self.rate - time.monotonic()))
            if self.late():
                break
            tasks.append(asyncio.create_task(one(run)))
        await asyncio.gather(*tasks)
        return {"latencies": latencies, "failures": failures[:20], "failed": len(failures)}

    async def upload(self, session, base: str, slot, run: dict, tokens) -> float:
        """POST one pulse run directory; the seconds the node API took to answer."""
        data = await asyncio.to_thread(self.pulse_archive, slot, run)
        url, began = f"{base}/v1/results?batch_id={run['batch_id']}", time.monotonic()
        async with session.post(url, data=data, headers=bearer(tokens[run["node_id"]])) as reply:
            text = await reply.text()
        if reply.status != 200 or not json.loads(text).get("ingested"):  # a 200 is JSON
            raise Failed(f"{reply.status} {text[:200]}")
        return round(time.monotonic() - began, 3)

    def pulse_archive(self, slot, run: dict) -> bytes:
        """A pulse's run directory as a node uploads it (tar.gz), started at the batch start."""
        lane, errors, rows = self.pulse_geo(slot, run)
        begin = run["start_at"].astimezone(UTC)
        moment = begin.replace(minute=begin.minute - begin.minute % 5, second=0, microsecond=0)
        geo, bins = [], Counter()
        for location, network, completed, successes, ms in rows:
            item = bucket(moment, successes, completed - successes, [(ms, successes)])
            geo.append(item | {"location": location, "network": network})
            bins[ms] += successes
        successes = synthetic.PULSE_REQUESTS - errors
        duration = 30 + run["batch_id"] % 10
        buckets = [run_bucket(moment, successes, errors, ((ms, n) for ms, n in bins.items() if n))]
        run_id = self.run_id("pulse", run["batch_id"], run["product_id"])
        summary = self.summary(run_id, lane, begin, duration, buckets)
        summary["config"] |= PULSE_SCOPE | {"requests": synthetic.PULSE_REQUESTS}
        ips = self.pulse_ips(slot, run, run_id, moment, successes)
        unique = len({row["ip"] for row in ips}) if ips else successes
        summary |= {"requested": synthetic.PULSE_REQUESTS, "unique_ips": unique}
        with tempfile.TemporaryDirectory(prefix="scale-pulse-") as temporary:
            directory = Path(temporary, "run")
            directory.mkdir()
            artifacts = {"ips": gzip_rows(directory / "ips.jsonl.gz", ips)}
            artifacts["geo"] = gzip_rows(directory / "geo.jsonl.gz", geo)
            summary |= {"stop_reason": "requests", "artifacts": artifacts}
            (directory / "summary.json").write_text(json.dumps(summary))
            return pack(directory, Path(temporary, "run.tar.gz")).read_bytes()

    def pulse_ips(self, slot, run: dict, run_id: str, moment: datetime, successes: int) -> list:
        """The ips.jsonl rows of a run: none for a pulse (the generator plans no load wave)."""
        return []

    def pulse_geo(self, slot, run: dict) -> tuple:
        """A pulse's lane, timeouts and (location, network, completed, successes, ms) rows."""
        (day, start), country = slot, self.positions[run["country"]]
        lane = self.model.lane(run["product_id"] - 1, country)
        errors, rows = self.model.pulse(lane, day, start.hour)
        places = self.locations, self.networks
        return lane, errors, [(places[0].get(a), places[1].get(b), *rest) for a, b, *rest in rows]

    # Steps 3 to 5.

    def release(self, day: int, start: datetime, step: dict) -> tuple[dict, bool]:
        async def released():
            db = Database(str(self.args.database), size=2)
            try:
                return await release_due(db, start + HOUR)
            finally:
                await db.close()

        return {"released": len(asyncio.run(released()))}, True

    def calculate(self, day: int, start: datetime, step: dict) -> tuple[dict, bool]:
        cli = ["proxy_index.cli", "calculate", "--database-file", str(self.args.database)]
        cli += ["--methodology", str(self.args.methodology), "--as-of", (start + HOUR).isoformat()]
        with tempfile.TemporaryDirectory(prefix="scale-calc-") as temporary:
            trace = Path(temporary, "statements.json")
            prefix = ["-m"] if self.pg_stat else [str(MEASURE), "trace", str(trace)]
            result = measure.run_child([sys.executable, *prefix, *cli, "--publish"])
            if trace.exists():
                result["statements"] = measure.top(json.loads(trace.read_text()))
        if result["exit"]:
            raise Failed(f"calculate exited with {result['exit']}")
        build = self.conn.execute("SELECT build_id FROM current_calculation").fetchone()
        return result | {"build_id": build[0]}, True

    def siteapi(self, day: int, start: datetime, step: dict) -> tuple[dict, bool]:
        """The wait until siteapi serves the new build, its build timings and peak RSS."""
        if not self.args.siteapi:
            return {"skipped": "no --siteapi"}, True
        build_id = self.state["current"]["steps"]["calculate"]["parts"][-1]["build_id"]
        return self.served(build_id), True

    def served(self, build_id) -> dict:
        """The wait until siteapi (--siteapi) serves build_id, its build timings and peak RSS."""
        deadline, health = time.monotonic() + self.args.siteapi_timeout, {}
        while not (reached := time.monotonic() >= deadline):
            health = healthz(self.args.siteapi)
            if (health.get("fingerprint") or {}).get("build_id") == build_id:
                break
            time.sleep(1)
        result = {"build_id": build_id, "reached": not reached, "status": health.get("status")}
        result |= {key: health.get(key) for key in ("buildMs", "loadMs", "assembleMs", "renderMs")}
        result["unserved"] = len(health.get("unserved") or []) + (health.get("unservedMore") or 0)
        if self.args.siteapi_pid:
            result["peak_rss_kb"] = measure.vm_hwm(self.args.siteapi_pid)
        return result
