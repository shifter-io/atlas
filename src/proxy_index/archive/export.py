"""Export one UTC day into `YYYY-MM-DD.tar.gz` (manifest.json and one CSV per table).

Every CSV is `COPY (SELECT ... ORDER BY <primary key>) TO STDOUT (FORMAT csv, HEADER)` from one
REPEATABLE READ snapshot in UTC/ISO session settings, so the same rows give the same bytes.
The tar members carry fixed headers (the day's midnight as mtime, no owner) inside a gzip
stream without a name or time, so the same CSVs give the same archive. The manifest lists every
table in load order with its role, columns, row count, size and sha256.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import tarfile
from datetime import date
from pathlib import Path

from psycopg import sql

from ..storage import connect
from . import FORMAT, FORMAT_VERSION
from .tables import SETTINGS, TABLES, Table, day_range, present, select_day

CHUNK = 1 << 20
MANIFEST = "manifest.json"


def manifest_bytes(manifest: dict) -> bytes:
    """The manifest as stored in the archive (sorted keys, so its sha256 is stable)."""
    return json.dumps(manifest, indent=1, sort_keys=True).encode()


def versions(conn) -> dict[str, int | None]:
    storage = conn.execute("SELECT max(version) FROM schema_versions").fetchone()[0]
    calculated = None
    if conn.execute("SELECT to_regclass('calculation_schema')").fetchone()[0] is not None:
        calculated = conn.execute("SELECT max(version) FROM calculation_schema").fetchone()[0]
    return {"storage_schema": storage, "calculated_schema": calculated}


def _copy_out(conn, table: Table, params: dict, directory: Path) -> dict:
    query = sql.SQL(table.query).format(**{k: sql.Literal(v) for k, v in params.items()})
    described = conn.execute(sql.SQL("SELECT * FROM ({}) q LIMIT 0").format(query))
    columns = [column.name for column in described.description]
    file = f"{table.name}.csv"
    digest, size = hashlib.sha256(), 0
    with conn.cursor() as cursor, (directory / file).open("wb") as out:
        statement = sql.SQL("COPY ({}) TO STDOUT WITH (FORMAT csv, HEADER)").format(query)
        with cursor.copy(statement) as copy:
            for chunk in copy:
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        rows = cursor.rowcount
    return {
        "name": table.name,
        "file": file,
        "role": table.role,
        "columns": columns,
        "rows": rows,
        "bytes": size,
        "sha256": digest.hexdigest(),
    }


def _member(name: str, size: int, mtime: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = size, mtime, 0o644
    return info


def _pack(parts: Path, manifest: dict, path: Path) -> None:
    mtime = int(day_range(date.fromisoformat(manifest["day"]))[0].timestamp())
    partial = path.with_name(path.name + ".partial")
    with (
        partial.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as stream,
        tarfile.open(fileobj=stream, mode="w", format=tarfile.GNU_FORMAT) as archive,
    ):
        data = manifest_bytes(manifest)
        archive.addfile(_member(MANIFEST, len(data), mtime), io.BytesIO(data))
        for entry in manifest["tables"]:
            with (parts / entry["file"]).open("rb") as source:
                archive.addfile(_member(entry["file"], entry["bytes"], mtime), source)
    partial.replace(path)


def export_day(database, day: date, directory: Path) -> tuple[Path, dict]:
    """Write `<directory>/<day>.tar.gz`; returns its path and manifest. Deletes nothing."""
    directory = Path(directory)
    parts = directory / f"{day.isoformat()}.parts"
    shutil.rmtree(parts, ignore_errors=True)
    parts.mkdir(parents=True)
    try:
        with connect(database) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            for statement in SETTINGS:
                conn.execute(statement)
            params = select_day(conn, day, dimensions=True)
            manifest = {"format": FORMAT, "version": FORMAT_VERSION, "day": day.isoformat()}
            manifest |= versions(conn)
            manifest["tables"] = [
                _copy_out(conn, table, params, parts) for table in TABLES if present(conn, table)
            ]
        path = directory / f"{day.isoformat()}.tar.gz"
        _pack(parts, manifest, path)
    finally:
        shutil.rmtree(parts, ignore_errors=True)
    return path, manifest


def file_digests(path: Path) -> dict[str, str | int]:
    """Size, sha256 and MD5 (an S3 single-part ETag) of a finished archive."""
    sha, md5, size = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
    with Path(path).open("rb") as stream:
        while chunk := stream.read(CHUNK):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return {"bytes": size, "sha256": sha.hexdigest(), "md5": md5.hexdigest()}
