"""Explicit scheduling-friendly calculation controls; serving never starts collection."""

import json
import sys
from pathlib import Path

from ..storage import connect
from . import store


def _parser(commands, name, **kwargs):
    p = commands.add_parser(name, **kwargs)
    p.add_argument("--database-file", required=True)
    return p


def _calculate_parser(commands, name):
    p = _parser(commands, name)
    p.add_argument("--methodology", default="configs/methodology.toml")
    p.add_argument("--as-of")
    p.add_argument("--publish", action="store_true")
    mode = p.add_mutually_exclusive_group()  # incremental by default
    mode.add_argument(
        "--full",
        dest="mode",
        action="store_const",
        const="recount",
        help="Count every window again in the database, reusing no stored list",
    )
    mode.add_argument(
        "--oracle",
        dest="mode",
        action="store_const",
        const="full",
        help="Read and aggregate every observation of the window in Python (tests, small data)",
    )
    mode.add_argument(
        "--incremental",
        dest="mode",
        action="store_const",
        const="incremental",
        help="Read observations only for windows whose runs changed (the default)",
    )
    p.set_defaults(mode="incremental")
    p.add_argument(
        "--parallel-reads",
        type=int,
        help="Helper connections reading the missed windows (overrides [calculate] parallel_reads)",
    )


def _backfill_parser(commands, name):
    p = _parser(commands, name, help="Recompute the Atlas Score trend points the history lacks")
    p.add_argument("--methodology", default="configs/methodology.toml")
    p.add_argument("--hours", type=int, default=23, help="Past hours of the 24h trend (0-23)")
    p.add_argument("--days", type=int, default=29, help="Past UTC days of the 30d trend (0-29)")
    p.add_argument("--limit", type=int, help="Points to compute in this run (default all)")
    p.add_argument("--dry-run", action="store_true", help="Compute the points, record nothing")
    only = p.add_mutually_exclusive_group()
    only.add_argument("--list", action="store_true", help="Only list the missing points")
    only.add_argument("--export", metavar="PATH", help="Only write the recomputed records out")
    only.add_argument("--import", dest="load", metavar="PATH", help="Only record exported ones")


def _publish_parser(commands, name):
    _parser(commands, name).add_argument("snapshot")


def _export_parser(commands, name):
    p = _parser(commands, name)
    p.add_argument("path")
    p.add_argument("--snapshot", default="current")


def _role_parser(commands, name):
    p = _parser(commands, name)
    p.add_argument("run_id")
    p.add_argument(
        "--role",
        required=True,
        choices=["exploratory", "pool_sampling", "benchmark", "monitor"],
    )


def _evidence_parser(commands, name):
    _parser(commands, name).add_argument("path")


def _serve_parser(commands, name):
    p = _parser(commands, name)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8081)


def _expire_parser(commands, name):
    expire = _parser(commands, name, help="Delete builds outside retention")
    expire.add_argument("--days", type=int, default=store.RETAIN_DAYS)
    expire.add_argument("--per-day", type=int, default=store.RETAIN_PER_DAY)
    expire.add_argument("--dry-run", action="store_true", help="Only report what would go")


def _report_parser(commands, name):
    report = commands.add_parser(name, help="Operator reports over census and pulse runs")
    kinds = report.add_subparsers(dest="report", required=True)
    traffic = _parser(kinds, "traffic", help="Wire GB per provider and test")
    traffic.add_argument("--test", choices=["census", "pulse", "load"])
    traffic.add_argument("--provider", help='"provider" or "provider/product"')
    traffic.add_argument("--country", help="Two-letter country code")
    traffic.add_argument("--region", help="Runner region slug, e.g. fra1")
    traffic.add_argument("--from", dest="start", help="Batch slot from (ISO; UTC if no zone)")
    traffic.add_argument("--to", dest="end", help="Batch slot until, exclusive")
    traffic.add_argument("--json", action="store_true", help="Print JSON instead of a table")
    pages = _parser(
        kinds, "html", help="Self-contained HTML batch reports and an auto-refreshing index"
    )
    pages.add_argument("--out", required=True, help="Output directory")
    pages.add_argument(
        "--batch", type=int, action="append", help="Only this batch (repeatable); default all"
    )
    pages.add_argument("--limit", type=int, default=500, help="Batches listed in the index")


def _show(result):
    print(json.dumps(result, indent=2))
    return 0


def _init(args):
    return _show(store.initialize(args.database_file))


def _calculate(args):
    from .calculator import calculate

    options = {"as_of": args.as_of, "publish": args.publish, "mode": args.mode}
    if args.parallel_reads is not None:  # else the calculate.parallel_reads setting
        options["parallel_reads"] = args.parallel_reads
    payload = calculate(args.database_file, args.methodology, **options)
    return _show({k: payload[k] for k in ("snapshot_id", "as_of", "readiness")})


def _backfill(args):
    from . import score_backfill
    from .policy import load
    from .score_history import SLOTS

    if not (0 <= args.hours < SLOTS["24h"] and 0 <= args.days < SLOTS["30d"]):
        raise SystemExit("--hours must be 0-23 and --days 0-29")
    if args.export:
        method_id = load(args.methodology)[1]
        return _show(score_backfill.export(args.database_file, method_id, args.export))
    if args.list:
        method_id = load(args.methodology)[1]
        with connect(args.database_file) as conn:
            as_of, todo = score_backfill.missing(conn, method_id, args.hours, args.days)
        left = [at.isoformat() for at in todo]
        return _show({"current_as_of": as_of and as_of.isoformat(), "left": left})
    options = {"hours": args.hours, "days": args.days, "limit": args.limit, "dry_run": args.dry_run}
    try:
        if args.load:
            return _show(score_backfill.import_scores(args.database_file, args.load))
        return _show(score_backfill.backfill(args.database_file, args.methodology, **options))
    except score_backfill.Busy as busy:
        print(busy, file=sys.stderr)
        return 3


def _publish(args):
    return _show(store.publish(args.database_file, args.snapshot))


def _export(args):
    return _show(store.export(args.database_file, args.path, args.snapshot))


def _role(args):
    return _show(store.assign_role(args.database_file, args.run_id, args.role))


def _evidence(args):
    return _show(store.evidence(args.database_file, json.loads(Path(args.path).read_text())))


def _serve(args):
    from .api import serve

    serve(args.database_file, args.host, args.port)
    return 0


def _expire(args):
    result = store.retention(args.database_file, args.days, args.per_day, dry_run=args.dry_run)
    return _show(result)


def _report_html(args):
    from .. import report_html

    return _show(report_html.write(args.database_file, args.out, args.batch, limit=args.limit))


def _report_traffic(args):
    from .. import traffic
    from .aggregate import iso

    with connect(args.database_file) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        inputs = traffic.TrafficInputs(
            test=args.test,
            provider=args.provider,
            country=args.country,
            region=args.region,
            start=traffic.parse_time(args.start),
            end=traffic.parse_time(args.end),
        )
        report = traffic.summary(conn, inputs)
    print(json.dumps(report, indent=2, default=iso) if args.json else traffic.table(report))
    return 0


def _report(args):
    return _report_html(args) if args.report == "html" else _report_traffic(args)


# Subcommand -> (parser builder, handler), in the order the commands appear in --help.
COMMANDS = {
    "calculate-init": (_parser, _init),
    "calculate": (_calculate_parser, _calculate),
    "calculate-backfill-scores": (_backfill_parser, _backfill),
    "snapshot-publish": (_publish_parser, _publish),
    "snapshot-export": (_export_parser, _export),
    "run-role": (_role_parser, _role),
    "metric-evidence": (_evidence_parser, _evidence),
    "serve": (_serve_parser, _serve),
    "calculate-expire": (_expire_parser, _expire),
    "report": (_report_parser, _report),
}


def register(commands):
    for name, (build, _) in COMMANDS.items():
        build(commands, name)


def dispatch(args):
    return COMMANDS[args.command][1](args)
