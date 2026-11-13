"""`atlas daily plan|run|ingest|hour|check|audits|tape` (operator; `hour` and `check` on the
manager).

`plan` prints the hour's droplet plan and writes nothing (a dry run). `run` runs the hour's test
on the manager and exits 3 when a worker is not verified gone or the hour did not finish.
`ingest` downloads a finished test (`campaign collect`), imports it and emits its events (with
`--formulas`, also its estimated hours); it exits 3 when a cell failed, an imported country got
no estimates or the download did not verify. `audits` writes the audits of the daily runs already
stored in a time range, hour by hour (daily.audits.backfill), and prints one JSON line per hour;
with `--dry-run` it writes nothing. `tape` runs the live tape's detector over the batches
that finished in a time range (daily.tape.replay) and prints one JSON line: the events per kind
and network, the range's outages and the seconds; with `--dry-run` it writes nothing. Both
write to production's proxy_reviews only with #817's owner go, acknowledgement and environment
(production_guard); else they exit 2.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
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
    ingest.add_argument(
        "--spool", type=Path, help="keep each imported run's raw files there (raw.spool_dir)"
    )
    ingest.add_argument(
        "--batch", type=int, help="daily tests stored per transaction (default 20, at least 1)"
    )
    ingest.add_argument(
        "--connections", type=int, help="transactions at once (default 25, at least 1)"
    )
    ingest.add_argument(
        "--processes",
        type=int,
        help="worker processes the transactions go over (default min(7, CPUs - 1, connections))",
    )
    hour = actions.add_parser("hour", help="Manager: run the next hour's test, then ingest it")
    check = actions.add_parser("check", help="Check formulas and template for all 24 hours")
    for action in (hour, check):
        action.add_argument("--formulas", type=Path, required=True, help="formulas/*.json")
        action.add_argument("--template", type=Path, required=True, help="A planned campaign root")
    hour.add_argument("--database-file", type=Path, required=True)
    hour.add_argument("--mmdb", type=Path)
    hour.add_argument("--start", default="next-hour", help="next-hour, now-hour or an ISO hour")
    _register_audits(actions)


def _moment(value: str) -> datetime:
    """An ISO time with its offset (argparse type)."""
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() is None:
        raise ValueError("the time needs its offset, for example 2026-11-10T00:00:00+00:00")
    return moment


def _register_audits(actions) -> None:
    from ..production_guard import ACKNOWLEDGE, PRODUCTION_GO_FORM

    audits = actions.add_parser("audits", help="Write the audits of stored daily runs, by hour")
    tape = actions.add_parser("tape", help="Run the live tape's detector over stored batches")
    tape.add_argument("--step-minutes", type=int, default=5, help="detector call spacing")
    for action in (audits, tape):
        action.add_argument("--database-file", type=Path, required=True)
        action.add_argument("--from", dest="since", type=_moment, required=True, help="ISO time")
        action.add_argument("--to", dest="until", type=_moment, required=True, help="ISO time")
        action.add_argument("--dry-run", action="store_true", help="report, then roll back")
        action.add_argument("--owner-go", help=f"production's go: {PRODUCTION_GO_FORM}")
        action.add_argument(ACKNOWLEDGE, action="store_true", help="the target is production")


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
    given = {"batch": args.batch, "connections": args.connections, "processes": args.processes}
    batch = {k: v for k, v in given.items() if v is not None}  # else ingest_campaign's
    report = asyncio.run(
        ingest_campaign(
            dest, args.database_file, args.mmdb, formulas=args.formulas, spool=args.spool, **batch
        )
    )
    print(json.dumps({k: report[k] for k in ("campaign_id", "complete")}, indent=2))
    return 0 if report["complete"] else 3


def _hour(args) -> int:
    """The hourly timer's job: exit 3 unless the hour ran clean and its import is complete."""
    from ..campaign.cli import cancel_on_signals
    from .hourly import run_and_ingest

    async def run() -> dict:
        cancel_on_signals()
        return await run_and_ingest(
            args.formulas, args.template.resolve(), args.database_file, args.mmdb, args.start
        )

    outcome = asyncio.run(run())
    print(json.dumps(outcome, indent=2))
    if outcome["state"] == "nothing_due":
        return 0
    clean = outcome["state"] == "done" and not outcome["leftover"]
    return 0 if clean and outcome["ingest"]["complete"] else 3


def _check(args) -> int:
    """One JSON line; exit 0 whether or not it found issues (`ok`)."""
    from .hourly import check

    print(json.dumps(check(args.formulas, args.template), separators=(",", ":")))
    return 0


def _audits(args) -> int:
    """One JSON line per stored hour; exit 2 when the production guard refuses."""
    from ..storage import connect
    from .audits import backfill, target_refusal

    with connect(args.database_file) as conn:
        found = target_refusal(conn, args.owner_go, args.i_know_this_is_production)
        if found:
            print(f"refused: {found}", file=sys.stderr)
            return 2
        for line in backfill(conn, args.since, args.until, dry_run=args.dry_run):
            print(json.dumps(line), flush=True)
    return 0


def _tape(args) -> int:
    """One JSON line; exit 2 when the production guard refuses."""
    from datetime import timedelta

    from ..storage import connect
    from .audits import target_refusal
    from .tape import replay

    if args.step_minutes < 1 or args.until <= args.since:
        print("refused: --step-minutes is at least 1 and --to after --from", file=sys.stderr)
        return 2
    with connect(args.database_file) as conn:
        found = target_refusal(conn, args.owner_go, args.i_know_this_is_production)
        if found:
            print(f"refused: {found}", file=sys.stderr)
            return 2
        step = timedelta(minutes=args.step_minutes)
        report = replay(conn, args.since, args.until, step=step, dry_run=args.dry_run)
    print(json.dumps(report))
    return 0


HANDLERS = {
    "plan": _plan,
    "run": _run,
    "ingest": _ingest,
    "hour": _hour,
    "check": _check,
    "audits": _audits,
    "tape": _tape,
}


def dispatch(args) -> int:
    return HANDLERS[args.daily_action](args)
