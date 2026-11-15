"""PULSE-COUNTERS-1: count the exit IPs of the pulse runs stored without them.

Until PULSE-ENRICH-1 the manager stored a pulse upload without its IP rows and so without the
DB-IP enrichment its droplet made. `backfill` reads those runs' uploads from the raw spool
(ARCHIVE-RAW-1, the uploads as received), enriches each with the given DB-IP file in place of
the droplet's (as the manager does at upload, enrichment.SharedEnricher) and imports it again
with storage.ingest's counters: the run is stored, so only its enrichment is added and, with it
and in its transaction, its exit IPs counted (ip_addresses, product_ips, provider_overlap).

Safe and idempotent: a candidate is a pool-eligible pulse run without a run_enrichments row, and
a run is counted with its first enrichment and only then (storage_batch.existing), so a run is
never counted twice, whoever adds it first (a retried upload, another backfill). Each `batch` of
runs is one storage.ingest_many transaction under the locks an import takes (the ingest lock
shared, its networks', its runs'); a batch the import refuses is imported one run at a time, and a
run it refuses is named and left (a candidate), so one run never stops the others. A database of
another schema stops it before any write. Without `apply` it only counts. The report: the
candidates and the product_ips rows before and after, the runs and IP rows counted, the runs
without a raw file and those enrichment or the import refused (counted, and named at most SHOWN).
"""

from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path

from .. import storage
from ..archive import rawfiles
from ..enrichment import SharedEnricher, find_source
from ..orchestrator.archive import unpack
from ..orchestrator.control import read_setting

BATCH = 200  # runs per transaction
SHOWN = 5
CANDIDATES = (
    "SELECT t.run_id::text,t.batch_id,t.imported_at FROM test_runs t JOIN test_batches b "
    "USING(batch_id) WHERE b.kind='pulse' AND (t.summary->>'pool_eligible')::boolean "
    "AND NOT EXISTS (SELECT 1 FROM run_enrichments e WHERE e.run_key=t.run_key) ORDER BY t.run_key"
)
COUNTERS = "SELECT count(*) FROM product_ips"


def _raw_files(root: Path, candidates: list[tuple]) -> dict[str, Path]:
    """The candidates' spooled uploads by file name, from the hours around their imports."""
    if not candidates:
        return {}
    wanted = {f"pulse-b{run[1]}-{run[0]}.tar.gz" for run in candidates}
    first = min(run[2] for run in candidates) - timedelta(hours=1)
    last = max(run[2] for run in candidates) + timedelta(hours=1)
    found = {}
    for hour, folder in rawfiles.hours(root):
        if first <= hour <= last:
            found |= {p.name: p for p in rawfiles._runs(folder) if p.name in wanted}
    return found


def _import(database_file, directories: list[Path], batch_ids: list, report: dict) -> list:
    """storage.ingest_many of a batch with the counters; when the import refuses it, each run
    on its own, a run it refuses named in the report (an unreachable database still stops)."""
    try:
        return storage.ingest_many(directories, database_file, batch_ids=batch_ids, counters=True)
    except (storage.IngestBatchError, ValueError, KeyError, TypeError, OSError):
        pass
    done = []
    for directory, batch_id in zip(directories, batch_ids, strict=True):
        try:
            done.append(storage.ingest(directory, database_file, batch_id=batch_id, counters=True))
        except storage.psycopg.OperationalError:
            raise
        except (ValueError, KeyError, TypeError, OSError, storage.psycopg.Error):
            report["refused"].append(f"{directory.name}.tar.gz")
    return done


def _apply(database_file, enricher, found: list[tuple], report: dict) -> None:
    """Enrich and import one batch of uploads ((path, batch id)), adding to the report."""
    with tempfile.TemporaryDirectory(prefix="atlas-backfill-") as temporary:
        directories, batch_ids = [], []
        for path, batch_id in found:
            directory = Path(temporary, path.name.removesuffix(".tar.gz"))
            directory.mkdir()
            unpack(path, directory, max_bytes=rawfiles.MAX_EXTRACTED)
            try:
                enricher.reenrich(directory)
            except (OSError, ValueError, KeyError, TypeError):
                report["refused"].append(path.name)
                continue
            directories.append(directory)
            batch_ids.append(batch_id)
        if not directories:
            return
        done = _import(database_file, directories, batch_ids, report)
    counted = [run["counted_ip_rows"] for run in done]
    report["runs"] += sum(rows > 0 for rows in counted)
    report["ip_rows"] += sum(counted)


def backfill(database_file, *, spool=None, mmdb=None, apply=False, batch=BATCH) -> dict:
    """Count, and with `apply` backfill, the pulse runs stored without their exit IPs counted
    (module docstring)."""
    with storage.connect(database_file) as conn:
        conn.autocommit = True
        root = Path(spool or read_setting(conn, "raw.spool_dir") or "")
        candidates = conn.execute(CANDIDATES).fetchall()
        before = conn.execute(COUNTERS).fetchone()[0]
        version = conn.execute("SELECT max(version) FROM schema_versions").fetchone()[0]
    if apply and version != storage.SCHEMA_VERSION:
        raise ValueError("Database schema requires db-migrate or a rebuild before a backfill")
    files = _raw_files(root, candidates) if root.name else {}
    names = [f"pulse-b{run[1]}-{run[0]}.tar.gz" for run in candidates]
    pairs = zip(names, candidates, strict=True)
    found = [(files[name], run[1]) for name, run in pairs if name in files]
    report = {"candidates": len(candidates), "raw_files": len(found), "runs": 0, "ip_rows": 0}
    report |= {"product_ips": before, "refused": []}
    missing = [n for n in names if n not in files]
    report |= {"no_raw_file": missing[:SHOWN], "no_raw_files": len(missing)}
    if apply and found:
        enricher = SharedEnricher(find_source(mmdb))
        try:
            for n in range(0, len(found), batch):
                _apply(database_file, enricher, found[n : n + batch], report)
        finally:
            enricher.close()
    with storage.connect(database_file) as conn:
        report["candidates_after"] = len(conn.execute(CANDIDATES).fetchall())
        report["product_ips_after"] = conn.execute(COUNTERS).fetchone()[0]
    report["refused_runs"] = len(report["refused"])
    report["refused"] = report["refused"][:SHOWN]
    return report
