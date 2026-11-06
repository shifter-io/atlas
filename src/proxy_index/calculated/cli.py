"""Explicit scheduling-friendly calculation controls; serving never starts collection."""

import json
from pathlib import Path

from . import store


def register(commands):
    for name in (
        "calculate-init",
        "calculate",
        "snapshot-publish",
        "snapshot-export",
        "run-role",
        "metric-evidence",
        "serve",
    ):
        p = commands.add_parser(name)
        p.add_argument("--database-file", required=True)
        if name == "calculate":
            p.add_argument("--methodology", default="configs/methodology.toml")
            p.add_argument("--as-of")
            p.add_argument("--publish", action="store_true")
            mode = p.add_mutually_exclusive_group()  # full until CALC-INC-2/CALC-FLEET-1
            mode.add_argument("--full", dest="mode", action="store_const", const="full")
            mode.add_argument(
                "--incremental",
                dest="mode",
                action="store_const",
                const="incremental",
                help="Read observations only for windows whose runs changed",
            )
            p.set_defaults(mode="full")
        elif name == "snapshot-publish":
            p.add_argument("snapshot")
        elif name == "snapshot-export":
            p.add_argument("path")
            p.add_argument("--snapshot", default="current")
        elif name == "run-role":
            p.add_argument("run_id")
            p.add_argument(
                "--role",
                required=True,
                choices=["exploratory", "pool_sampling", "benchmark", "monitor"],
            )
        elif name == "metric-evidence":
            p.add_argument("path")
        elif name == "serve":
            p.add_argument("--host", default="127.0.0.1")
            p.add_argument("--port", type=int, default=8081)
    expire = commands.add_parser("calculate-expire", help="Delete builds outside retention")
    expire.add_argument("--database-file", required=True)
    expire.add_argument("--days", type=int, default=store.RETAIN_DAYS)
    expire.add_argument("--per-day", type=int, default=store.RETAIN_PER_DAY)
    expire.add_argument("--dry-run", action="store_true", help="Only report what would go")
    report = commands.add_parser("report", help="Operator reports over census and pulse runs")
    kinds = report.add_subparsers(dest="report", required=True)
    traffic = kinds.add_parser("traffic", help="Wire GB per provider and test")
    traffic.add_argument("--database-file", required=True)
    traffic.add_argument("--test", choices=["census", "pulse", "load"])
    traffic.add_argument("--provider", help='"provider" or "provider/product"')
    traffic.add_argument("--country", help="Two-letter country code")
    traffic.add_argument("--region", help="Runner region slug, e.g. fra1")
    traffic.add_argument("--from", dest="start", help="Batch slot from (ISO; UTC if no zone)")
    traffic.add_argument("--to", dest="end", help="Batch slot until, exclusive")
    traffic.add_argument("--json", action="store_true", help="Print JSON instead of a table")
    pages = kinds.add_parser(
        "html", help="Self-contained HTML batch reports and an auto-refreshing index"
    )
    pages.add_argument("--database-file", required=True)
    pages.add_argument("--out", required=True, help="Output directory")
    pages.add_argument(
        "--batch", type=int, action="append", help="Only this batch (repeatable); default all"
    )
    pages.add_argument("--limit", type=int, default=500, help="Batches listed in the index")


def dispatch(args):
    db = args.database_file
    if args.command == "report" and args.report == "html":
        from .. import report_html

        result = report_html.write(db, args.out, args.batch, limit=args.limit)
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "report":
        from .. import traffic
        from ..storage import connect
        from .aggregate import iso

        with connect(db) as conn:
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
    if args.command == "calculate-expire":
        result = store.retention(db, args.days, args.per_day, dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "calculate-init":
        result = store.initialize(db)
    elif args.command == "calculate":
        from .calculator import calculate

        options = {"as_of": args.as_of, "publish": args.publish, "mode": args.mode}
        payload = calculate(db, args.methodology, **options)
        result = {k: payload[k] for k in ("snapshot_id", "as_of", "readiness")}
    elif args.command == "snapshot-publish":
        result = store.publish(db, args.snapshot)
    elif args.command == "snapshot-export":
        result = store.export(db, args.path, args.snapshot)
    elif args.command == "run-role":
        result = store.assign_role(db, args.run_id, args.role)
    elif args.command == "metric-evidence":
        result = store.evidence(db, json.loads(Path(args.path).read_text()))
    elif args.command == "serve":
        from .api import serve

        serve(db, args.host, args.port)
        return 0
    print(json.dumps(result, indent=2))
    return 0
