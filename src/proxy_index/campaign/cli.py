"""`proxy-index campaign …` commands. Append-only registry: each task adds its own subcommands."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Subcommand name -> handler(args) -> exit code; each task appends its own entries.
HANDLERS: dict = {}
# Functions that add subparsers to `campaign`; each task appends its own entries.
PARSERS: list = []


def register(commands) -> None:
    parser = commands.add_parser("campaign", help="Private hourly droplet campaign (operator)")
    actions = parser.add_subparsers(dest="campaign_action", required=True)
    for add in PARSERS:
        add(actions)


def dispatch(args) -> int:
    return HANDLERS[args.campaign_action](args)


def _data_parsers(actions) -> None:
    """CAMP-1e: merge, completeness and verified collect."""
    merge = actions.add_parser("merge", help="Build each region's state.json from its rounds")
    merge.add_argument("--root", type=Path, required=True)
    merge.add_argument("--network", action="append", help="Repeatable; default: every network")
    completeness = actions.add_parser("completeness", help="Write the per-cell completeness")
    completeness.add_argument("--root", type=Path, required=True)
    collect = actions.add_parser("collect", help="Download and verify a campaign root (SHA-256)")
    collect.add_argument("--inventory", type=Path, required=True)
    collect.add_argument("--campaign-id", required=True)
    collect.add_argument("--dest", type=Path, required=True)


def _merge(args) -> int:
    from .merge import CampaignNotFinished, RoundDataMissing, load_spec, merge_networks

    try:
        merged = merge_networks(args.root, args.network or load_spec(args.root).networks)
    except (CampaignNotFinished, RoundDataMissing) as exc:
        print(f"Not merged: {exc}", file=sys.stderr)  # names the cell; holds no IP
        return 3
    for network, summaries in merged.items():
        print(
            json.dumps(
                {
                    "network": network,
                    "regions": len(summaries),
                    "unfinished_countries": sum(s["unfinished_countries"] for s in summaries),
                }
            )
        )
    return 0


def _completeness(args) -> int:
    from .completeness import build

    result = build(args.root)
    print(
        json.dumps(
            {
                "campaign_id": result["campaign_id"],
                "totals": result["totals"],
                "issues": len(result["issues"]),
            }
        )
    )
    return 0 if result["totals"]["complete"] == result["cells"] else 3


def _collect(args) -> int:
    from .collect import collect

    report = collect(args.inventory, args.campaign_id, args.dest.expanduser())
    print(
        json.dumps(
            {
                key: report[key] if isinstance(report[key], (bool, int)) else len(report[key])
                for key in ("files", "verified", "mismatches", "missing", "extra", "clean")
            }
        )
    )
    return 0 if report["clean"] else 3


PARSERS.append(_data_parsers)
HANDLERS.update(merge=_merge, completeness=_completeness, collect=_collect)


DEFAULT_SIZES = ("c-2", "s-8vcpu-16gb", "s-1vcpu-1gb")
SECRETS = Path(".secrets")


def _ops_parsers(actions) -> None:
    """CAMP-1f: read-only limits, the worker image build and the Mac watch agent."""
    from .contracts import ACCOUNTS

    limits_parser = actions.add_parser(
        "limits", help="Read-only: droplet limit, our resources, rate limit and size offers"
    )
    limits_parser.add_argument("--account", choices=sorted(ACCOUNTS), required=True)
    limits_parser.add_argument("--sizes", nargs="+", default=list(DEFAULT_SIZES))
    image = actions.add_parser("image", help="Build the worker snapshot in every region")
    image.add_argument("image_action", choices=["build"])
    image.add_argument("--account", choices=sorted(ACCOUNTS), required=True)
    image.add_argument("--wheel", type=Path, required=True)
    image.add_argument("--requirements", type=Path, required=True)
    image.add_argument(
        "--mac-key", type=Path, default=Path("~/.ssh/proxy-index-campaign").expanduser()
    )
    image.add_argument("--inventory", type=Path, default=SECRETS / "campaign-images.json")
    image.add_argument(
        "--mmdb", type=Path, help="DB-IP MMDB placed in the image for the run-time country check"
    )
    guard = actions.add_parser("guard", help="The Mac's alert-only campaign watch (launchd)")
    guard.add_argument("guard_action", choices=["install", "remove", "status"])
    guard.add_argument("--inventory", type=Path, default=SECRETS / "campaign-manager.json")
    guard.add_argument("--status-file", type=Path)
    guard.add_argument("--traffic-dir", type=Path)


def _cloud(account_key: str):
    from .contracts import ACCOUNTS
    from .do_api import DigitalOcean, keychain_token

    return DigitalOcean(keychain_token(ACCOUNTS[account_key]))


def _limits(args) -> int:
    from .contracts import PROJECT_NAME

    print(json.dumps(asyncio.run(limits(_cloud(args.account), PROJECT_NAME, args.sizes)), indent=2))
    return 0


async def limits(cloud, project_name: str, sizes: list[str]) -> dict:
    """Counts only: never lists production droplets, prints no IPs and no token."""
    from ..regions import REGION_IDS
    from .contracts import CAMPAIGN_ROLES

    async with cloud:
        account = await cloud.account()
        ours = {tag: len(await cloud.list_tag(tag)) for tag, _ in CAMPAIGN_ROLES.values()}
        in_project = len(await cloud.project_droplet_ids(await cloud.project_id(project_name)))
        offered = await cloud.sizes()
        return {
            "droplet_limit": account["droplet_limit"],
            "account_status": account["status"],
            "our_tag_counts": ours,
            "project": {"name": project_name, "droplets": in_project},
            "ratelimit_remaining": cloud.ratelimit_remaining,
            "sizes": {
                size: [r for r in REGION_IDS if r in offered.get(size, [])] for size in sizes
            },
        }


def _image(args) -> int:
    from .image import ImageJob, build, ensure_mac_key

    job = ImageJob(
        args.wheel, args.requirements, ensure_mac_key(args.mac_key), args.inventory, mmdb=args.mmdb
    )

    async def run() -> dict:
        async with _cloud(args.account) as cloud:
            return await build(cloud, job)

    print(json.dumps(asyncio.run(run()), indent=2, sort_keys=True))
    return 0


def _guard(args) -> int:
    from . import guard

    home = Path.home()
    if args.guard_action == "install":
        if not (args.status_file and args.traffic_dir):
            raise SystemExit("guard install needs --status-file and --traffic-dir")
        command = guard.watch_command(args.inventory, args.status_file, args.traffic_dir)
        print(json.dumps({"installed": str(guard.install(command, home))}))
    elif args.guard_action == "remove":
        print(json.dumps({"removed": guard.remove(home)}))
    else:
        print(json.dumps(guard.status(home)))
    return 0


PARSERS.append(_ops_parsers)
HANDLERS.update(limits=_limits, image=_image, guard=_guard)


def _scheduler_parsers(actions) -> None:
    """CAMP-1d: tick, watchdog, network pause, the collector and the Mac watch; PULSE-1b: the
    pulse fleet."""
    from . import collector, pulse_fleet, tick, watch

    for module in (tick, collector, watch, pulse_fleet):
        module.register(actions, HANDLERS)


PARSERS.append(_scheduler_parsers)


def _plan_parsers(actions) -> None:
    """CAMP-1g: the campaign plan and the manager's deploy, enable, allow-me and teardown."""
    from .contracts import ACCOUNTS

    _plan_parser(actions, ACCOUNTS)
    _manager_parsers(actions, ACCOUNTS)


def _plan_parser(actions, accounts) -> None:
    plan = actions.add_parser("plan", help="Validate inputs; write the manifests and the spec")
    plan.add_argument("--account", choices=sorted(accounts), required=True)
    plan.add_argument("--start", required=True, help="ISO whole hour with offset, or now-hour")
    plan.add_argument("--networks", required=True, help="all16, or comma-separated names")
    plan.add_argument("--regions", nargs="+")
    plan.add_argument("--concurrency", type=int, default=2000)
    plan.add_argument("--size", default="c-2", help="worker droplet size slug (c-…, c2-…, s-…)")
    plan.add_argument("--image", required=True, help="snapshot name from `image build`")
    plan.add_argument("--grace-hours", type=int, default=48)
    plan.add_argument(
        "--slot-minutes",
        type=int,
        default=60,
        help="minutes between slots, a divisor of 60 (15: four slots an hour)",
    )
    plan.add_argument("--slots", type=int, default=24, help="number of slots (1-24)")
    plan.add_argument("--ssh-key-name", help="default: the deployed worker key")
    plan.add_argument("--synthetic", action="store_true")
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--credentials-file", type=Path, default=SECRETS / "credentials.toml")
    plan.add_argument("--configs", type=Path, default=Path("configs"))
    plan.add_argument("--inventory", type=Path, default=SECRETS / "campaign-manager.json")
    plan.add_argument("--images", type=Path, default=SECRETS / "campaign-images.json")


def _manager_parsers(actions, accounts) -> None:
    from . import browser_hosts, manager_orch

    manager = actions.add_parser("manager", help="Deploy and operate the campaign manager")
    steps = manager.add_subparsers(dest="manager_action", required=True)
    deploy = _deploy_parser(steps, accounts)
    upload = steps.add_parser("upload-spec", help="Copy a planned campaign to the manager")
    upload.add_argument("root", type=Path)
    enable = steps.add_parser("enable", help="Start a campaign's timers (owner's go only)")
    enable.add_argument("campaign_id")
    enable.add_argument("--owner-go", type=Path, help="the owner's recorded answer file")
    stop = steps.add_parser("disable", help="Stop a campaign's timers and collector")
    stop.add_argument("campaign_id")
    allow = steps.add_parser("allow-me", help="Admit SSH from this Mac's current IPv4")
    teardown = _teardown_parser(steps)
    for step in (deploy, allow):
        step.add_argument("--mac-ip", help="default: as the echo target sees this Mac")
    for step in (deploy, upload, enable, stop, allow, teardown):
        step.add_argument("--inventory", type=Path, default=SECRETS / "campaign-manager.json")
    manager_orch.add_parsers(steps)  # MANAGER-ORCH-1
    browser_hosts.add_parser(steps)  # BROWSER-MTLS-1


def _deploy_parser(steps, accounts):
    from . import host_deploy

    deploy = steps.add_parser("deploy", help="Create or update the hardened manager")
    deploy.add_argument("--account", choices=sorted(accounts), required=True)
    deploy.add_argument("--wheel", type=Path, required=True)
    deploy.add_argument("--requirements", type=Path, required=True)
    deploy.add_argument(
        "--size",
        help="a new manager's droplet size slug, offered in its region "
        "(default: the recorded manager's, else s-8vcpu-16gb)",
    )
    deploy.add_argument(
        "--region",
        help="a new manager's DigitalOcean region, e.g. tor1 (default: the inventory's "
        "manager_region, else lon1); a recorded manager is never moved: tear it down with "
        "--keep-worker-assets first (MANAGER-REGION-1)",
    )
    deploy.add_argument(
        "--environment",
        choices=["production", "staging"],
        default="production",
        help="staging: a manager beside production with its own pi-camp-stg- tags, firewalls "
        "and keys; give it its own --inventory and --mac-key (CAMP-STAGING-1)",
    )
    deploy.add_argument("--networks", default="all16")
    deploy.add_argument("--credentials-file", type=Path, default=SECRETS / "credentials.toml")
    deploy.add_argument(
        "--spaces-file",
        type=Path,
        help="[spaces] settings of the raw-data archive (secret from the keychain); written "
        "to the manager's .secrets/spaces.toml",
    )
    deploy.add_argument(
        "--mac-key", type=Path, default=Path("~/.ssh/proxy-index-campaign").expanduser()
    )
    host_deploy.add_arguments(deploy)  # MANAGER-HOST-1
    return deploy


def _teardown_parser(steps):
    teardown = steps.add_parser("teardown", help="Remove the manager and its resources")
    teardown.add_argument(
        "--after-collect",
        type=Path,
        action="append",
        default=[],
        help="a clean collect report; one for every campaign the "
        "inventory records (none when it records no campaign)",
    )
    teardown.add_argument(
        "--keep-worker-assets",
        action="store_true",
        help="remove only the manager and its firewall; keep the worker "
        "snapshots, firewall and keys for the next manager (they can only "
        "be removed by a full teardown after the next deploy)",
    )
    return teardown


def _plan(args) -> int:
    from ..regions import REGION_IDS
    from .planspec import PlanError, PlanInputs, parse_networks, plan

    try:
        inputs = PlanInputs(
            args.account,
            args.start,
            parse_networks(args.networks),
            args.image,
            args.output,
            args.credentials_file,
            args.inventory,
            args.images,
            tuple(args.regions or REGION_IDS),
            args.configs,
            args.ssh_key_name,
            args.concurrency,
            args.grace_hours,
            args.synthetic,
            args.size,
            args.slot_minutes,
            args.slots,
        )

        async def run() -> dict:
            async with _cloud(args.account) as cloud:
                return await plan(cloud, inputs)

        print(json.dumps(asyncio.run(run()), indent=2))
    except PlanError as exc:
        print(f"Not planned: {exc}", file=sys.stderr)
        return 3
    return 0


async def _manager_step(args) -> dict:
    from . import browser_hosts, deploy, host_deploy, host_layout, manager_orch

    if args.manager_action in manager_orch.ACTIONS:  # MANAGER-ORCH-1
        return await manager_orch.run(args, _cloud)
    if args.manager_action == "browser-cert":  # BROWSER-MTLS-1 (SSH only)
        return await browser_hosts.issue(args.inventory, args.host, args.out)
    if args.manager_action == "upload-spec":
        return await deploy.upload_spec(args.inventory, args.root)
    if args.manager_action == "enable":
        return await deploy.enable(args.inventory, args.campaign_id, args.owner_go)
    if args.manager_action == "disable":
        return await deploy.disable(args.inventory, args.campaign_id)
    if args.manager_action == "allow-me" and host_layout.of(deploy.load_inventory(args.inventory)):
        return host_deploy.allow_me()  # MANAGER-HOST-1: the provider's panel, no cloud firewall
    # teardown and allow-me act on the account the inventory records (deploy checks it too)
    account = (
        args.account
        if args.manager_action == "deploy"
        else deploy.load_inventory(args.inventory).get("account")
    )
    if account is None:
        raise deploy.DeployError("no manager is recorded")
    async with _cloud(account) as cloud:
        return await _manager_on_cloud(args, cloud)


async def _manager_on_cloud(args, cloud) -> dict:
    """`manager teardown`, `allow-me` and `deploy` on the account's cloud."""
    from . import deploy, teardown

    if args.manager_action == "teardown":
        return await teardown.teardown(
            cloud,
            args.inventory,
            args.after_collect,
            keep_worker_assets=args.keep_worker_assets,
        )
    if args.manager_action == "deploy" and args.host:  # MANAGER-HOST-1: no Mac address needed
        return await _deploy(args, cloud, "")
    address = deploy.checked_ipv4(args.mac_ip) if args.mac_ip else deploy.mac_ipv4()
    if args.manager_action == "allow-me":
        return await deploy.allow_me(cloud, args.inventory, address)
    return await _deploy(args, cloud, address)


async def _deploy(args, cloud, address: str) -> dict:
    from ..archive.spaces import manager_settings, push_settings
    from . import deploy, host_deploy
    from .contracts import ACCOUNTS
    from .do_api import keychain_token
    from .image import ensure_mac_key
    from .planspec import parse_networks

    job = deploy.DeployJob(
        args.account,
        args.wheel,
        args.requirements,
        ensure_mac_key(args.mac_key),
        args.inventory,
        deploy.credential_files(args.credentials_file, parse_networks(args.networks)),
        address,
        keychain_token(ACCOUNTS[args.account]),  # read only when streamed
        args.size,
        args.environment,
        args.region,
        args.database_file.read_bytes() if args.database_file else None,
    )
    spaces = manager_settings(args.spaces_file) if args.spaces_file else None
    if args.host:
        target = host_deploy.HostTarget(
            args.host, args.ssh_user, args.ref, args.setup_script, args.ssh_port
        )
        result = await host_deploy.deploy_host(cloud, job, target)
    else:
        result = await deploy.deploy(cloud, job)
    if spaces:  # the raw-data archive's settings (ARCHIVE-1), after the manager is up
        await push_settings(args.inventory, spaces)
    return result


def _manager(args) -> int:
    from .deploy import DeployError
    from .ops_api import OpsError

    try:
        result = asyncio.run(_manager_step(args))
    except (DeployError, OpsError) as exc:
        print(f"Manager {args.manager_action} refused or failed: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("clean", True) else 3


PARSERS.append(_plan_parsers)
HANDLERS.update(plan=_plan, manager=_manager)


def _probe_parsers(actions) -> None:
    """CONC-1: each network's max safe concurrency, from one worker (run on the manager)."""
    from . import probe

    found = actions.add_parser("probe", help="Measure each network's max concurrency (manager)")
    what = found.add_mutually_exclusive_group(required=True)
    what.add_argument("--root", type=Path, help="a planned campaign uploaded to this manager")
    what.add_argument(
        "--remove",
        type=Path,
        metavar="PROBE_FOLDER",
        help="remove the worker a stopped probe left (guarded)",
    )
    found.add_argument("--networks", help="comma-separated; default: the campaign's networks")
    found.add_argument("--requests", type=int, default=probe.REQUESTS)
    found.add_argument("--start", type=int, default=probe.START)
    found.add_argument("--step", type=int, default=probe.STEP)
    found.add_argument("--floor", type=int, default=probe.FLOOR)
    found.add_argument("--country", default=probe.COUNTRY)
    found.add_argument("--region", default=probe.REGION)
    found.add_argument("--size", default=probe.SIZE)
    found.add_argument("--output", type=Path, help="default: <manager>/probes")


def cancel_on_signals() -> None:
    """The first SIGTERM, SIGHUP or SIGINT cancels the running probe, whose `finally` still
    removes its worker; later ones are ignored so that removal can finish."""
    import signal

    loop, task = asyncio.get_running_loop(), asyncio.current_task()
    numbers = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)

    def stop() -> None:
        for number in numbers:
            loop.add_signal_handler(number, lambda: None)
        task.cancel()

    for number in numbers:
        loop.add_signal_handler(number, stop)


def _probe(args) -> int:
    from .collector import MANAGER_ROOT
    from .contracts import runtime_dir
    from .do_api import DigitalOcean, file_token
    from .fanout import OpenSSHRemote
    from .planspec import PlanError, parse_networks
    from .probe import ProbeError, ProbeInputs, probe
    from .recover import recover

    secrets = MANAGER_ROOT / ".secrets"
    output = args.output or MANAGER_ROOT / "probes"
    try:
        inputs = ProbeInputs(
            args.root,
            output,
            parse_networks(args.networks) if args.networks else (),
            args.requests,
            args.start,
            args.step,
            args.floor,
            args.country,
            args.region,
            args.size,
        )
    except PlanError as exc:
        print(f"Not probed: {exc}", file=sys.stderr)
        return 3

    async def run() -> dict:
        cancel_on_signals()
        async with DigitalOcean(file_token(secrets / "do-token")) as cloud:
            if args.remove:
                return await recover(cloud, args.remove)
            return await probe(
                cloud,
                lambda folder: OpenSSHRemote(secrets / "worker-key", runtime_dir(folder), 0),
                inputs,
            )

    try:
        result = asyncio.run(run())
    except ProbeError as exc:
        print(f"Not probed: {exc}", file=sys.stderr)
        return 3
    except asyncio.CancelledError:  # stopped outside a probe run, e.g. during --remove
        print("Interrupted: run `campaign probe --remove` again", file=sys.stderr)
        return 3
    print(json.dumps(result, indent=2))
    done = ("destroyed", "absent") if args.remove else ("destroyed",)
    return 0 if result["worker_status"] in done and "error" not in result else 3


PARSERS.append(_probe_parsers)
HANDLERS.update(probe=_probe)


def parse_pairs(value: str | None) -> dict[str, str]:
    """`net=value,net=value` (for example `soax=c-2,proxy-cheap=c-2`)."""
    pairs = [part.strip().split("=", 1) for part in (value or "").split(",") if part.strip()]
    if any(len(pair) != 2 or not all(pair) for pair in pairs):
        raise ValueError("expected network=value pairs")
    return {network: setting for network, setting in pairs}


def _shot_parsers(actions) -> None:
    """SYNC-1: one fixed step per network and region, all started at one time (manager)."""
    from . import shot

    found = actions.add_parser("shot", help="One synchronized step per network and region")
    what = found.add_mutually_exclusive_group(required=True)
    what.add_argument("--root", type=Path, help="a planned campaign uploaded to this manager")
    what.add_argument(
        "--remove",
        type=Path,
        metavar="SHOT_FOLDER",
        help="remove the workers a stopped shot left (guarded)",
    )
    found.add_argument("--networks", help="comma-separated; default: the campaign's networks")
    found.add_argument("--regions", help="comma-separated; default: the campaign's regions")
    found.add_argument("--requests", type=int, default=shot.REQUESTS)
    found.add_argument("--size", default=shot.SIZE)
    found.add_argument("--size-by-network", help="e.g. soax=c-2,proxy-cheap=c-2")
    found.add_argument("--country-by-network", help="e.g. soax=US (default: per region)")
    found.add_argument(
        "--lead-minutes",
        type=float,
        default=shot.LEAD_SECONDS / 60,
        help="minutes from the shot's start to the common start time",
    )
    found.add_argument("--output", type=Path, help="default: <manager>/shots")


def _shot(args) -> int:
    from .collector import MANAGER_ROOT
    from .planspec import PlanError
    from .probe import ProbeError

    try:
        inputs = _shot_inputs(args, MANAGER_ROOT)
    except (PlanError, ValueError) as exc:
        print(f"Not fired: {exc}", file=sys.stderr)
        return 3
    try:
        result = asyncio.run(_shot_run(args, inputs, MANAGER_ROOT / ".secrets"))
    except ProbeError as exc:
        print(f"Not fired: {exc}", file=sys.stderr)
        return 3
    except asyncio.CancelledError:  # stopped outside a shot run, e.g. during --remove
        print("Interrupted: run `campaign shot --remove` again", file=sys.stderr)
        return 3
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in ("workers", "cells")}
            | {"outcomes": _outcomes(result)},
            indent=2,
        )
    )
    return 0 if result["workers_left"] == 0 and "error" not in result else 3


def _shot_inputs(args, manager_root: Path):
    """The shot's inputs from its command line (PlanError or ValueError when invalid)."""
    from .planspec import parse_networks
    from .shot import ShotInputs

    regions = tuple(r.strip() for r in (args.regions or "").split(",") if r.strip())
    if len(set(regions)) != len(regions):
        raise ValueError("--regions must be distinct")
    return ShotInputs(
        args.root,
        args.output or manager_root / "shots",
        parse_networks(args.networks) if args.networks else (),
        regions,
        args.requests,
        args.size,
        parse_pairs(args.size_by_network),
        {n: c.upper() for n, c in parse_pairs(args.country_by_network).items()},
        args.lead_minutes * 60,
    )


async def _shot_run(args, inputs, secrets: Path) -> dict:
    from .contracts import runtime_dir
    from .do_api import DigitalOcean, file_token
    from .fanout import OpenSSHRemote
    from .shot import recover, shot

    cancel_on_signals()
    async with DigitalOcean(file_token(secrets / "do-token")) as cloud:
        if args.remove:
            return await recover(cloud, args.remove)
        return await shot(
            cloud,
            lambda folder: OpenSSHRemote(secrets / "worker-key", runtime_dir(folder), 0),
            inputs,
        )


def _outcomes(result: dict) -> dict[str, int]:
    """How many cells ended in each outcome (the full table is in the shot folder)."""
    counts: dict[str, int] = {}
    for cell in result.get("cells", {}).values():
        counts[cell["outcome"]] = counts.get(cell["outcome"], 0) + 1
    return counts


PARSERS.append(_shot_parsers)
HANDLERS.update(shot=_shot)
