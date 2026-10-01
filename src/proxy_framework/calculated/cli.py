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


def dispatch(args):
    db = args.database_file
    if args.command == "calculate-init":
        result = store.initialize(db)
    elif args.command == "calculate":
        from .calculator import calculate

        payload = calculate(db, args.methodology, as_of=args.as_of, publish=args.publish)
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
