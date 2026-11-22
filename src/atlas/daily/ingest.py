"""`daily ingest`: one collected daily test into the database, then its TAPE-1 events.

Runs on the Mac (the database is there) on a campaign root downloaded and checked with
`campaign collect`. Each network x region cell's round is checked with `rounds.verify_round`;
each country that completed and reconciles becomes an ingestible run directory under
`<root>/runs/<run_id>/` (the scheduler's measurement as `summary.json`, and its raw observation
chunks aggregated into `ips.jsonl.gz` per 5-minute bucket and IP, the rows the runner's JSON
sink writes), filed raw in the provider's folder of `spool` when given (batches.spool,
ARCHIVE-RAW-1), then enriched with DB-IP (`enrichment.enrich_run`). A country whose requests were
mostly refused at login (proxy_login.py, PROXY-AUTH-ALERT-1) is not imported: our login failed,
so its cell fails by our side and the country keeps its previous verdict and estimates. The
countries of whole cells are then imported about `batch` (20) at a time, each batch in one
`storage.ingest_many`
transaction, `connections` batches at once over `processes` worker processes (atlas/sizing.py)
(daily.batches over storage_parallel; a batch that fails is retried one country at a time).
Then each cell gets `daily_test_started` and `daily_test_finished` (done only when every due
country was measured and imported, failed otherwise), in campaign order. A database error
fails its country or its cell's events, never the other cells.

A rerun is safe: a converted run is reused, `storage.ingest` replays as a no-op, and a cell
whose events are recorded in `<root>/daily-ingest.json` gets no second pair. So the first pair
emitted is authoritative: a rerun that later imports a country that failed (or a network whose
product had no row yet, which defers its events to the rerun) adds the data, never a corrected
event. The report holds names and counts only, never an address.

With `formulas` (the formulas/*.json directory), every imported country's 24 estimated hours
are stored too, a batch's in one `compute.estimate_runs` (a rerun replaces only unreleased
hours), and the ended hours
are released (estimates.release). A country without an estimate is an issue of its cell's
`estimate_issues` and makes the report incomplete, never a failed test.

Once the hour's cells are stored, the live tape's audits (new hosting ASN, IPs shared with
another network) run once on all its stored runs (daily.audits, detect's checks); a rerun
repeats none. A database error there is the report's `audit_issue` and makes it incomplete.

`ingest_part` imports only some networks' cells (Part, DAILY-BUDGET-STOP-1): the hour job
(daily.hourly) imports each network that ended while others still ran without the audits, the
report's `tested` naming their runs, then the rest with the audits over every part's runs
(DAILY-IMPORT-1). A cell the connection
budget stopped (the ledger's `budget-stop.json`, daily.overtime) stores the country it stopped
as measured, completed with stop_reason `budget`, and is `stopped_budget` (done for its events
and the report's `complete`) when nothing else is wrong. A cell the HH:45 cell stop ended
(LOWAVAIL-PACE-2, the record's `cell_stop`) is the same with stop_reason `cell_stop`, as
`stopped_cell`. Either lists the countries it never started as `not_reached` (in its report and
its daily_test_finished event, read by daily.verify): no run is stored for them, so they are
neither failed tests nor outages. So does a cell whose round left countries `not_reached` at its
soft limit (onboarding.rounds.SOFT_LIMIT, LOWAVAIL-PACE-3); that cell is `done`.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from .. import proxy_login, refusals, sizing, storage_parallel, timing
from ..campaign.contracts import budget_stop_path
from ..campaign.tick import load_spec
from ..enrichment import enrich_run
from ..estimates import compute, release
from ..onboarding.files import atomic_json, digest, read_json
from ..onboarding.manifest import load, region_countries
from ..onboarding.rounds import NOT_REACHED, round_directory, verify_round
from ..onboarding.scheduler import job_directory
from ..orchestrator import events
from ..orchestrator.db import Database
from ..regions import REGION_IDS
from ..stats import bucket_of, iso
from . import DAILY, MARKER, audits, batches

REPORT = "daily-ingest.json"
STOPPED_LATER = "not_started_deadline"  # a country its round never started (rounds._close)
ENDED = ("done", "stopped_budget", "stopped_cell")  # all measured, or stopped by design
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


def convert_country(folder: Path, runs: Path, *, stopped: str | None = None) -> Path:
    """An ingestible run directory from one round country folder; returns its path. A country
    a stop ended is stored as measured: completed, with its stop_reason (`stopped`: `budget` or
    `cell_stop`)."""
    found = read_json(folder / "summary.json")
    measured = found["measurement"]
    if not measured:
        raise ValueError("the country has no measurement")
    if stopped:
        measured |= {"status": "completed", "stop_reason": stopped}
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
    refusals.write(directory, found.get("refusals") or [])  # raw only, never imported
    atomic_json(directory / "summary.json", measured | {"artifacts": artifacts, MARKER: DAILY})
    return directory


def _enrich(directory: Path, mmdb) -> None:
    try:
        enrich_run(directory, mmdb)
    except FileExistsError:
        pass  # enriched by an earlier attempt


def _times(jobs: list[dict]) -> tuple[str | None, str | None]:
    started = [j["started_at"] for j in jobs if j.get("started_at")]
    finished = [j["finished_at"] for j in jobs if j.get("finished_at")]
    return (min(started) if started else None), (max(finished) if finished else None)


@dataclass
class Cell:
    """A network x region cell on its way in: its report so far and its due countries."""

    key: str
    network: str
    manifest: dict
    report: dict
    countries: list[batches.Country] = field(default_factory=list)
    complete: bool = False
    missing: list[str] = field(default_factory=list)  # why the network has no formula


def _unreached(job: dict, stopped: bool) -> bool:
    """A country the round left at its soft limit, or (`stopped`) never started before a stop."""
    return job["status"] == NOT_REACHED or stopped and job["status"] == STOPPED_LATER


def _budget_issues(jobs: list[dict]) -> set[str]:
    """The issues a round stopped by the budget has by design: the country it stopped and those
    it never started."""
    found = {f"{j['country']}: interrupted" for j in jobs if j["status"] == "interrupted"}
    return found | {f"{j['country']}: not started" for j in jobs if j["status"] == STOPPED_LATER}


@dataclass(frozen=True)
class CellInputs:
    """What prepare_cell converts a cell's countries with: the DB-IP file, the network's
    formula, the raw-file spool, the campaign's slot (the hour each country's estimate counts
    for, however late its run started), whether a stop ended the cell (daily.overtime) and its
    stop_reason (`budget`, or `cell_stop` for the HH:45 cell stop)."""

    mmdb: object = None
    formula: dict | None = None
    spool: Path | None = None
    scheduled: datetime | None = None
    budget_stop: bool = False
    stop_reason: str = "budget"


def _convert(country: batches.Country, folder: Path, runs: Path, spool, mmdb, provider) -> None:
    """Convert, spool (into the provider's folder) and enrich one country, or set its issue (a
    country mostly refused at login is spooled, never stored)."""
    try:
        stopped = {"stopped": country.stopped} if country.stopped else {}
        country.directory = convert_country(folder, runs, **stopped)
        batches.spool(country, spool, provider)
        summary = read_json(country.directory / "summary.json")
        refused = (summary.get("errors") or {}).get(proxy_login.ERROR) or 0
        country.login = (refused, summary.get("completed") or 0)
        country.payment = proxy_login.payments(summary.get("status_codes"))
        if proxy_login.summary_refused(summary):
            country.issue = f"{country.code}: not imported (proxy login refused)"
            return
        _enrich(country.directory, mmdb)
    except (OSError, ValueError, KeyError) as exc:
        country.fail(exc)


def prepare_cell(
    manifest_path: Path, region: str, runs: Path, inputs: CellInputs | None = None
) -> tuple[dict, list[batches.Country], bool]:
    """Verify one network x region round, then convert, spool (batches.spool) and enrich each
    completed country; returns the cell's report so far, its countries to store (batches.store)
    and whether it verified complete. A country that fails a step, or was mostly refused at
    login, has its issue set and is not stored (it is still spooled). In a cell the budget
    stopped the country it stopped is stored too (convert_country) and the round is complete
    without the countries it never started: `stopped_budget`.
    """
    inputs = inputs or CellInputs()
    manifest = load(manifest_path)
    countries = region_countries(manifest, region)
    cell = {"region": region, "countries": countries, "status": "failed", "ingested": []}
    cell |= {"requests": 0, "unique_ips": 0, "started_at": None, "finished_at": None}
    cell |= {"estimated": [], "estimate_issues": [], "spool_issues": []}
    try:
        verified = verify_round(manifest_path, region, ROUND)
        jobs = read_json(round_directory(manifest_path, region, ROUND) / "round.json")["jobs"]
    except (OSError, ValueError, KeyError) as exc:  # no round collected: the worker failed
        cell["issues"] = [f"round unreadable ({type(exc).__name__})"]
        return cell, [], False
    expected = _budget_issues(jobs) & set(verified["issues"]) if inputs.budget_stop else set()
    cell["issues"] = [issue for issue in verified["issues"] if issue not in expected]
    if expected:
        status = "stopped_cell" if inputs.stop_reason == "cell_stop" else "stopped_budget"
        cell[status] = True
    unreached = [j["country"] for j in jobs if _unreached(j, bool(expected))]
    if unreached:
        cell["not_reached"] = unreached
    bad = {issue.split(":", 1)[0] for issue in cell["issues"]}
    folder = manifest_path.parent / "regions" / region
    due = []
    for job, entry in zip(jobs, verified["countries"], strict=True):
        cell["requests"] += entry.get("requests") or 0
        stopped = bool(expected) and job["status"] == "interrupted"
        if (job["status"] != "completed" and not stopped) or job["country"] in bad:
            continue
        country = batches.Country(job["country"], entry, inputs.formula, inputs.scheduled)
        country.stopped = inputs.stop_reason if stopped else None
        due.append(country)
        options = (inputs.spool, inputs.mmdb, manifest["provider"])
        _convert(country, job_directory(folder, job), runs, *options)
    cell["started_at"], cell["finished_at"] = _times(jobs)
    return cell, due, verified["complete"] or bool(expected) and not cell["issues"]


def finish_cell(cell: dict, countries: list[batches.Country], complete: bool) -> dict:
    """The cell's report once its countries are stored, in the countries' order."""
    for country in countries:
        if country.spool_issue:  # spooled before its import, so whether or not imported
            issue = f"{country.code}: raw files not kept ({country.spool_issue})"
            cell["spool_issues"].append(issue)
        if country.issue:
            cell["issues"].append(country.issue)
            continue
        cell["ingested"].append(country.code)
        if country.estimate_issue:
            cell["estimate_issues"].append(country.estimate_issue)
        elif country.estimated:
            cell["estimated"].append(country.code)
        cell["unique_ips"] += country.entry.get("unique_ips") or 0
    done = next((s for s in ("stopped_cell", "stopped_budget") if cell.get(s)), "done")
    cell["status"] = done if complete and not cell["issues"] else "failed"
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
        status="failed" if cell["status"] == "failed" else "done",  # stopped_*: done
        not_reached=tuple(cell.get("not_reached", ())),
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


@dataclass(frozen=True)
class Part:
    """Which cells one import of an hour takes (DAILY-BUDGET-STOP-1): those of `networks`
    (None: all), and whether it writes the hour's audits, over its own stored runs and the
    `earlier` ones of a part imported without them (that part's report's `tested`)."""

    networks: frozenset | None = None
    audits: bool = True
    earlier: dict = field(default_factory=dict)


def _prepared(root: Path, spec, sources: tuple, networks: frozenset | None):
    """Every cell of `networks` (None: all) with something due, verified, converted, spooled
    and enriched, in campaign order; `sources` is (formulas, mmdb, spool)."""
    formulas, mmdb, spool = sources
    scheduled = datetime.fromisoformat(spec.starts_at)
    marker = budget_stop_path(root, ROUND)
    record = read_json(marker) if marker.exists() else {"cells": []}
    stopped, late = set(record["cells"]), set(record.get("cell_stop", []))
    for network, manifest_path, manifest, region in _cells(root, spec):
        if networks is not None and network not in networks:
            continue
        formula, missing = _formula(formulas, network)
        budget_stop = f"{network}/{region}" in stopped
        reason = "cell_stop" if f"{network}/{region}" in late else "budget"
        inputs = CellInputs(mmdb, formula, spool, scheduled, budget_stop, reason)
        found = prepare_cell(manifest_path, region, root / "runs", inputs)
        report, countries, complete = found
        cell = Cell(f"{network}/{region}", network, manifest, {"network": network} | report)
        cell.countries, cell.complete, cell.missing = countries, complete, missing
        yield cell


@dataclass
class CampaignHour:
    """One ingest_campaign call: where it stores, and its report and the previous one."""

    db: Database
    credentials: Path
    path: Path
    previous: dict
    report: dict
    fallback: datetime
    products: set = field(default_factory=set)  # networks whose product row exists
    options: storage_parallel.Options = field(default_factory=storage_parallel.Options)
    on_commit: Callable | None = None  # storage_parallel.store's, as each batch commits
    part: Part = field(default_factory=Part)


async def _has_product(hour: CampaignHour, network: str, manifest: dict) -> bool:
    """Whether the network's product row exists (or may: a lookup error), before a chunk."""
    if network not in hour.products:
        try:
            if await _product_id(hour.db, manifest) is None:
                return False
        except psycopg.Error:
            return True  # _emit_once looks again and reports the error
        hour.products.add(network)
    return True


def _groups(chunks: list[list[Cell]], batch: int) -> list[tuple[list, frozenset]]:
    """Each chunk's countries `batch` at a time, with the networks they belong to."""
    groups = []
    for chunk in chunks:
        owned = [(country, cell.network) for cell in chunk for country in cell.countries]
        for start in range(0, len(owned), batch):
            part = owned[start : start + batch]
            groups.append(([country for country, _ in part], frozenset(n for _, n in part)))
    return groups


def plan_groups(cells, batch: int) -> tuple[list[Cell], list[tuple[list, frozenset]]]:
    """The cells in order and their groups: about `batch` countries each, whole cells per group
    (a larger cell is split into groups of its own), with the networks they belong to."""
    chunks, chunk = [], []
    for cell in cells:
        if chunk and sum(len(c.countries) for c in chunk) + len(cell.countries) > batch:
            chunks.append(chunk)
            chunk = []
        chunk.append(cell)
    chunks += [chunk] if chunk else []
    return [cell for chunk in chunks for cell in chunk], _groups(chunks, batch)


async def _finish(hour: CampaignHour, cells: list[Cell], known: set) -> None:
    """Finish the stored cells in order: each one's spool alert, its events (once) and the saved
    report."""
    for cell in cells:
        report = finish_cell(cell.report, cell.countries, cell.complete)
        report["estimate_issues"] += cell.missing if report["ingested"] else []
        known |= {cell.network} if report["ingested"] else set()
        if report["spool_issues"]:
            await _spool_alert(hour.db, cell.key, report["spool_issues"])
        report["events"] = hour.previous["cells"].get(cell.key, {}).get("events", False)
        if not report["events"] and cell.network in known:
            report["events"] = await _emit_once(hour.db, report, cell.manifest, hour.fallback)
        hour.report["cells"][cell.key] = report
        # Saved after every cell, so an event pair is never emitted twice.
        cells_now = hour.previous["cells"] | hour.report["cells"]
        atomic_json(hour.path, hour.report | {"cells": cells_now})
    await _login_triggers(hour, cells)


async def _login_triggers(hour: CampaignHour, cells: list[Cell]) -> None:
    """PROXY-AUTH-PAUSE-1: a network whose daily tests here were mostly refused at login (or for
    payment, PAUSE-402-1) raises the trigger that pauses it (orchestrator.login_pause)."""
    from ..orchestrator.login_pause import COUNTS, daily_refused

    totals: dict[str, tuple[dict, dict]] = {}
    for cell in cells:
        counts, _ = totals.setdefault(cell.network, (dict.fromkeys(COUNTS, 0), cell.manifest))
        for country in cell.countries:
            counts["refused"] += country.login[0]
            counts["requests"] += country.login[1]
            counts["payment"] += country.payment
    for counts, manifest in totals.values():
        if proxy_login.refused(counts["refused"], counts["requests"]):
            await daily_refused(hour.db, manifest, counts, hour.fallback)


async def store_cells(hour: CampaignHour, cells, batch: int) -> None:
    """Store the cells' countries about `batch` at a time, whole cells per batch (a larger cell
    is split into batches of its own), all batches over hour.options.connections connections at
    once (batches.store), then finish the cells in order (_finish) and write the hour's audits
    (_audit). SIM-LOAD-1's generated cells (scripts/scale/simulate_steps.py) go through it too.

    A cell's events wait for a rerun when its network has no product row yet, as they would
    cell by cell: unless the row existed before the store, only a cell at or after the
    network's first import looks it up.
    """
    with timing.stage("ingest", "prepare"):  # verify, convert, spool and enrich (_prepared)
        ordered, groups = plan_groups(cells, batch)
    networks = {cell.network: cell.manifest for cell in ordered}
    known = {n for n, manifest in networks.items() if await _has_product(hour, n, manifest)}
    with timing.stage("ingest", "store", groups=len(groups)):
        batches.store(groups, hour.credentials, hour.options, hour.on_commit)
    with timing.stage("ingest", "events", cells=len(ordered)):
        await _finish(hour, ordered, known)
    with timing.stage("ingest", "audits"):
        await _audit(hour, ordered)


async def _audit(hour: CampaignHour, cells: list[Cell]) -> None:
    """The audits of the hour's stored runs (daily.audits), in one transaction."""
    tested = {
        country.directory.name: (country.code, cell.report["region"])
        for cell in cells
        for country in cell.countries
        if country.issue is None and country.directory is not None
    }
    if not hour.part.audits:  # a later part of the hour audits them with its own
        hour.report["tested"] = tested
        return
    tested |= {run: tuple(entry) for run, entry in hour.part.earlier.items()}
    if not tested:
        return
    try:
        written = await asyncio.to_thread(audits.stored, hour.credentials, tested, hour.fallback)
    except (OSError, ValueError, psycopg.Error) as exc:
        hour.report["audit_issue"] = f"audits not written ({type(exc).__name__})"
        return
    hour.report["audits"] = written


async def ingest_campaign(
    root: Path,
    credentials: Path,
    mmdb=None,
    *,
    database=None,
    formulas=None,
    spool=None,
    **sizes: int,
) -> dict:
    """Import every cell of a collected daily test and emit the events not yet emitted.

    The cells' countries are stored about `batch` (sizes, else sizing.ingest_sizes) at a time,
    whole cells per batch (a larger cell is split into batches of its own), `connections`
    batches at once over `processes` worker processes, each stage timed. With `formulas`, store each
    imported country's estimated hours, then release the ended ones.
    """
    sizes = {name: value for name, value in sizes.items() if value is not None}
    if set(sizes) - set(sizing.INGEST):
        raise TypeError(f"ingest_campaign() got unexpected sizes {sorted(sizes)}")
    if sizes.get("batch", 1) < 1:
        raise ValueError("the batch must hold at least one daily test")
    if sizes.get("connections", 1) < 1:
        raise ValueError("the batches need at least one connection")
    if sizes.get("processes", 1) < 1:
        raise ValueError("the batches need at least one process")
    return await _ingest(root, credentials, (formulas, mmdb, spool), database, sizes, Part())


async def ingest_part(root: Path, credentials: Path, mmdb, part: Part, **sources) -> dict:
    """ingest_campaign for one part of the hour (Part), with its `formulas` and `spool`. The
    report of the hour (`daily-ingest.json`) holds every part's cells; the one returned, only
    this part's."""
    found = (sources.get("formulas"), mmdb, sources.get("spool"))
    return await _ingest(root, credentials, found, None, {}, part)


async def _ingest(root: Path, credentials: Path, sources: tuple, database, sizes, part) -> dict:
    """The import of ingest_campaign and ingest_part: `sources` is (formulas, mmdb, spool)."""
    spec = load_spec(root)
    path = root / REPORT
    previous = read_json(path) if path.exists() else {"cells": {}}
    report = {"campaign_id": spec.campaign_id, "cells": {}}
    fallback = datetime.fromisoformat(spec.starts_at)
    db = database or Database(str(credentials), size=2)
    hour = CampaignHour(db, credentials, path, previous, report, fallback, part=part)
    try:
        sizes = await sizing.ingest_sizes(db, sizes)
        hour.options.connections, hour.options.processes = sizes["connections"], sizes["processes"]
        hour.options.memory_budget = sizes["memory_budget"]
        cells = _prepared(root, spec, sources, part.networks)
        await store_cells(hour, cells, sizes["batch"])
        if sources[0]:  # the formulas
            with timing.stage("ingest", "release"):
                await _release(db, report)
    finally:
        if database is None:
            await db.close()
    cells = report["cells"].values()
    issues = ("release_issue", "audit_issue")
    report["complete"] = not any(key in report for key in issues) and all(
        c["status"] in ENDED and c["events"] and not c["estimate_issues"] for c in cells
    )
    report["finished_at"] = datetime.now(UTC).isoformat()
    atomic_json(path, report | {"cells": previous["cells"] | report["cells"]})
    return report
