"""`proxy-index db-server check|analyze` (operator).

`check` prints the read-only report as JSON (exit 0 whether or not it found gaps), or with
`--sql` only the statements that close the gaps, as SQL to run once with the admin config.
`analyze` prints one JSON health line and exits 1 when it is not "ok".
"""

from __future__ import annotations

import json
from pathlib import Path

from . import Roles


def register(commands) -> None:
    parser = commands.add_parser(
        "db-server", help="Database server: read-only check and the nightly ANALYZE (operator)"
    )
    actions = parser.add_subparsers(dest="db_server_action", required=True)
    check = actions.add_parser(
        "check", help="Read-only report, with the statements that close its gaps"
    )
    check.add_argument("--database-file", type=Path, required=True, help="Read in place")
    for field, default in Roles()._asdict().items():
        check.add_argument(f"--{field}-role", default=default, help=f"Default {default}")
    check.add_argument("--sql", action="store_true", help="Print only the statements, as SQL")
    analyze = actions.add_parser(
        "analyze", help="ANALYZE the partitioned parents; prints one health line"
    )
    analyze.add_argument("--database-file", type=Path, required=True, help="Read in place")


def _check(args) -> int:
    from .check import check

    roles = Roles(args.runtime_role, args.api_role, args.admin_role)
    report = check(args.database_file, roles)
    if not args.sql:
        print(json.dumps(report, indent=2, default=str))
        return 0
    for gap in report["gaps"]:
        print(f"-- {gap['detail']} (run as {gap['run_as']})")
        print("\n".join(gap["statements"]))
    return 0


def _analyze(args) -> int:
    from .analyze import analyze

    line = analyze(args.database_file)
    print(json.dumps(line, separators=(",", ":")))
    return 0 if line["db_server_analyze"] == "ok" else 1


HANDLERS = {"check": _check, "analyze": _analyze}


def dispatch(args) -> int:
    return HANDLERS[args.db_server_action](args)
