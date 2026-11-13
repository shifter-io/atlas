"""BROWSER-MODEL-1: modelled browser pulse bundles from BP-VERIFY-2's measured numbers (#147).

Browser pulse data does not exist in production yet. BP-VERIFY-2 measured one network-hour
(Shifter, US, one host with 12 browsers, the 18:00 UTC slot): 8 targets, 600 page loads, 2.79 MB
per load, 1.674 GB per network-hour. settings() is that hour's settings file. bundle() writes one
planned run's bundle through the real runner and seal: the measured tries, the exact outcome
counts and bytes drawn around the measured mean, its summary marked scale_replay_model and
scale_replay (the replay's id).
browser_step() adds the modelled browser hour to a replay slice: one node per network in the
browser region, the hour planned by orchestrator/browser.py's planner with these settings given
in-process (no settings row is written), every bundle uploaded through the in-process node API
into the real ingest. The archive is a temporary local directory (local_archive), never the
object store.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import random
import shutil
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import aiohttp
from cycle_steps import HOUR, Failed, bearer, serve
from synthetic import apportion
from target import REPLAY_MARKER

from atlas import ProxyConfig, RunConfig, RunOptions, run
from atlas.browser import contracts as bc
from atlas.browser import release
from atlas.browser.rawfiles import Batch, FileRecorder, seal
from atlas.browser.run import BundleSink
from atlas.browser.settings import BrowserSettings, load_settings
from atlas.models import Outcome
from atlas.orchestrator import browser_ingest
from atlas.orchestrator.browser import browser_plan
from atlas.orchestrator.db import Database
from atlas.orchestrator.nodes import issue_token
from atlas.storage import connect

MODEL = "BP-VERIFY-2"
MARKER = "scale_replay_model"
# The logical host of the modelled nodes: a host name is browser-<letters and digits>.
HOST = "browser-scalereplay"
NETWORKS = ("scale-replay/residential",)
BROWSERS = 12
PAGE_TIMEOUT_S = 45
MB = 1_000_000
UPLOADS = 2  # bundles uploaded at once, as a host's outbox sends them
PENDING = ("queued", "running", "uploading")
LOAD_MS = 2500  # a modelled try's page load when its plan gives none
CAMOUFOX = {
    "release": release.RELEASE,
    "engine_version": release.ENGINE_VERSION,
    "user_agent": bc.user_agent(release.ENGINE_VERSION, "linux"),
}


@dataclass(frozen=True)
class Measured:
    """One target's #147 hour: tries, mean MB per load, tries per error code (None: ok)."""

    tries: int
    mean_mb: float
    outcomes: dict[str | None, int]
    inputs: tuple[Any, ...]


# Outcomes besides ok, challenge, block and timeout are #147's other outcomes; its one
# instagram_web error outside no_content is not named there and counts as browser_error.
MEASURED = {
    "amazon_pdp": Measured(100, 4.51, {None: 93, "timeout": 7}, ("0306406152",)),
    "google_search": Measured(
        100, 1.52, {None: 39, "challenge": 56, "timeout": 5}, ("example query",)
    ),
    "linkedin_public": Measured(
        100, 1.35, {None: 12, "login_wall": 81, "no_content": 7}, ("example",)
    ),
    "instagram_web": Measured(
        100,
        3.69,
        {None: 48, "timeout": 1, "no_content": 50, "browser_error": 1},
        ({"site": "instagram", "handle": "example"}, {"site": "x", "handle": "example"}),
    ),
    "cloudflare_challenge": Measured(
        100,
        3.01,
        {None: 29, "challenge": 3, "blocked": 20, "timeout": 1, "no_content": 47},
        ({"url": "https://example.com/protected", "expect": "Example Domain"},),
    ),
    "ai_overview": Measured(34, 1.66, {"challenge": 23, "no_answer": 11}, ("example question",)),
    "ai_chatgpt": Measured(33, 2.16, {"timeout": 2, "no_content": 31}, ("example question",)),
    "ai_gemini": Measured(
        33, 4.16, {None: 31, "timeout": 1, "browser_error": 1}, ("example question",)
    ),
}
HEADER = f"""schema = 2
enabled = true

[schedule]
countries = ["US"]
lead_seconds = 60

[browser]
browsers_per_host = {BROWSERS}
recycle_after = 25
page_timeout_s = {PAGE_TIMEOUT_S}
headless = true
block_resources = ["image", "media", "font"]
screenshots = "none"
locale = "en-US"

[outbox]
dir = "outbox"
max_pending_gb = 20
"""
RUNS = (
    "SELECT br.batch_id,br.product_id,br.node_id,br.status,trim(b.country_code) AS country,"
    "b.target_id,b.scheduled_for,b.start_at,p.provider,p.name AS product FROM batch_runs br "
    "JOIN test_batches b USING(batch_id) JOIN products p USING(product_id) "
    "WHERE b.kind='browser' AND b.scheduled_for>=%s AND b.scheduled_for<%s "
    "ORDER BY b.start_at,br.batch_id,br.product_id"
)
ROWS = (
    "WITH b AS (SELECT batch_id FROM test_batches WHERE kind='browser' AND scheduled_for>=%s "
    "AND scheduled_for<%s), r AS (SELECT run_key FROM test_runs WHERE batch_id IN "
    "(SELECT batch_id FROM b)) SELECT (SELECT count(*) FROM b),(SELECT count(*) FROM batch_runs "
    "WHERE batch_id IN (SELECT batch_id FROM b)),(SELECT count(*) FROM r),"
    "(SELECT count(*) FROM run_buckets WHERE run_key IN (SELECT run_key FROM r)),"
    "(SELECT count(*) FROM ip_observations WHERE run_key IN (SELECT run_key FROM r)),"
    "(SELECT count(*) FROM run_geo_buckets WHERE run_key IN (SELECT run_key FROM r))"
)
TABLES = ("test_batches", "batch_runs", "test_runs", "run_buckets")
TABLES += ("ip_observations", "run_geo_buckets")


def _toml(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key} = {json.dumps(text)}" for key, text in value.items()) + "}"
    return json.dumps(value)


def settings(every_minutes: int = 60, networks=NETWORKS) -> str:
    """The modelled settings file (K-BP3): #147's 8 targets and tries, US, one host."""
    parts = [HEADER]
    for target, measured in MEASURED.items():
        inputs = ", ".join(_toml(value) for value in measured.inputs)
        parts.append(
            f'\n[[target]]\nid = "{target}"\ntries = {measured.tries}\n'
            f"every_minutes = {every_minutes}\ninputs = [{inputs}]\n"
        )
    parts.append(f'\n[[host]]\nname = "{HOST}"\nnetworks = {json.dumps(list(networks))}\n')
    return "".join(parts)


def load(text: str, directory: Path) -> BrowserSettings:
    """The settings text as load_settings checks and returns it."""
    path = directory / "browser-settings.toml"
    path.write_text(text)
    return load_settings(path)


def plan(target: str, key: str) -> list[tuple[str | None, int]]:
    """(error code, bytes) per try: the measured counts shuffled, bytes drawn around the mean
    and summing to mean x tries."""
    measured, rng = MEASURED[target], random.Random(key)
    codes = [code for code, count in measured.outcomes.items() for _ in range(count)]
    rng.shuffle(codes)
    weights = [rng.randint(50, 150) for _ in codes]
    sizes = apportion(round(measured.mean_mb * MB * len(codes)), weights)
    return list(zip(codes, sizes, strict=True))


class ModelTransport:
    """The runner's transport of a modelled run: try i is plan[i], recorded as a host would."""

    def __init__(self, config: RunConfig, *, tries, inputs, recorder: FileRecorder) -> None:
        self.tries, self.inputs, self.recorder = tries, inputs, recorder

    async def __aenter__(self) -> ModelTransport:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, target, *, context) -> Outcome:
        i = context.request_index
        code, size, ms, shot = (*self.tries[i], LOAD_MS, 0)[:4]
        verdict = bc.CODE_VERDICTS[code] if code else "ok"
        status = None if code == "timeout" else 200
        timings = {"queue": 0, "context": 100, "navigate": max(0, ms - 500), "settle": 500}
        timings["total"] = ms + 100
        record = bc.try_record(
            i=i,
            input=self.inputs[i % len(self.inputs)],
            source=None,
            verdict=verdict,
            error=code,
            status=status,
            final_url=None,
            timings_ms=timings,
            bytes_down_est=size,
            reason=None,
            screenshot=bc.bundle_members().screenshot(i) if shot else None,
        )
        # A screenshot of `shot` incompressible bytes: the bundle bytes a failure costs.
        image = hashlib.shake_256(f"screenshot:{i}".encode()).digest(shot) if shot else None
        self.recorder.try_done(record, image)
        return Outcome(float(ms), error=code, status=status, verdict=verdict, bytes_down=size)


def bundle(run_row: dict, target: str, seed: Any, out: Path, tries=None) -> Path:
    """One planned browser run's bundle (K-BP4) in the outbox `out`; its tar.gz. `tries`: (error
    code, bytes[, page load ms, screenshot bytes]) per try, by default plan()'s; host: the row's."""
    tries = tries or plan(target, f"{seed}:{run_row['batch_id']}:{run_row['product_id']}")
    host = run_row.get("host") or HOST
    batch = Batch(run_row["batch_id"], run_row["country"], run_row["scheduled_for"], host)
    recorder = FileRecorder(batch, hashlib.sha256(settings().encode()).hexdigest())
    sink = BundleSink(Path(out) / "runs", recorder)
    config = RunConfig(
        target,
        ProxyConfig("http", "unused.invalid", 8080),
        requests=len(tries),
        concurrency=BROWSERS,
        timeout_seconds=float(PAGE_TIMEOUT_S),
        provider=run_row["provider"],
        product=run_row["product"],
        requested_country=run_row["country"].lower(),
        runner_location=bc.BROWSER_REGION,
    )
    began, start = time.perf_counter(), run_row["start_at"].timestamp()
    transport = partial(
        ModelTransport, tries=tries, inputs=MEASURED[target].inputs, recorder=recorder
    )
    clock = lambda: start + time.perf_counter() - began  # noqa: E731
    asyncio.run(run(config, sink, RunOptions(transport_factory=transport, wall_clock=clock)))
    summary = json.loads((sink.directory / "summary.json").read_text()) | {MARKER: MODEL}
    summary[REPLAY_MARKER] = seed  # the replay's id: every run it stores carries it
    (sink.directory / "summary.json").write_text(json.dumps(summary))
    sealed = seal(sink.directory, batch, summary, recorder.settings_sha256, CAMOUFOX, outbox=out)
    return sealed.archive


@contextlib.contextmanager
def local_archive(root: Path):
    """For the block, browser ingest archives every bundle under root (LocalArchive), whatever
    the settings say: the replay never writes to the object store or a settings row."""
    store = browser_ingest._store

    def local(upload, key, meta, replay):
        config = upload.config | {"browser.archive_dir": str(root)}
        return store(replace(upload, config=config), key, meta, replay)

    browser_ingest._store = local
    try:
        yield
    finally:
        browser_ingest._store = store


def browser_step(steps, day: int, start: datetime, state: dict) -> tuple[dict, bool]:
    """The modelled browser hour of a replay slice: (runs, loads, bytes, rows per table, done)."""
    database, model = steps.args.database, steps.model
    nodes = {}
    for n, provider in enumerate(model.settings.networks):
        product = model.formulas[n].get("product", "residential")
        node = issue_token(
            database, provider=provider, product=product, region=bc.BROWSER_REGION, host=HOST
        )
        nodes[node["node_id"]] = node
    networks = [f"{node['provider']}/{node['product']}" for node in nodes.values()]
    done = state.setdefault("uploaded", [])
    hour = Hour(database, model.settings.seed, start, done)
    part = asyncio.run(hour.drive(settings(networks=networks), nodes))
    with connect(database) as conn:
        counts = conn.execute(ROWS, (start, start + HOUR)).fetchone()
    part["rows"] = dict(zip(TABLES, counts, strict=True))
    return part, part["failed"] == 0


@dataclass
class Hour:
    """One modelled browser hour on a database: plan, then upload each pending run's bundle."""

    database: Any
    seed: Any
    start: datetime
    done: list

    async def drive(self, text: str, nodes: dict[int, dict]) -> dict:
        work = Path(tempfile.mkdtemp(prefix="scale-browser-"))
        db = Database(str(self.database), size=4)
        try:
            with local_archive(work / "archive"):
                planned = await self.plan(db, text, work)
                await db.ensure_partition("test_events", datetime.now(UTC))
                async with serve(self.database, db) as base, aiohttp.ClientSession() as http:
                    part = await self.upload_all(db, http, base, nodes, work)
        finally:
            await db.close()
            shutil.rmtree(work, ignore_errors=True)
        return part | {"planned": planned.get("created", 0)}

    async def plan(self, db: Database, text: str, work: Path) -> dict:
        """The planner's result for the hour: browser_plan at its start with these settings."""
        return await browser_plan(db, self.start, load(text, work))

    def bundle(self, row: dict, outbox: Path) -> Path:
        return bundle(row, row["target_id"], self.seed, outbox)

    def size(self, row: dict) -> tuple[int, int]:  # an uploaded run's (page loads, bytes)
        measured = MEASURED[row["target_id"]]
        return measured.tries, round(measured.mean_mb * MB * measured.tries)

    async def upload_all(self, db, http, base: str, nodes: dict, work: Path) -> dict:
        for node in nodes.values():
            body = {key: node[key] for key in ("provider", "product")}
            body["host"] = node.get("host") or HOST
            body["region"] = bc.BROWSER_REGION
            async with http.post(f"{base}/v1/register", json=body, headers=bearer(node["token"])):
                pass
            async with http.get(f"{base}/v1/work", headers=bearer(node["token"])) as reply:
                reply.raise_for_status()
        async with db.connection() as conn:
            rows = await (await conn.execute(RUNS, (self.start, self.start + HOUR))).fetchall()
        todo = [row for row in rows if row["status"] in PENDING and row["node_id"] in nodes]
        todo = [row for row in todo if self.key(row) not in self.done]
        gate, failures = asyncio.Semaphore(UPLOADS), []
        part = {"runs": 0, "loads": 0, "bytes": 0}

        async def one(row):
            async with gate:
                try:
                    await self.upload(http, base, row, nodes[row["node_id"]]["token"], work)
                except (Failed, aiohttp.ClientError, TimeoutError, ValueError, OSError) as exc:
                    failures.append(f"batch {row['batch_id']}: {exc}"[:300])
                    return
            loads, size = self.size(row)
            part["runs"] += 1
            part["loads"] += loads
            part["bytes"] += size

        await asyncio.gather(*(one(row) for row in todo))
        return part | {"failed": len(failures), "failures": failures[:20]}

    @staticmethod
    def key(row: dict) -> list[int]:
        return [row["batch_id"], row["product_id"]]

    async def upload(self, http, base: str, row: dict, token: str, work: Path) -> None:
        """POST one run's bundle as a host's outbox does (K-BP7)."""
        path = await asyncio.to_thread(self.bundle, row, work / "outbox")
        data = path.read_bytes()
        path.unlink()
        path.with_name(path.name.replace(".tar.gz", ".json")).unlink()
        headers = bearer(token) | {
            "Content-Type": bc.CONTENT_TYPE,
            bc.HEADER_SHA256: hashlib.sha256(data).hexdigest(),
            bc.HEADER_BUNDLE_ID: path.name.removesuffix(".tar.gz"),
        }
        url = f"{base}/v1/results?batch_id={row['batch_id']}"
        async with http.post(url, data=data, headers=headers) as reply:
            text = await reply.text()
        if reply.status != 200 or not json.loads(text).get("ingested"):
            raise Failed(f"{reply.status} {text[:200]}")
        self.done.append(self.key(row))
