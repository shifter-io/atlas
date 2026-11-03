"""`proxy-index archive export|restore` (operator).

`export` writes one day's file (`<out>/YYYY-MM-DD.tar.gz`) and deletes nothing. `restore`
loads a day file into a database with the same storage schema; it exits 2 when the file or
the database does not fit (nothing is changed then).
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from . import ArchiveError


def register(commands) -> None:
    parser = commands.add_parser("archive", help="Daily raw-data archive files (operator)")
    actions = parser.add_subparsers(dest="archive_action", required=True)
    export = actions.add_parser("export", help="Write one UTC day's archive file (no deletion)")
    export.add_argument("--date", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    export.add_argument("--database-file", type=Path, required=True)
    export.add_argument("--out", type=Path, required=True, help="Directory for the file")
    restore = actions.add_parser("restore", help="Load a day's archive file into a database")
    restore.add_argument("--file", type=Path, required=True, help="A YYYY-MM-DD.tar.gz file")
    restore.add_argument("--database-file", type=Path, required=True)


def _export(args) -> int:
    from .export import export_day, file_digests

    path, manifest = export_day(args.database_file, args.date, args.out)
    rows = {t["name"]: t["rows"] for t in manifest["tables"]}
    print(json.dumps({"file": str(path), **file_digests(path), "rows": rows}, indent=2))
    return 0


def _restore(args) -> int:
    from .restore import restore_file

    try:
        report = restore_file(args.file, args.database_file)
    except ArchiveError as exc:
        print(f"Restore refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


HANDLERS = {"export": _export, "restore": _restore}


def dispatch(args) -> int:
    return HANDLERS[args.archive_action](args)
