"""Local Shifter collection: one census sweep and serial 30-minute pulse sweeps.

Uses the production runner, enrichment, ingestion and batch transitions. Countries
are serial within each of the two lanes (census and pulse); at most 200 proxy
connections in total. An overdue pulse sweep waits for the previous sweep to finish.
Run from the framework checkout; credentials remain in .secrets.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import secrets
import signal
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

from milf_framework import __version__, storage
from milf_framework.enrichment import LocalEnricher
from milf_framework.node import Active, NodeAgent, provider_factory
from milf_framework.orchestrator import batches
from milf_framework.orchestrator.control import write_settings
from milf_framework.orchestrator.db import Database
from milf_framework.orchestrator.jobs import register_jobs
from milf_framework.orchestrator.scheduler import Scheduler

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("shifter-local")


def now():
    return datetime.now(UTC)


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n")
    temporary.replace(path)


class LocalAgent(NodeAgent):
    """Call the same orchestrator transitions in-process, without an HTTP server."""

    async def _call(self, method, path, **options):
        body = options.get("json", {})
        if path == "/v1/progress":
            return await batches.progress(
                self.db, self.identity, body["batch_id"], body["counters"]
            ) or {"stop": True}
        if path == "/v1/failed":
            return {"status": await batches.fail_run(
                self.db, self.identity, body["batch_id"], body["error"]
            )}
        if path == "/v1/heartbeat":
            async with self.db.connection() as conn:
                await conn.execute(
                    "UPDATE nodes SET status='online',last_heartbeat=now(),version=%s,"
                    "open_conns=%s,req_per_s=%s,rss_mb=%s WHERE node_id=%s",
                    (__version__, body["open_conns"], body["req_per_s"], body["rss_mb"],
                     self.node_id),
                )
            return {"ok": True}
        raise ValueError("Unsupported local node operation")

    async def upload(self, batch_id, directory):
        await batches.begin_upload(self.db, self.identity, batch_id)
        # Keep portable artifacts, including unsuccessful provider measurements.
        summary = json.loads((directory / "summary.json").read_text())
        for attempt in range(8):
            try:
                imported = await asyncio.to_thread(
                    storage.ingest, directory, self.db.credentials_file, batch_id=batch_id
                )
                status = await batches.finish_run(
                    self.db, self.identity, batch_id, summary, imported["run_key"]
                )
                LOG.info("stored batch=%s status=%s requests=%s successes=%s",
                         batch_id, status, summary["completed"], summary["successes"])
                return {"status": status, "run_key": imported["run_key"]}
            except (OSError, storage.psycopg.OperationalError) as exc:
                LOG.warning("ingest retry batch=%s error=%s", batch_id, type(exc).__name__)
                if attempt == 7:
                    raise
                await asyncio.sleep(min(2 ** attempt, 60))


async def collect(args, state, folder, stop):
    database_file = str(ROOT / ".secrets/database.toml")
    with storage.connect(database_file) as conn:
        if conn.execute("SELECT current_database()").fetchone()[0] != "proxy_reviews":
            raise ValueError("Expected the main local proxy_reviews database")
        if not conn.execute("SELECT pg_try_advisory_xact_lock(817452919)").fetchone()[0]:
            raise ValueError("Another setup is running")
        # The cloud mapping stays unchanged. These measurements have their own honest origin.
        region = conn.execute(
            "INSERT INTO runner_regions(region_id,slug,cloud,pattern) "
            "VALUES (100,'local-mac','local','local-mac') "
            "ON CONFLICT(slug) DO UPDATE SET cloud='local' RETURNING region_id"
        ).fetchone()[0]
        conn.execute("INSERT INTO providers VALUES ('shifter') ON CONFLICT DO NOTHING")
        product = conn.execute(
            "INSERT INTO products(provider,name) VALUES ('shifter','residential') "
            "ON CONFLICT(provider,name) DO UPDATE SET name=excluded.name RETURNING product_id"
        ).fetchone()[0]
        node = conn.execute(
            "INSERT INTO nodes(region_id,provider,product,host,token_hash,status,last_heartbeat) "
            "VALUES (%s,'shifter','residential',%s,%s,'online',now()) "
            "ON CONFLICT(region_id,provider,product) DO UPDATE SET host=excluded.host,"
            "status='online',last_heartbeat=now() RETURNING node_id",
            (region, socket.gethostname(), hashlib.sha256(secrets.token_bytes(32)).hexdigest()),
        ).fetchone()[0]
        countries = [r[0].strip() for r in conn.execute(
            "SELECT country_code FROM countries ORDER BY country_code"
        )]
        # Create monthly partitions before the two lanes can race their first writes.
        for table in ("test_events", "run_buckets", "ip_observations", "run_geo_buckets"):
            storage.partition(conn, table, now())
        conn.execute(
            "UPDATE targets SET census_concurrency=100,pulse_concurrency=100,"
            "pulse_requests=100,pulse_interval_minutes=30 "
            "WHERE target_id IN ('generic_http','generic_https')"
        )
        # This controller owns planning: the production fan-out planners must remain off.
        write_settings(conn, {"scheduler.census_enabled": False,
                              "scheduler.pulse_enabled": False,
                              "reports.dir": str(ROOT / "reports/shifter-local")})
    db = Database(database_file)
    scheduler = Scheduler(db)
    register_jobs(scheduler, db, database_file, ROOT / "configs/methodology.toml")
    for name in ("census_plan", "pulse_plan", "retention"):
        scheduler.tasks.pop(name)
    # Reports are generated in batches every minute rather than after every 100 requests.
    db.on_close.clear()
    scheduler.tasks["reports"].every_s = 60
    scheduler.start()
    factory = provider_factory(ROOT / "configs/shifter.toml", ROOT / ".secrets/credentials.toml")
    factory("ae")
    until = datetime.fromisoformat(state["pulse_until"])
    census_slot = datetime.fromisoformat(state["started_at"])
    with LocalEnricher() as enricher:
        agent = LocalAgent("", "", provider="shifter", product="residential",
                           region="local-mac", proxy_factory=factory,
                           work_dir=ROOT / "results/shifter-local", enricher=enricher,
                           mmdb=enricher.path, progress_interval=15)
        agent.db, agent.node_id = db, node
        agent.identity = {"node_id": node, "product_id": product}
        heartbeat = asyncio.create_task(agent._heartbeats(stop))

        async def stop_active():
            await stop.wait()
            for active in list(agent.active.values()):
                active.stop.set()

        stop_watcher = asyncio.create_task(stop_active())

        async def one(kind, country, target, slot):
            if stop.is_set():
                return
            async with db.transaction() as conn:
                row = await (await conn.execute(
                    "INSERT INTO test_batches(kind,country_code,region_id,target_id,"
                    "scheduled_for,start_at) VALUES (%s,%s,%s,%s,%s,now()) "
                    "ON CONFLICT DO NOTHING RETURNING batch_id",
                    (kind, country, region, target, slot),
                )).fetchone()
                if row is None:
                    return
                batch_id = row["batch_id"]
                await conn.execute(
                    "INSERT INTO batch_runs(batch_id,product_id,node_id,status) "
                    "VALUES (%s,%s,%s,'running')", (batch_id, product, node)
                )
            full = kind == "census" and args.census_requests == 0
            assignment = {"batch_id": batch_id, "kind": kind, "country_code": country,
                          "target_id": target, "timeout_ms": 15000, "concurrency": 100,
                          "requests": None if full else 100,
                          "stop_rule": "unique_ratio" if full else "count",
                          "start_at": now().isoformat()}
            active = Active(assignment)
            agent.active[batch_id] = active
            await agent.execute(active)

        async def census():
            for country in countries:
                if stop.is_set():
                    break
                await one("census", country, "generic_http", census_slot)
            LOG.info("census sweep finished")

        async def pulses():
            slot = datetime.fromisoformat(state.get("pulse_slot", state["started_at"]))
            while slot < until and not stop.is_set():
                delay = (slot - now()).total_seconds()
                if delay > 0:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), delay)
                if stop.is_set() or now() >= until:
                    break
                LOG.info("pulse sweep started scheduled_for=%s", slot.isoformat())
                for country in countries:
                    if stop.is_set():
                        break
                    reference_start = now()
                    await one("pulse", country, "generic_http", slot)
                    await one("pulse", country, "generic_https", slot)
                    if country == "AE" and not stop.is_set():
                        # Keep the reference pulse clear of its own load wave, as production does.
                        delay = max(0, 60 - (now() - reference_start).total_seconds())
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(stop.wait(), delay)
                        async with db.transaction() as conn:
                            await conn.execute(
                                "INSERT INTO load_references(day,region_id,country_code,basis) "
                                "VALUES (%s,%s,'AE','override') ON CONFLICT DO NOTHING",
                                (slot.date(), region),
                            )
                        await one("load", "AE", "generic_http", slot)
                LOG.info("pulse sweep finished scheduled_for=%s", slot.isoformat())
                # Never create concurrent sweeps or pretend late measurements ran in old slots.
                slot += timedelta(minutes=30)
                if slot < now():
                    slot = datetime.fromtimestamp((now().timestamp() // 1800 + 1) * 1800, UTC)
                    LOG.warning("sweep exceeded interval; next sweep=%s", slot.isoformat())
                state["pulse_slot"] = slot.isoformat()
                save(folder / "state.json", state)

        try:
            await asyncio.gather(census(), pulses())
            await scheduler.run_now("reports")
            await scheduler.run_now("calculate")
        finally:
            stop.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, stop_watcher, return_exceptions=True)
            await scheduler.stop()
            async with db.connection() as conn:
                await conn.execute(
                    "UPDATE nodes SET status='offline',open_conns=0 WHERE node_id=%s", (node,)
                )
            await db.close()


async def main(args, folder):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    path = folder / "state.json"
    state = json.loads(path.read_text()) if path.exists() else {
        "started_at": now().isoformat(), "pulse_until": args.until,
        "census_requests": args.census_requests, "concurrency": 100,
        "pulse_requests": 100, "pulse_interval_minutes": 30,
        "origin": "local-mac", "database": "proxy_reviews",
    }
    if state["census_requests"] != args.census_requests:
        raise ValueError("Census settings differ from the saved run")
    save(path, state)
    await collect(args, state, folder, stop)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--until", required=True, help="ISO timestamp: stop starting pulse sweeps")
    parser.add_argument("--census-requests", type=int, choices=(0, 100), default=100,
                        help="100 for bounded sampling; 0 for the full unique-ratio census")
    args = parser.parse_args()
    if datetime.fromisoformat(args.until).tzinfo is None:
        parser.error("--until must include a timezone")
    os.chdir(ROOT)
    os.umask(0o077)
    folder = ROOT / ".state/shifter-local"
    folder.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    with (folder / "runner.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (folder / "runner.pid").write_text(str(os.getpid()) + "\n")
        asyncio.run(main(args, folder))
