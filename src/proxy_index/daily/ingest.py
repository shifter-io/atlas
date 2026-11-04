"""`daily ingest`: one collected daily test into the database, then its TAPE-1 events.

Runs on the Mac (the database is there) on a campaign root downloaded and checked with
`campaign collect`. Each network x region cell's round is checked with `rounds.verify_round`;
each country that completed and reconciles becomes an ingestible run directory under
`<root>/runs/<run_id>/` (the scheduler's measurement as `summary.json`, and its raw observation
chunks aggregated into `ips.jsonl.gz` per 5-minute bucket and IP, the rows the runner's JSON
sink writes), enriched with DB-IP (`enrichment.enrich_run`) and imported with `storage.ingest`.
Then each cell gets `daily_test_started` and `daily_test_finished` (done only when every due
country was measured and imported, failed otherwise). A database error fails its country or
its cell's events, never the other cells.

A rerun is safe: a converted run is reused, `storage.ingest` replays as a no-op, and a cell
whose events are recorded in `<root>/daily-ingest.json` gets no second pair. So the first pair
emitted is authoritative: a rerun that later imports a country that failed (or a network whose
product had no row yet, which defers its events to the rerun) adds the data, never a corrected
event. The report holds names and counts only, never an address.

With `formulas` (the formulas/*.json directory), every imported country's 24 estimated hours
are stored too (estimates.compute; a rerun replaces only unreleased hours) and the ended hours
are released (estimates.release). A country without an estimate is an issue of its cell's
`estimate_issues` and makes the report incomplete, never a failed test.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from .. import storage
from ..archive import rawfiles
from ..campaign.tick import load_spec
from ..enrichment import enrich_run
from ..estimates import compute, release
from ..onboarding.files import atomic_json, digest, read_json
from ..onboarding.manifest import load, region_countries
from ..onboarding.rounds import round_directory, verify_round
from ..onboarding.scheduler import job_directory
from ..orchestrator import events
from ..orchestrator.db import Database
from ..regions import REGION_IDS
from ..stats import bucket_of, iso
from . import DAILY, MARKER

REPORT = "daily-ingest.json"
ROUND = 0  # a daily test is a one-slot campaign: round 00 only


def _aggregate(folder: Path, run_id: str) -> list[dict]:
    """The country's observations as IP rows, one per 5-minute bucket and IP."""
    seen: dict[tuple[int, str], list] = {}
    for chunk in sorted(folder.glob("observations-*.jsonl.gz")):
        with gzip.open(chunk, "rt", encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                at = datetime.fromisoformat(row["observed_at"]).timestamp()
                key = (bucket_of(at), row["ip"])
                entry = seen.get(key)
                if entry is None:
                    seen[key] = [1, at, at]
                else:
                    entry[0] += 1
                    entry[1], entry[2] = min(entry[1], at), max(entry[2], at)
    return [
        {
            "schema_version": "1",
            "run_id": run_id,
            "ip": ip,
            "bucket_start": iso(bucket),
            "count": count,
            "first_seen": iso(first),
            "last_seen": iso(last),
        }
        for (bucket, ip), (count, first, last) in sorted(seen.items())
    ]


def convert_country(folder: Path, runs: Path) -> Path:
    """An ingestible run directory from one round country folder; returns its path."""
    measured = read_json(folder / "summary.json")["measurement"]
    if not measured:
        raise ValueError("the country has no measurement")
    directory = runs / measured["run_id"]
    if (directory / "summary.json").exists():
        return directory  # converted by an earlier attempt; summary.json is written last
    rows = _aggregate(folder, measured["run_id"])
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    ips = directory / "ips.jsonl.gz"
    with ips.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as out:
        for row in rows:
            out.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
    ips.chmod(0o600)
    artifacts = {"ips": {"file": ips.name, "rows": len(rows), "sha256": digest(ips)}}
    atomic_json(directory / "summary.json", measured | {"artifacts": artifacts, MARKER: DAILY})
    return directory


def _import(directory: Path, credentials: Path, mmdb, spool: Path | None) -> str | None:
    """Enrich and import a run; then keep its raw files when `spool` is set. The import has
    committed by then, so a spool failure is returned (its exception type), never raised."""
    try:
        enrich_run(directory, mmdb)
    except FileExistsError:
        pass  # enriched by an earlier attempt
    storage.ingest(directory, credentials)
    if not spool:  # the raw files this update used (ARCHIVE-1: 30 days here, then Spaces)
        return None
    name = f"daily-{directory.name}.tar.gz"
    return rawfiles.try_spool(spool, datetime.now(UTC), name, directory, move=False)


def _times(jobs: list[dict]) -> tuple[str | None, str | None]:
    started = [j["started_at"] for j in jobs if j.get("started_at")]
    finished = [j["finished_at"] for j in jobs if j.get("finished_at")]
    return (min(started) if started else None), (max(finished) if finished else None)


def _estimate(cell: dict, formula: dict | None, run_id: str, country: str, credentials) -> None:
    if formula is None:
        return
    try:
        compute.estimate_run(credentials, run_id, country, formula)
    except (KeyError, ValueError, ZeroDivisionError, psycopg.Error) as exc:
        cell["estimate_issues"].append(f"{country}: no estimates ({type(exc).__name__})")
        return
    cell["estimated"].append(country)


def ingest_cell(
    manifest_path: Path, region: str, runs: Path, credentials: Path, *, mmdb=None, **options
) -> dict:
    """Verify, convert and import one network x region round; returns the cell's report.

    With the network's `formula`, each imported country's estimated hours are stored too; with
    a `spool` directory, each imported run directory is filed there (rawfiles.spool).
    """
    formula, spool = options.get("formula"), options.get("spool")
    countries = region_countries(load(manifest_path), region)
    cell = {"region": region, "countries": countries, "status": "failed", "ingested": []}
    cell |= {"requests": 0, "unique_ips": 0, "started_at": None, "finished_at": None}
    cell |= {"estimated": [], "estimate_issues": [], "spool_issues": []}
    try:
        verified = verify_round(manifest_path, region, ROUND)
        jobs = read_json(round_directory(manifest_path, region, ROUND) / "round.json")["jobs"]
    except (OSError, ValueError, KeyError) as exc:  # no round collected: the worker failed
        cell["issues"] = [f"round unreadable ({type(exc).__name__})"]
        return cell
    issues = list(verified["issues"])
    bad = {issue.split(":", 1)[0] for issue in issues}
    folder = manifest_path.parent / "regions" / region
    for job, entry in zip(jobs, verified["countries"], strict=True):
        cell["requests"] += entry.get("requests") or 0
        if job["status"] != "completed" or job["country"] in bad:
            continue
        try:
            directory = convert_country(job_directory(folder, job), runs)
            spool_issue = _import(directory, credentials, mmdb, spool)
        except (OSError, ValueError, KeyError, psycopg.Error) as exc:
            issues.append(f"{job['country']}: not imported ({type(exc).__name__})")
            continue
        if spool_issue:
            cell["spool_issues"].append(f"{job['country']}: raw files not kept ({spool_issue})")
        cell["ingested"].append(job["country"])
        _estimate(cell, formula, directory.name, job["country"], credentials)
        cell["unique_ips"] += entry.get("unique_ips") or 0
    cell["started_at"], cell["finished_at"] = _times(jobs)
    cell["status"] = "done" if verified["complete"] and not issues else "failed"
    cell["issues"] = issues
    return cell


async def _spool_alert(db: Database, cell: str, issues: list[str]) -> None:
    async with db.transaction() as conn:
        await events.raise_alert(
            conn,
            "raw_spool_failed",
            f"daily test {cell}: the raw files of {len(issues)} imported runs were not kept",
            severity="major",
            dedupe_key="raw_spool_failed:daily",
            data={"cell": cell, "issues": issues[:20]},
        )


async def _product_id(db: Database, manifest: dict) -> int | None:
    async with db.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT product_id FROM products WHERE provider=%s AND name=%s",
                (manifest["provider"], manifest["product"]),
            )
        ).fetchone()
    return row["product_id"] if row else None


async def emit_cell(db: Database, cell: dict, product_id: int, fallback: datetime) -> None:
    """The cell's daily_test_started and daily_test_finished, in one transaction."""
    test = events.DailyTest(
        product_id=product_id,
        region_id=REGION_IDS[cell["region"]],
        region=cell["region"],
        countries=tuple(cell["countries"]),
    )
    began = datetime.fromisoformat(cell["started_at"]) if cell["started_at"] else fallback
    ended = datetime.fromisoformat(cell["finished_at"]) if cell["finished_at"] else began
    result = events.DailyTestResult(
        status=cell["status"],
        requests=cell["requests"],
        unique_ips=cell["unique_ips"],
        duration_s=max(0.0, (ended - began).total_seconds()),
    )
    for moment in (began, ended):
        await db.ensure_partition("test_events", moment)
    async with db.transaction() as conn:
        await events.emit_daily_test_started(conn, began, test)
        await events.emit_daily_test_finished(conn, ended, test, result)


def _cells(root: Path, spec):
    """(network, manifest path, manifest, region) of every cell with something due."""
    for network in spec.networks:
        manifest_path = root / spec.manifests[network]
        manifest = load(manifest_path)
        for region in spec.regions:
            if region_countries(manifest, region):
                yield network, manifest_path, manifest, region


async def _emit_once(db: Database, cell: dict, manifest: dict, fallback: datetime) -> bool:
    """Emit the cell's pair; False when it must wait for a rerun (no product row, a DB error)."""
    try:
        product_id = await _product_id(db, manifest)
        if product_id is None:  # the network was never imported
            return False
        await emit_cell(db, cell, product_id, fallback)
    except psycopg.Error as exc:
        cell["issues"].append(f"events not emitted ({type(exc).__name__})")
        return False
    return True


def _formula(formulas: Path | None, network: str) -> tuple[dict | None, list[str]]:
    """The network's formula, or None and why (no file, not a version 1 formula)."""
    if formulas is None:
        return None, []
    try:
        return compute.load_formula(formulas, network), []
    except (OSError, ValueError) as exc:
        return None, [f"{network}: no formula ({type(exc).__name__})"]


async def _release(db: Database, report: dict) -> None:
    try:
        report["released_hours"] = len(await release.release_due(db))
    except psycopg.Error as exc:
        report["release_issue"] = f"estimated hours not released ({type(exc).__name__})"


async def ingest_campaign(
    root: Path, credentials: Path, mmdb=None, *, database=None, formulas=None, spool=None
) -> dict:
    """Import every cell of a collected daily test and emit the events not yet emitted.

    With `formulas`, store each imported country's estimated hours, then release the ended ones.
    """
    spec = load_spec(root)
    path = root / REPORT
    previous = read_json(path) if path.exists() else {"cells": {}}
    report = {"campaign_id": spec.campaign_id, "cells": {}}
    fallback = datetime.fromisoformat(spec.starts_at)
    db = database or Database(str(credentials), size=2)
    try:
        for network, manifest_path, manifest, region in _cells(root, spec):
            key = f"{network}/{region}"
            formula, missing = _formula(formulas, network)
            cell = {"network": network} | ingest_cell(
                manifest_path,
                region,
                root / "runs",
                credentials,
                mmdb=mmdb,
                formula=formula,
                spool=spool,
            )
            cell["estimate_issues"] += missing if cell["ingested"] else []
            if cell["spool_issues"]:
                await _spool_alert(db, key, cell["spool_issues"])
            cell["events"] = previous["cells"].get(key, {}).get("events", False)
            if not cell["events"]:
                cell["events"] = await _emit_once(db, cell, manifest, fallback)
            report["cells"][key] = cell
            # Saved after every cell, so an event pair is never emitted twice.
            atomic_json(path, report | {"cells": previous["cells"] | report["cells"]})
        if formulas:
            await _release(db, report)
    finally:
        if database is None:
            await db.close()
    cells = report["cells"].values()
    report["complete"] = "release_issue" not in report and all(
        c["status"] == "done" and c["events"] and not c["estimate_issues"] for c in cells
    )
    report["finished_at"] = datetime.now(UTC).isoformat()
    atomic_json(path, report)
    return report
