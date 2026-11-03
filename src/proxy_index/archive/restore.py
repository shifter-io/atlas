"""Load a day file back into a database with the same storage schema version.

One transaction. Every CSV is COPYed into a temporary table (header names checked), its
sha256 and row count must match the manifest, then its rows are inserted keeping their ids
(`OVERRIDING SYSTEM VALUE ... ON CONFLICT DO NOTHING`, so rows already present stay as they
are). Before inserting, the ids are checked: a key that exists with other values, or a unique
value (an IP, a run id, a batch slot, a place...) that exists under another key, refuses the
restore, since the file's ids then mean other rows there (restore into the database the file
came from, or into an empty one). Links that point outside the file and the database become
NULL; missing partitions are created; identity sequences move past the restored ids.
"""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from psycopg import sql

from ..partitions import partition
from ..storage import connect
from . import FORMAT, FORMAT_VERSION, ArchiveError
from .export import CHUNK, MANIFEST
from .tables import BY_NAME, OPTIONAL, PARTITIONED, Table

MAX_MANIFEST = 1 << 20
COLUMN = re.compile(r"[a-z_][a-z0-9_]{0,62}")
SEQUENCE = re.compile(r'[A-Za-z0-9_."]+')


def _manifest(archive: tarfile.TarFile, member: tarfile.TarInfo | None) -> dict:
    if member is None or member.name != MANIFEST or not member.isreg():
        raise ArchiveError("the archive does not start with manifest.json")
    if member.size > MAX_MANIFEST:
        raise ArchiveError("manifest.json is too large")
    manifest = json.loads(archive.extractfile(member).read())
    if manifest.get("format") != FORMAT or manifest.get("version") != FORMAT_VERSION:
        raise ArchiveError("not a proxy-index raw day archive of a known version")
    for entry in manifest.get("tables", []):
        table = BY_NAME.get(entry.get("name"))
        if (
            table is None
            or entry.get("role") != table.role
            or entry.get("file") != f"{table.name}.csv"
            or not all(COLUMN.fullmatch(str(c)) for c in entry.get("columns", []))
        ):
            raise ArchiveError(f"unknown table entry in the manifest: {entry.get('name')!r}")
    return manifest


def _row(columns: tuple[str, ...], alias: str) -> str:
    return "(" + ",".join(f"{alias}.{c}" for c in columns) + ")"


def _id_conflicts(conn, table: Table, tmp: str) -> list[str]:
    """What makes the file's ids mean other rows in the target (empty when they agree)."""
    found = []
    if table.natural:
        on = " AND ".join(f"d.{c}=t.{c}" for c in table.key)
        differs = f"{_row(table.natural, 'd')} IS DISTINCT FROM {_row(table.natural, 't')}"
        found.append(("a key with other values", on, differs))
    if table.unique:
        if table.nullable:
            on = f"{_row(table.natural, 'd')} IS NOT DISTINCT FROM {_row(table.natural, 't')}"
        else:
            on = " AND ".join(f"d.{c}=t.{c}" for c in table.natural)
        differs = f"{_row(table.key, 'd')} IS DISTINCT FROM {_row(table.key, 't')}"
        found.append(("values under another key", on, differs))
    conflicts = []
    for what, on, differs in found:
        query = sql.SQL("SELECT count(*) FROM {} t JOIN {} d ON " + on + " WHERE " + differs)
        count = conn.execute(
            query.format(sql.Identifier(tmp), sql.Identifier(table.name))
        ).fetchone()[0]
        if count:
            conflicts.append(f"{table.name}: {count} rows with {what}")
    return conflicts


def _prepare(conn, table: Table, tmp: str) -> None:
    """Null links to rows that are neither here nor in the file; create missing partitions."""
    if table.name in OPTIONAL:  # both point at test_batches(batch_id)
        column, parent = OPTIONAL[table.name]
        query = (
            "UPDATE {tmp} t SET {col}=NULL WHERE t.{col} IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM {parent} p WHERE p.batch_id=t.{col})"
        )
        if parent == table.name:  # a retry of a batch of the same file
            query += " AND NOT EXISTS (SELECT 1 FROM {tmp} p WHERE p.batch_id=t.{col})"
        names = {"tmp": tmp, "col": column, "parent": parent}
        conn.execute(sql.SQL(query).format(**{k: sql.Identifier(v) for k, v in names.items()}))
    if table.name in PARTITIONED:
        days = conn.execute(
            sql.SQL("SELECT DISTINCT ({} AT TIME ZONE 'UTC')::date FROM {}").format(
                sql.Identifier(PARTITIONED[table.name]), sql.Identifier(tmp)
            )
        ).fetchall()
        for (day,) in days:
            partition(conn, table.name, datetime(day.year, day.month, day.day, tzinfo=UTC))


def _load(conn, table: Table, entry: dict, stream) -> int:
    """COPY one CSV into a temporary table, check it, insert it; returns the rows inserted."""
    tmp = f"restore_{table.name}"
    conn.execute(
        sql.SQL("CREATE TEMP TABLE {} (LIKE {} INCLUDING DEFAULTS) ON COMMIT DROP").format(
            sql.Identifier(tmp), sql.Identifier(table.name)
        )
    )
    columns = sql.SQL(",").join(map(sql.Identifier, entry["columns"]))
    digest = hashlib.sha256()
    with conn.cursor() as cursor:
        statement = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER MATCH)")
        with cursor.copy(statement.format(sql.Identifier(tmp), columns)) as copy:
            while chunk := stream.read(CHUNK):
                digest.update(chunk)
                copy.write(chunk)
        rows = cursor.rowcount
    if digest.hexdigest() != entry["sha256"] or rows != entry["rows"]:
        raise ArchiveError(f"{entry['file']} does not match its manifest entry")
    conflicts = _id_conflicts(conn, table, tmp)
    if conflicts:
        raise ArchiveError(
            "the database's ids differ from the archive's (" + "; ".join(conflicts) + "): "
            "restore into the database the archive came from, or into an empty one"
        )
    _prepare(conn, table, tmp)
    insert = sql.SQL(
        "INSERT INTO {} ({}) OVERRIDING SYSTEM VALUE SELECT {} FROM {} ON CONFLICT DO NOTHING"
    )
    target = sql.Identifier(table.name)
    return conn.execute(insert.format(target, columns, columns, sql.Identifier(tmp))).rowcount


def _advance_identities(conn, table: str) -> None:
    """Move every identity sequence of the table past its largest restored id."""
    columns = conn.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass AND attidentity<>'' "
        "AND NOT attisdropped",
        (table,),
    ).fetchall()
    for (column,) in columns:
        sequence = conn.execute("SELECT pg_get_serial_sequence(%s,%s)", (table, column)).fetchone()[
            0
        ]
        if not SEQUENCE.fullmatch(sequence):
            raise ArchiveError(f"unexpected sequence name for {table}.{column}")
        conn.execute(
            sql.SQL(
                "SELECT setval(%s::regclass,m) FROM (SELECT max({}) AS m FROM {}) x WHERE m>"
                "(SELECT CASE WHEN is_called THEN last_value ELSE last_value-1 END FROM {})"
            ).format(sql.Identifier(column), sql.Identifier(table), sql.SQL(sequence)),
            (sequence,),
        )


def _check_target(conn, manifest: dict) -> None:
    if conn.execute("SELECT to_regclass('schema_versions')").fetchone()[0] is None:
        raise ArchiveError("the database has no proxy-index schema: run db-init first")
    version = conn.execute("SELECT max(version) FROM schema_versions").fetchone()[0]
    if version != manifest["storage_schema"]:
        raise ArchiveError(
            f"the archive has storage schema v{manifest['storage_schema']}, "
            f"the database v{version}: restore into a database of the archive's version"
        )


def restore_file(path: Path, database) -> dict:
    """Load one day file; returns the day and the rows inserted per table."""
    inserted = {}
    with tarfile.open(path, "r:gz") as archive, connect(database) as conn:
        members = iter(archive)
        manifest = _manifest(archive, next(members, None))
        _check_target(conn, manifest)
        conn.execute("SET LOCAL TimeZone='UTC'")
        for entry in manifest["tables"]:
            member = next(members, None)
            if member is None or member.name != entry["file"] or not member.isreg():
                raise ArchiveError(f"{entry['file']} is missing or out of order")
            table = BY_NAME[entry["name"]]
            if conn.execute("SELECT to_regclass(%s)", (table.name,)).fetchone()[0] is None:
                if entry["rows"]:
                    raise ArchiveError(f"the database has no table {table.name}")
                continue
            inserted[table.name] = _load(conn, table, entry, archive.extractfile(member))
        if next(members, None) is not None:
            raise ArchiveError("the archive holds files its manifest does not list")
        for name, rows in inserted.items():
            if rows:
                _advance_identities(conn, name)
    return {"day": manifest["day"], "inserted": inserted}
