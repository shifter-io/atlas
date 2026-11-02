"""`proxy-index daily plan|run|ingest` (operator).

`plan` prints the hour's droplet plan and writes nothing (a dry run). `run` runs the hour's test
on the manager and exits 3 when a worker is not verified gone or the hour did not finish.
`ingest` downloads a finished test (`campaign collect`), imports it and emits its events (with
`--formulas`, also its estimated hours); it exits 3 when a cell failed, an imported country got
no estimates or the download did not verify.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from ..campaign.tick import utc_now
from .planner import load_schedules, plan_hour


def register(commands) -> None:
    parser = commands.add_parser("daily", help="Hourly daily test of the countries due (operator)")
    actions = parser.add_subparsers(dest="daily_action", required=True)
    for name, text in (("plan", "Print the hour's droplet plan (dry run)"), ("run", "Run it")):
        action = actions.add_parser(name, help=text)
        action.add_argument("--formulas", type=Path, required=True, help="formulas/*.json")
        action.add_argument(
            "--template", type=Path, required=name == "run", help="A planned campaign root"
        )
        action.add_argument(
            "--start", default="now-hour", help="now-hour, next-hour or an ISO hour with offset"
        )
    ingest = actions.add_parser("ingest", help="Download, import and emit a finished test")
    ingest.add_argument("--inventory", type=Path, required=True)
    ingest.add_argument("--campaign-id", required=True)
    ingest.add_argument("--dest", type=Path, required=True)
    ingest.add_argument("--database-file", type=Path, required=True)
    ingest.add_argument("--mmdb", type=Path)
    ingest.add_argument(
        "--formulas", type=Path, help="formulas/*.json: also store and release the estimated hours"
    )


def _plan(args) -> int:
    from .runner import RunInputs, hour_plan, start_hour

    now = utc_now()
    if args.template is None:
        begins, _ = start_hour(args.start, now)
        plan = plan_hour(load_schedules(args.formulas), begins.hour)
    else:
        _, plan, begins, _ = hour_plan(RunInputs(args.template, args.formulas, args.start), now)
    print(json.dumps({"starts_at": begins.isoformat()} | plan.to_dict(), indent=2))
    return 0


def _run(args) -> int:
    from ..campaign.cli import cancel_on_signals
    from .runner import RunInputs, run_hour

    async def run() -> dict:
        cancel_on_signals()  # a signal cancels the run; its clean-up still removes the workers
        return await run_hour(RunInputs(args.template, args.formulas, args.start))

    outcome = asyncio.run(run())
    print(json.dumps(outcome, indent=2))
    if outcome["state"] == "nothing_due":
        return 0
    return 0 if outcome["state"] == "done" and not outcome["leftover"] else 3


def _ingest(args) -> int:
    from ..campaign.collect import REPORT, collect
    from .ingest import ingest_campaign

    dest = args.dest.expanduser()
    collected = dest / REPORT
    if not collected.exists():  # a rerun reuses the verified download
        collect(args.inventory, args.campaign_id, dest)
    if not json.loads(collected.read_text())["clean"]:
        print("Not ingested: the download did not verify", file=sys.stderr)
        return 3
    report = asyncio.run(
        ingest_campaign(dest, args.database_file, args.mmdb, formulas=args.formulas)
    )
    print(json.dumps({k: report[k] for k in ("campaign_id", "complete")}, indent=2))
    return 0 if report["complete"] else 3


HANDLERS = {"plan": _plan, "run": _run, "ingest": _ingest}


def dispatch(args) -> int:
    return HANDLERS[args.daily_action](args)
