"""`atlas daily plan|pulse-plan|pulse-hour|pulse-backfill|run|ingest|hour|check|audits|tape|
verify-hour|droplets|image|resume-network` (operator; `hour`, `pulse-hour`, `pulse-switch`,
`pulse-backfill`, `check` and `resume-network` on the manager).

`plan` prints the hour's droplet plan and writes nothing (a dry run). `pulse-plan` prints the
hour's on-demand pulse droplets (daily.pulses, a dry run) and exits 3 when they are over the
cap (`--max-droplets`, default 144) or, with `--root`, when the pulse root's `fleet.json` is
refused (pulse_fleet.load_config: a snapshot name for an id; its networks then plan).
`pulse-hour` runs one hour of on-demand pulses (campaign.pulse_ondemand) and exits 3 unless
the hour is done with no droplet left up (results only waiting to be stored are done; a cell
handed to another region's droplet is not lost), each such hour having opened its alert.
`pulse-switch on` turns the orchestrator to on-demand hourly pulses (daily.pulses.switch;
`status` only reads). `pulse-backfill` counts the exit IPs of the pulse runs stored without
them, with the manager's DB-IP release, from the raw spool (daily.pulse_backfill; only counts
without `--apply`, exits 3 when an applied one leaves some uncounted). `run` runs the hour's
test on the manager and exits 3 when a worker is not verified gone or the hour did not finish.
`ingest` downloads a finished test (`campaign collect`), imports it and emits its events (with
`--formulas`, also its estimated hours); it exits 3 when a cell failed, an imported country got
no estimates or the download did not verify. `audits` writes the audits of the daily runs already
stored in a time range, hour by hour (daily.audits.backfill), and prints one JSON line per hour;
with `--dry-run` it writes nothing. `tape` runs the live tape's detector over the batches
that finished in a time range (daily.tape.replay) and prints one JSON line: the events per kind
and network, the range's outages and the seconds; with `--dry-run` it writes nothing. Both
write to production's proxy_reviews only with #817's owner go, acknowledgement and environment
(production_guard); else they exit 2. `verify-hour` prints one hour's read-only health report
(daily.verify, with `--no-cloud` without daily.verify_cloud's droplets and archive) and exits 3
unless everything due is there. `droplets` lists the test and pulse droplets, read only, for the
Slack alerts (MONITOR-1): one `<subject>\t<ok|failed>\t<detail>` line per tag's orphans and for
the count against `--cap`, exit 3 when one failed. `image` checks the worker image against the
code (daily.image_check: its locked dependencies and its DB-IP release's age), in the same form.
`resume-network` resumes a network paused for a refused login (orchestrator.login_pause) once a
small login test through its pulse config passes; it exits 3 while the test does not pass.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from ..campaign.tick import utc_now
from .planner import load_schedules, plan_hour
from .pulses import DEFAULT_CAP


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
    pulses = actions.add_parser("pulse-plan", help="Print the hour's pulse droplets (dry run)")
    pulses.add_argument("--template", type=Path, help="A planned campaign root: its networks")
    pulses.add_argument("--root", type=Path, help="a pulse root: check its fleet.json")
    pulse_run = actions.add_parser("pulse-hour", help="Manager: one hour of on-demand pulses")
    pulse_run.add_argument("--root", type=Path, required=True, help="the pulse root (fleet.json)")
    for action in (pulses, pulse_run):
        action.add_argument("--formulas", type=Path, required=True, help="formulas/*.json")
        action.add_argument("--start", default="next-hour", help="next-hour, now-hour or ISO")
        action.add_argument(
            "--max-droplets", type=int, default=DEFAULT_CAP, help="the hour's droplet cap (144)"
        )
    backfill = actions.add_parser("pulse-backfill", help="Manager: count old pulse runs' IPs")
    backfill.add_argument("--database-file", type=Path, required=True)
    backfill.add_argument("--spool", type=Path, help="the raw spool (default: raw.spool_dir)")
    backfill.add_argument("--mmdb", type=Path, help="the DB-IP file (default: $DBIP_MMDB)")
    backfill.add_argument("--batch", type=int, default=200, help="runs per transaction")
    backfill.add_argument("--apply", action="store_true", help="write (without: only count)")
    switch = actions.add_parser("pulse-switch", help="Manager: on-demand hourly pulses on")
    switch.add_argument("state", choices=["on", "status"])
    switch.add_argument("--database-file", type=Path, required=True)
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
        "--spool", type=Path, help="keep each converted run's raw files there (raw.spool_dir)"
    )
    ingest.add_argument(
        "--batch", type=int, help="daily tests per transaction (default: setting ingest.batch)"
    )
    ingest.add_argument(
        "--connections", type=int, help="transactions at once (default: ingest.connections)"
    )
    ingest.add_argument(
        "--processes",
        type=int,
        help="worker processes the transactions go over (default: ingest.processes, else sized)",
    )
    hour = actions.add_parser("hour", help="Manager: run the next hour's test, then ingest it")
    check = actions.add_parser("check", help="Check formulas and template for all 24 hours")
    for action in (hour, check):
        action.add_argument("--formulas", type=Path, required=True, help="formulas/*.json")
        action.add_argument("--template", type=Path, required=True, help="A planned campaign root")
    hour.add_argument("--database-file", type=Path, required=True)
    hour.add_argument("--mmdb", type=Path)
    hour.add_argument("--start", default="next-hour", help="next-hour, now-hour or an ISO hour")
    _register_verify(actions)
    _register_audits(actions)
    resume = actions.add_parser("resume-network", help="Manager: login test, then resume a network")
    resume.add_argument("network", help="the network paused for a refused login (fleet name)")
    resume.add_argument("--database-file", type=Path, required=True)
    resume.add_argument("--root", type=Path, help="the manager root (default $ATLAS_MANAGER_ROOT)")


def _moment(value: str) -> datetime:
    """An ISO time with its offset (argparse type)."""
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() is None:
        raise ValueError("the time needs its offset, for example 2026-11-10T00:00:00+00:00")
    return moment


def _register_verify(actions) -> None:
    verify = actions.add_parser("verify-hour", help="Read-only health report of one hour")
    verify.add_argument("--formulas", type=Path, required=True, help="formulas/*.json")
    verify.add_argument("--database-file", type=Path, required=True)
    verify.add_argument("--template", type=Path, help="A planned campaign root: its networks")
    verify.add_argument("--hour", default="last-hour", help="last-hour, now-hour or ISO hour")
    verify.add_argument("--json", action="store_true", help="every check as JSON")
    verify.add_argument("--no-cloud", action="store_true", help="skip the droplets and archive")
    verify.add_argument("--do-token-file", type=Path, help="default: the manager's do-token")
    watch = actions.add_parser("droplets", help="Read-only droplet checks for the alerts")
    watch.add_argument("--do-token-file", type=Path, help="default: the manager's do-token")
    watch.add_argument("--cap", type=int, default=250, help="more droplets up is near the limit")
    image = actions.add_parser("image", help="Read-only worker image checks for the alerts")
    image.add_argument("--template", type=Path, help="default: the manager's daily/template")
    image.add_argument("--images", type=Path, help="default: .secrets/campaign-images.json")
    image.add_argument("--wheels", type=Path, help="default: the manager's wheels/")


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
    from .runner import RunInputs, hour_budget, hour_plan, start_hour

    now = utc_now()
    if args.template is None:
        begins, _ = start_hour(args.start, now)
        plan = plan_hour(load_schedules(args.formulas), begins.hour)
        shares = {}
    else:
        base, plan, begins, _ = hour_plan(RunInputs(args.template, args.formulas, args.start), now)
        shares = hour_budget(args.template, base, plan)
    extra = {"budget": shares} if shares else {}
    print(json.dumps({"starts_at": begins.isoformat()} | plan.to_dict() | extra, indent=2))
    return 0


def _pulse_plan(args) -> int:
    """The hour's pulse droplets as JSON; exit 3 (`refused`) when over the cap or, with
    `--root`, when its `fleet.json` is refused (the pulse hour's own check)."""
    from ..campaign.pulse_fleet import FleetError, load_config
    from ..campaign.tick import load_spec
    from .pulses import CapError, check_cap, pulse_hour
    from .runner import start_hour

    begins, _ = start_hour(args.start, utc_now())
    networks = load_spec(args.template).networks if args.template else None
    if args.root:
        try:
            fleet = tuple(sorted(load_config(args.root).networks))
        except FleetError as error:
            print(json.dumps({"starts_at": begins.isoformat(), "refused": str(error)}))
            return 3
        networks = networks or fleet
    plan = pulse_hour(load_schedules(args.formulas), begins.hour, networks=networks)
    out = {"starts_at": begins.isoformat(), "cap": args.max_droplets} | plan.to_dict()
    try:
        check_cap(plan, args.max_droplets)
    except CapError as error:
        out["refused"] = str(error)
    print(json.dumps(out, indent=2))
    return 3 if "refused" in out else 0


def _pulse_hour(args) -> int:
    """The pulse timer's job: exit 3 unless the hour is done and nothing is left up; every hour
    that is not opened its one-line alert (campaign.pulse_alerts)."""
    from ..campaign.cli import cancel_on_signals
    from ..campaign.pulse_fleet import FleetError
    from ..campaign.pulse_ondemand import HourInputs, run_hour, services
    from .pulses import CapError

    inputs = HourInputs(args.root, args.formulas, args.start, args.max_droplets)

    async def run() -> dict:
        cancel_on_signals()
        built = services(args.root)
        try:
            return await run_hour(inputs, built, utc_now())
        finally:
            await built.cloud.close()
            await built.db.close()

    try:
        outcome = asyncio.run(run())
    except (CapError, FleetError) as error:  # a refused fleet.json also opened its alert
        print(json.dumps({"refused": str(error)}))
        return 3
    print(json.dumps(outcome, indent=2))
    return 0 if outcome["state"] == "done" and not outcome["leftover"] else 3


def _pulse_switch(args) -> int:
    """One JSON line: on_demand, step_seconds and the HTTP targets' pulse intervals."""
    from .pulses import switch

    print(json.dumps(switch(args.database_file, args.state), separators=(",", ":")))
    return 0


def _resume_network(args) -> int:
    """PROXY-AUTH-PAUSE-1: one JSON line; exit 3 when the login test refused the resume."""
    from ..campaign.collector import MANAGER_ROOT
    from ..netpause import Places
    from ..orchestrator.control import read_setting
    from ..orchestrator.login_pause import resume
    from ..storage import connect

    with connect(args.database_file) as conn:
        browser = read_setting(conn, "browser.settings_file")
        places = Places(args.root or MANAGER_ROOT, Path(browser) if browser else None)
        outcome = resume(conn, places, args.network)
    print(json.dumps(outcome, separators=(",", ":")))
    return 0 if outcome["resumed"] else 3


def _pulse_backfill(args) -> int:
    """One JSON report; exit 3 when an applied backfill leaves pulse runs uncounted."""
    from .pulse_backfill import backfill

    report = backfill(
        args.database_file, spool=args.spool, mmdb=args.mmdb, apply=args.apply, batch=args.batch
    )
    print(json.dumps(report, indent=2))
    return 3 if args.apply and report["candidates_after"] else 0


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


def _verify_hour(args) -> int:
    """The hour's report, text or JSON; exit 3 unless every check is ok (or not due)."""
    from ..campaign.collector import MANAGER_ROOT
    from ..campaign.tick import load_spec
    from .runner import waiting
    from .verify import Hour, hour_of, render, report

    now = utc_now()
    networks = load_spec(args.template).networks if args.template else None
    start = hour_of(args.hour, now)
    hour = Hour(start, now, waiting(args.template or MANAGER_ROOT / "daily" / "template", start))
    found = report(args.database_file, args.formulas, hour, networks)
    if not args.no_cloud:
        from .verify_cloud import with_cloud

        token = args.do_token_file or MANAGER_ROOT / ".secrets" / "do-token"
        found = with_cloud(found, args.database_file, hour, token)
    print(json.dumps(found.to_dict(), indent=2) if args.json else render(found))
    return 0 if found.ok else 3


def _droplets(args) -> int:
    """The orphan and count checks, one tab-separated line each; exit 3 unless all are ok."""
    from ..campaign.collector import MANAGER_ROOT
    from ..campaign.do_api import DigitalOcean, file_token
    from .verify_cloud import watch

    token = args.do_token_file or MANAGER_ROOT / ".secrets" / "do-token"
    checks = watch(DigitalOcean(file_token(token)), utc_now(), args.cap)
    for check in checks:
        print(f"{check.subject}\t{'ok' if check.ok else 'failed'}\t{check.detail}")
    return 0 if all(check.ok for check in checks) else 3


def _image(args) -> int:
    """The image checks, one tab-separated line each; exit 3 unless all are ok."""
    from ..campaign.collector import MANAGER_ROOT
    from .image_check import ImageFiles, checks

    files = ImageFiles(
        args.template or MANAGER_ROOT / "daily" / "template",
        args.images or MANAGER_ROOT / ".secrets" / "campaign-images.json",
        args.wheels or MANAGER_ROOT / "wheels",
    )
    found = checks(files, utc_now())
    for check in found:
        print(f"{check.subject}\t{'ok' if check.ok else 'failed'}\t{check.detail}")
    return 0 if all(check.ok for check in found) else 3


HANDLERS = {
    "plan": _plan,
    "pulse-plan": _pulse_plan,
    "pulse-hour": _pulse_hour,
    "pulse-switch": _pulse_switch,
    "pulse-backfill": _pulse_backfill,
    "resume-network": _resume_network,
    "run": _run,
    "ingest": _ingest,
    "hour": _hour,
    "check": _check,
    "audits": _audits,
    "tape": _tape,
    "verify-hour": _verify_hour,
    "droplets": _droplets,
    "image": _image,
}


def dispatch(args) -> int:
    return HANDLERS[args.daily_action](args)
