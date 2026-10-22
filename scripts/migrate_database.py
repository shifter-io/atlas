"""Back up the local v4 or v5 database, then migrate it in place to the current schema
(proxy-index db-migrate).

The migration itself compares every rebuilt summary, scope, pool and bucket (and, from v4, every
cohort, breakdown and score) with its original JSON before committing; this wrapper adds the dump
and an evidence file.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from proxy_index.providers.credentials import read_credentials
from proxy_index.storage import connect, migrate, read_summary, status

ROOT = Path(__file__).resolve().parents[1]


def main():
    credentials = ROOT / ".secrets/database.toml"
    config = read_credentials(credentials, "postgres")
    if (config["host"], str(config["port"]), config["dbname"]) != (
        "127.0.0.1",
        "55432",
        "proxy_reviews",
    ):
        raise ValueError("Only the dedicated local project database is supported")
    with connect(credentials) as conn:
        runs = [r[0] for r in conn.execute("SELECT run_id::text FROM test_runs ORDER BY 1")]
        version = conn.execute("SELECT max(version) FROM schema_versions").fetchone()[0]
    before = {run_id: read_summary(run_id, credentials) for run_id in runs}
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    folder = ROOT / ".state/postgres/backups" / f"{stamp}_v{version}"
    folder.mkdir(parents=True, mode=0o700)
    binary = Path(json.loads((ROOT / ".state/postgres/runtime.json").read_text())["bin_dir"])
    dump = folder / "database.dump"
    subprocess.run(
        [
            str(binary / "pg_dump"),
            *("-h", config["host"], "-p", str(config["port"]), "-U", config["user"]),
            *("-d", config["dbname"], "-Fc", "-f", str(dump)),
        ],
        env=os.environ | {"PGPASSWORD": config["password"]},
        check=True,
        capture_output=True,
    )
    dump.chmod(0o600)
    subprocess.run(
        [str(binary / "pg_restore"), "--list", str(dump)], check=True, capture_output=True
    )
    result = migrate(credentials)
    if {run_id: read_summary(run_id, credentials) for run_id in runs} != before:
        raise ValueError(f"Run summaries changed; restore {dump}")
    evidence = result | {
        "backup": str(dump),
        "backup_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
        "status": status(credentials),
    }
    (folder / "migration.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({k: v for k, v in evidence.items() if k != "status"}))


if __name__ == "__main__":
    main()
