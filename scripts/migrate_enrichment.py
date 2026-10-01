"""Back up the local v2 database, migrate to v3, and verify every enrichment value."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from milf_framework.providers.credentials import read_credentials
from milf_framework.storage import ENRICHMENT_FIELDS, SCHEMA_VERSION, connect, initialize, status

ROOT = Path(__file__).resolve().parents[1]


def main():
    if SCHEMA_VERSION != 3:
        raise SystemExit("Historical v2/v3 tool; use scripts/rebuild_database.py for v4")
    credentials = ROOT / ".secrets/database.toml"
    config = read_credentials(credentials, "postgres")
    if (config["host"], str(config["port"]), config["dbname"]) != (
        "127.0.0.1",
        "55432",
        "proxy_reviews",
    ):
        raise ValueError("Only the dedicated local project database is supported")
    with connect(credentials) as conn:
        version = conn.execute("SELECT version FROM schema_versions").fetchone()[0]
        if version != 2:
            raise ValueError("This verification script requires schema v2")
        original = dict(
            conn.execute("SELECT (ip_id,release_id)::text,attributes FROM ip_enrichment")
        )
    before = status(credentials)
    folder = ROOT / ".state/postgres/backups" / datetime.now(UTC).strftime("%Y%m%d_%H%M%S_v2")
    folder.mkdir(parents=True, mode=0o700)
    binary = Path(json.loads((ROOT / ".state/postgres/runtime.json").read_text())["bin_dir"])
    dump = folder / "database.dump"
    subprocess.run(
        [
            str(binary / "pg_dump"),
            "-h",
            config["host"],
            "-p",
            str(config["port"]),
            "-U",
            config["user"],
            "-d",
            config["dbname"],
            "-Fc",
            "-f",
            str(dump),
        ],
        env=os.environ | {"PGPASSWORD": config["password"]},
        check=True,
        capture_output=True,
    )
    dump.chmod(0o600)
    subprocess.run(
        [str(binary / "pg_restore"), "--list", str(dump)], check=True, capture_output=True
    )
    initialize(credentials)
    with connect(credentials) as conn:
        migrated = dict(
            conn.execute(
                "SELECT (ip_id,release_id)::text,to_jsonb(e)-'ip_id'-'release_id' "
                "FROM ip_enrichment e"
            )
        )
    if original.keys() != migrated.keys():
        raise ValueError("Enrichment identities differ after migration")
    for key, attributes in original.items():
        row = migrated[key]
        extras = row.pop("extra_attributes")
        expected = {field: attributes.get(field) for field in ENRICHMENT_FIELDS}
        unknown = {
            field: value for field, value in attributes.items() if field not in ENRICHMENT_FIELDS
        }
        if row != expected or extras != unknown:
            raise ValueError("Enrichment values differ after migration")
    after = status(credentials)
    if after != before:
        raise ValueError("Measurement history or cached metrics changed")
    evidence = {
        "schema_version": 3,
        "backup": str(dump),
        "backup_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
        "verified_enrichment_rows": len(original),
        "typed_fields": list(ENRICHMENT_FIELDS),
        "status": after,
    }
    (folder / "migration.json").write_text(json.dumps(evidence, indent=2) + "\n")
    (ROOT / "artifacts/postgresql.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({key: value for key, value in evidence.items() if key != "status"}))


if __name__ == "__main__":
    main()
