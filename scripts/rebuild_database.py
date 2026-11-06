"""Back up and rebuild the dedicated local database from its saved run artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from postgres_local import save_private
from psycopg import sql
from rebuild_verification import benchmark, logical_snapshot, sizes

from proxy_index.providers.credentials import read_credentials
from proxy_index.storage import SCHEMA_VERSION, connect, ingest, initialize, status

ROOT = Path(__file__).resolve().parents[1]


def inventory(conn):
    return dict(conn.execute("SELECT run_id::text,summary_sha256 FROM test_runs"))


def load_credentials():
    """Return the app file and the app and admin credentials of the local project server."""
    app_file = ROOT / ".secrets/database.toml"
    app = read_credentials(app_file, "postgres")
    admin = read_credentials(ROOT / ".secrets/postgres-admin.toml", "postgres")
    if (
        app["dbname"] != "proxy_reviews"
        or app["host"] != "127.0.0.1"
        or str(app["port"]) != "55432"
    ):
        raise ValueError("This script only rebuilds the dedicated local project database")
    if (admin["host"], str(admin["port"])) != (app["host"], str(app["port"])):
        raise ValueError("Admin connection must address the same server")
    return app_file, app, admin


def saved_paths(expected):
    """Map each existing run to the directory of its unchanged saved summary."""
    paths = {}
    for path in (ROOT / "results").rglob("summary.json"):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        run_id = json.loads(path.read_text())["run_id"]
        if expected.get(run_id) == digest:
            paths[run_id] = path.parent
    if set(paths) != set(expected):
        raise ValueError("Saved artifacts missing for existing runs; refusing rebuild")
    return paths


def dump_database(binary, app, backup):
    """Dump the current database into the backup directory and check the dump reads back."""
    dump = backup / "database.dump"
    env = os.environ | {"PGPASSWORD": app["password"]}
    subprocess.run(
        [
            str(binary / "pg_dump"),
            "-h",
            app["host"],
            "-p",
            str(app["port"]),
            "-U",
            app["user"],
            "-d",
            app["dbname"],
            "-Fc",
            "-f",
            str(dump),
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    dump.chmod(0o600)
    subprocess.run(
        [str(binary / "pg_restore"), "--list", str(dump)], check=True, capture_output=True
    )
    return dump


def build_candidate(control, config, app, candidate, paths):
    """Create the candidate database, ingest every saved run into it and time the ingest."""
    control.execute(
        sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(candidate), sql.Identifier(app["user"])
        )
    )
    initialize(config)
    started = time.perf_counter()
    for directory in paths.values():
        ingest(directory, config)
    return time.perf_counter() - started


def verify_rebuild(config, expected, original_snapshot):
    """Check the candidate against the original; return its snapshot, sizes and benchmark."""
    with connect(config) as fresh:
        if inventory(fresh) != expected:
            raise ValueError("Rebuilt run inventory differs")
        mismatch = fresh.execute(
            "SELECT r.run_id FROM test_runs r LEFT JOIN "
            "ip_observations o USING(run_key) GROUP BY r.run_key HAVING "
            "coalesce(sum(o.count),0) <> CASE WHEN (r.summary->>'pool_eligible')::boolean "
            "THEN (r.summary->>'successes')::bigint ELSE 0 END"
        ).fetchall()
        if mismatch:
            raise ValueError("Observation totals differ from saved results")
    with connect(config) as fresh:
        fresh.execute("ANALYZE")
        rebuilt_snapshot = logical_snapshot(fresh)
        if rebuilt_snapshot != original_snapshot:
            differences = [
                k for k in original_snapshot if original_snapshot[k] != rebuilt_snapshot[k]
            ]
            raise ValueError(f"Rebuild differs in: {differences}")
        rebuilt_sizes = sizes(fresh)
        rebuilt_benchmark = benchmark(fresh)
    return rebuilt_snapshot, rebuilt_sizes, rebuilt_benchmark


def print_validation(original_snapshot, ingest_seconds, original_sizes, rebuilt_sizes):
    """Print what was validated, the ingest time and the size before and after."""
    print(
        json.dumps(
            {
                "validated": list(original_snapshot),
                "ingest_seconds": ingest_seconds,
                "before_bytes": sum(v["total_bytes"] for v in original_sizes.values()),
                "after_bytes": sum(v["total_bytes"] for v in rebuilt_sizes.values()),
            }
        ),
        flush=True,
    )


def verify_replay(config, paths):
    """Replay every saved run and check that nothing is imported twice."""
    before = status(config)
    for directory in paths.values():
        if ingest(directory, config)["imported"]:
            raise ValueError("Replay unexpectedly imported a duplicate")
    if status(config) != before:
        raise ValueError("Replay changed cached totals")


def swap(control, original, app, candidate, previous):
    """Rename the original database away and the candidate into its place."""
    # Disallow new connections before releasing the ingestion lock.
    for name in (app["dbname"], candidate):
        control.execute(
            sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(sql.Identifier(name))
        )
    original.close()
    try:
        control.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=ANY(%s)",
            ([app["dbname"], candidate],),
        )
        with control.transaction():
            control.execute(
                sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                    sql.Identifier(app["dbname"]), sql.Identifier(previous)
                )
            )
            control.execute(
                sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                    sql.Identifier(candidate), sql.Identifier(app["dbname"])
                )
            )
            control.execute(
                sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(
                    sql.Identifier(app["dbname"])
                )
            )
    except BaseException:
        control.execute(
            sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(
                sql.Identifier(app["dbname"])
            )
        )
        raise


def record_evidence(app_file, backup, dump, previous, measured):
    """Write the rebuild evidence next to the backup and into the artifacts; return it."""
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "previous_database": previous,
        "backup": str(dump),
        "backup_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
        "status": status(app_file),
        "replay_verified": True,
    } | measured
    (backup / "rebuild.json").write_text(json.dumps(evidence, indent=2) + "\n")
    (ROOT / "artifacts/postgresql.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


def main():
    app_file, app, admin = load_credentials()
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    candidate = "proxy_reviews_build_" + stamp
    previous = "proxy_reviews_before_" + stamp
    backup = ROOT / ".state/postgres/backups" / stamp
    backup.mkdir(parents=True, mode=0o700)
    config = backup / "candidate.toml"
    save_private(config, app | {"dbname": candidate})
    binary = Path(json.loads((ROOT / ".state/postgres/runtime.json").read_text())["bin_dir"])
    # Keep ingestion blocked until inventory, dump, staging and swap are complete.
    with connect(app_file) as original:
        original.execute("SELECT pg_advisory_lock(817452901)")
        expected = inventory(original)
        original_snapshot = logical_snapshot(original)
        original_sizes = sizes(original)
        original_benchmark = benchmark(original)
        paths = saved_paths(expected)
        dump = dump_database(binary, app, backup)
        with psycopg.connect(**(admin | {"dbname": "postgres"}), autocommit=True) as control:
            ingest_seconds = build_candidate(control, config, app, candidate, paths)
            rebuilt_snapshot, rebuilt_sizes, rebuilt_benchmark = verify_rebuild(
                config, expected, original_snapshot
            )
            print_validation(original_snapshot, ingest_seconds, original_sizes, rebuilt_sizes)
            verify_replay(config, paths)
            swap(control, original, app, candidate, previous)
    measured = {
        "logical_checksums": rebuilt_snapshot,
        "sizes_before": original_sizes,
        "sizes_after": rebuilt_sizes,
        "queries_before": original_benchmark,
        "queries_after": rebuilt_benchmark,
        "ingest_seconds": ingest_seconds,
    }
    evidence = record_evidence(app_file, backup, dump, previous, measured)
    config.unlink()
    print(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "backup": str(dump),
                "previous_database": previous,
                "counts": evidence["status"]["counts"],
            }
        )
    )


if __name__ == "__main__":
    main()
