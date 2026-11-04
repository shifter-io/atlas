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
child process, and wait for siteapi (--siteapi) to serve the new build. README.md describes the
steps, the measurements (measure.py) and the report. Progress is kept in the settings row
scale.cycle: past --max-minutes a run stops between steps (or inside the ingest and pulse steps),
and the same command continues it until N hours are done.

Guards: the database must be named proxy_scale, or the session's only schema must be proxy_scale
(target.py); it must hold a whole-day generator data set, and no test_runs row written by anything
but the generator and this driver (summary key scale_cycle).
"""

from __future__ import annotations

import argparse
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
import generate
import measure
import synthetic
import target
import writer
from aiohttp import web
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from synthetic import Model, address, apportion
from writer import BODY, DOWN, TARGET, Writer

from proxy_index import daily, storage
from proxy_index.calculated import store
from proxy_index.estimates import compute
from proxy_index.estimates.release import release_due
from proxy_index.orchestrator import batches
from proxy_index.orchestrator.app import build_app
from proxy_index.orchestrator.archive import pack
from proxy_index.orchestrator.db import Database
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.stats import LOCATION_FIELDS, NETWORK_FIELDS, Histogram
from proxy_index.storage import GEO_COUNTERS

DATABASE, SETTING, MARKER = "proxy_scale", "scale.cycle", "scale_cycle"
STEPS = ("ingest", "pulses", "release", "calculate", "siteapi")
HOUR = timedelta(hours=1)
MEASURE = Path(__file__).resolve().with_name("measure.py")
METHODOLOGY = Path(__file__).resolve().parents[2] / "configs" / "methodology.toml"
# The scope settings the generator gives daily tests and pulses (Writer.scope).
DAILY_SCOPE = {"concurrency": writer.DAILY_CONCURRENCY, "timeout_seconds": writer.TIMEOUT}
DAILY_SCOPE["requests_per_second"] = None
PULSE_SCOPE = DAILY_SCOPE | {"concurrency": writer.PULSE_CONCURRENCY}
DAILY_LENGTH = {"requests": None, "stop_rule": "unique_ratio", "min_sample": 1000}
DAILY_LENGTH["stall_seconds"] = 600
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


def guard(conn, expected: str = DATABASE):
    """The generator's settings and progress; refuse another database or foreign runs."""
    found = target.refusal(conn, expected)
    if found:
        raise Refused(f"{found}; this driver uses only {expected!r} {target.ALLOWED}")
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


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def work(session, base: str, token: str) -> None:
    """GET /v1/work as a node does: its queued runs start."""
    async with session.get(f"{base}/v1/work", headers=bearer(token)) as reply:
        reply.raise_for_status()


class Cycle:
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

    def summary(self, run_id: str, lane, started: datetime, duration: int, buckets) -> dict:
        """A run's summary.json (without its kind's keys), as the runner writes one."""
        bins, successes, errors = Counter(), 0, 0
        for item in buckets:
            bins.update(dict(item["latency_histogram"]["bins"]))
            successes, errors = successes + item["successes"], errors + item["timeouts"]
        body = self.writer.summary(successes, errors, bins, duration, {}, {})
        del body["config_overrides"], body[writer.MARKER]
        finished = (started + timedelta(seconds=duration)).isoformat()
        body |= {"run_id": run_id, "status": "completed", "target": self.writer.target}
        body |= {"started_at": started.isoformat(), "finished_at": finished, "buckets": buckets}
        body["latency_histogram"] = Histogram(bins).export()
        return body | {"config": self.writer.config(lane), MARKER: self.model.settings.seed}

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
        ips = gzip_rows(directory / "ips.jsonl.gz", [row for _, row in rows])
        release, places = self.writer.release(test.day), self.attributes(test)
        enriched = [row | {"dbip_release": release, "enrichment": places[n]} for n, row in rows]
        report = gzip_rows(directory / f"enriched-{release}.jsonl.gz", enriched)
        report |= {"run_id": run_id, "release": {"sha256": release, "synthetic": True}}
        (directory / f"enrichment-{release}.json").write_text(json.dumps(report))
        bins, errors = (
            self.model.latency(test.lane.network),
            apportion(test.errors, [1] * test.buckets),
        )
        buckets = [
            run_bucket(
                test.started_at + timedelta(minutes=5 * b),
                good,
                bad,
                zip(bins, apportion(good, synthetic.WEIGHTS), strict=True),
            )
            for b, (good, bad) in enumerate(zip(successes, errors, strict=True))
        ]
        summary = self.summary(run_id, test.lane, test.started_at, test.buckets * 300 - 1, buckets)
        summary["config"] |= DAILY_SCOPE | DAILY_LENGTH | {"target_url": self.writer.target["url"]}
        summary |= {"requested": None, "unique_ips": test.ips, "stop_reason": "unique_ratio"}
        summary |= {"artifacts": {"ips": ips}, daily.MARKER: daily.DAILY}
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

    def pulses(self, day: int, start: datetime, step: dict) -> tuple[dict, bool]:
        tokens = {}
        slugs = {self.writer.regions[code][1] for code in self.positions}
        for n, network in enumerate(self.model.settings.networks):
            product = self.model.formulas[n].get("product", "residential")
            for slug in sorted(slugs):
                node = issue_token(
                    self.args.database, provider=network, product=product, region=slug
                )
                tokens[node["node_id"]] = node["token"]
        part = asyncio.run(self.upload_hour(day, start, tokens))
        runs = self.conn.cursor(row_factory=dict_row).execute(RUNS, (TARGET, start)).fetchall()
        part["planned"] = len(runs)
        part["statuses"] = dict(Counter(run["status"] for run in runs))  # the runs' final states
        return part, not any(run["status"] in PENDING for run in runs)

    @contextlib.asynccontextmanager
    async def node_api(self, db: Database):
        """The node API's base URL: --node-api, or an orchestrator app served on loopback."""
        if self.args.node_api:
            yield self.args.node_api.rstrip("/")
            return
        app = build_app(str(self.args.database), with_scheduler=False, db=db, admin_credential=None)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            yield f"http://127.0.0.1:{runner.addresses[0][1]}"
        finally:
            await runner.cleanup()

    async def upload_hour(self, day: int, start: datetime, tokens: dict[int, str]) -> dict:
        """Plan the hour's slot as the scheduler does, then upload its runs at the pulse rate."""
        db = Database(str(self.args.database), size=4)
        try:
            await batches.pulse_plan(db, start)
            # Concurrent first events of a month can race on CREATE TABLE IF NOT EXISTS of
            # the test_events partition; the app shares this pool, which creates it once here.
            await db.ensure_partition("test_events", datetime.now(UTC))
            runs = self.conn.cursor(row_factory=dict_row).execute(RUNS, (TARGET, start))
            runs = [run for run in runs.fetchall() if run["status"] in PENDING]
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
        (day, start), country = slot, self.positions[run["country"]]
        lane = self.model.lane(run["product_id"] - 1, country)
        errors, rows = self.model.pulse(lane, day, start.hour)
        begin = run["start_at"].astimezone(UTC)
        moment = begin.replace(minute=begin.minute - begin.minute % 5, second=0, microsecond=0)
        geo, bins = [], Counter()
        for location, network, completed, successes, ms in rows:
            item = bucket(moment, successes, completed - successes, [(ms, successes)])
            item |= {"location": self.locations.get(location)}
            geo.append(item | {"network": self.networks.get(network)})
            bins[ms] += successes
        successes = synthetic.PULSE_REQUESTS - errors
        duration = 30 + run["batch_id"] % 10
        buckets = [run_bucket(moment, successes, errors, ((ms, n) for ms, n in bins.items() if n))]
        run_id = self.run_id("pulse", run["batch_id"], run["product_id"])
        summary = self.summary(run_id, lane, begin, duration, buckets)
        summary["config"] |= PULSE_SCOPE | {"requests": synthetic.PULSE_REQUESTS}
        summary |= {"requested": synthetic.PULSE_REQUESTS, "unique_ips": successes}
        with tempfile.TemporaryDirectory(prefix="scale-pulse-") as temporary:
            directory = Path(temporary, "run")
            directory.mkdir()
            artifacts = {"ips": gzip_rows(directory / "ips.jsonl.gz", [])}
            artifacts["geo"] = gzip_rows(directory / "geo.jsonl.gz", geo)
            summary |= {"stop_reason": "requests", "artifacts": artifacts}
            (directory / "summary.json").write_text(json.dumps(summary))
            return pack(directory, Path(temporary, "run.tar.gz")).read_bytes()

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
                result["statements"] = json.loads(trace.read_text())
        if result["exit"]:
            raise Failed(f"calculate exited with {result['exit']}")
        build = self.conn.execute("SELECT build_id FROM current_calculation").fetchone()
        return result | {"build_id": build[0]}, True

    def siteapi(self, day: int, start: datetime, step: dict) -> tuple[dict, bool]:
        """The wait until siteapi serves the new build, its build timings and peak RSS."""
        if not self.args.siteapi:
            return {"skipped": "no --siteapi"}, True
        build_id = self.state["current"]["steps"]["calculate"]["parts"][-1]["build_id"]
        deadline, health = time.monotonic() + self.args.siteapi_timeout, {}
        while not (reached := time.monotonic() >= deadline):
            health = healthz(self.args.siteapi)
            if json.loads(health.get("fingerprint") or "{}").get("build_id") == build_id:
                break
            time.sleep(1)
        result = {"build_id": build_id, "reached": not reached, "status": health.get("status")}
        result |= {key: health.get(key) for key in ("buildMs", "loadMs", "assembleMs", "renderMs")}
        result["unserved"] = len(health.get("unserved") or [])
        if self.args.siteapi_pid:
            result["peak_rss_kb"] = measure.vm_hwm(self.args.siteapi_pid)
        return result, True

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
