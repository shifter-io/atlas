"""`proxy-index archive export|restore|run|bucket` (operator).

`export` writes one day's file (`<out>/YYYY-MM-DD.tar.gz`) and deletes nothing. `restore`
loads a day file (`--file`, or `--date` from the bucket) into a database with the same storage
schema. `run` archives the due days now (what the orchestrator's daily `archive` job does).
`bucket` checks that the bucket is private and versioned (`--apply` enables versioning).
`rebuild` imports the spooled raw files of some days into a database (rawfiles.rebuild) and,
with `--spaces-file`, the browser bundles archived in Spaces for those days.
Refusals exit 2 and change nothing.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, date, datetime
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
    source = restore.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="A YYYY-MM-DD.tar.gz file")
    source.add_argument("--date", type=date.fromisoformat, help="The day's file in the bucket")
    restore.add_argument("--database-file", type=Path, required=True)
    restore.add_argument("--spaces-file", type=Path, help="[spaces] settings (with --date)")
    run = actions.add_parser("run", help="Archive and delete the due days now")
    run.add_argument("--database-file", type=Path, required=True)
    run.add_argument("--spaces-file", type=Path, required=True)
    run.add_argument("--work-dir", type=Path, required=True)
    run.add_argument("--max-days", type=int, default=3)
    rebuild = actions.add_parser("rebuild", help="Import the spooled raw files of some days")
    rebuild.add_argument("--spool", type=Path, help="raw.spool_dir")
    rebuild.add_argument("--spaces-file", type=Path, help="Also the browser bundles in Spaces")
    rebuild.add_argument("--browser-prefix", default="browser-files", help="browser.archive_prefix")
    rebuild.add_argument("--from", dest="first", type=date.fromisoformat, required=True)
    rebuild.add_argument("--to", dest="last", type=date.fromisoformat, required=True)
    rebuild.add_argument("--database-file", type=Path, required=True)
    bucket = actions.add_parser("bucket", help="Check the bucket is private and versioned")
    bucket.add_argument("--spaces-file", type=Path, required=True)
    bucket.add_argument("--apply", action="store_true", help="Enable versioning")


def _export(args) -> int:
    from .export import export_day, file_digests

    path, manifest = export_day(args.database_file, args.date, args.out)
    rows = {t["name"]: t["rows"] for t in manifest["tables"]}
    print(json.dumps({"file": str(path), **file_digests(path), "rows": rows}, indent=2))
    return 0


def _restore(args) -> dict:
    from .job import object_key
    from .restore import restore_file
    from .spaces import Spaces

    if args.file:
        return restore_file(args.file, args.database_file)
    if not args.spaces_file:
        raise ArchiveError("--date needs --spaces-file")
    with tempfile.TemporaryDirectory(prefix="proxy-index-restore-") as temporary:
        path = Path(temporary, f"{args.date.isoformat()}.tar.gz")
        Spaces.from_file(args.spaces_file).get_file(object_key(args.date), path)
        return restore_file(path, args.database_file)


def _run(args) -> dict:
    from .job import run_due
    from .spaces import Spaces

    spaces = Spaces.from_file(args.spaces_file)
    now = datetime.now(UTC)
    return run_due(args.database_file, spaces, args.work_dir, now, max_days=args.max_days)


def _bucket(args) -> dict:
    from .spaces import Spaces

    spaces = Spaces.from_file(args.spaces_file)
    if args.apply and spaces.versioning() != "Enabled":
        spaces.enable_versioning()
    report = {"bucket": spaces.bucket, "versioning": spaces.versioning()}
    report["public_grants"] = spaces.public_grants()
    if report["public_grants"] or report["versioning"] != "Enabled":
        raise ArchiveError(f"the bucket is not private and versioned: {report}")
    return report


def _rebuild(args) -> dict:
    from .rawfiles import rebuild, rebuild_browser
    from .spaces import Spaces

    if not args.spool and not args.spaces_file:
        raise ArchiveError("rebuild needs --spool, --spaces-file or both")
    report = rebuild(args.spool, args.database_file, args.first, args.last) if args.spool else {}
    if args.spaces_file:
        spaces = Spaces.from_file(args.spaces_file)
        report["browser"] = rebuild_browser(
            spaces, args.database_file, args.browser_prefix, args.first, args.last
        )
    return report


HANDLERS = {"restore": _restore, "run": _run, "bucket": _bucket, "rebuild": _rebuild}


def dispatch(args) -> int:
    if args.archive_action == "export":
        return _export(args)
    try:
        report = HANDLERS[args.archive_action](args)
    except ArchiveError as exc:
        print(f"Archive {args.archive_action} refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0
