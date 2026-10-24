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
        print(json.dumps({"network": network, "regions": len(summaries),
                          "unfinished_countries": sum(s["unfinished_countries"]
                                                      for s in summaries)}))
    return 0


def _completeness(args) -> int:
    from .completeness import build

    result = build(args.root)
    print(json.dumps({"campaign_id": result["campaign_id"], "totals": result["totals"],
                      "issues": len(result["issues"])}))
    return 0 if result["totals"]["complete"] == result["cells"] else 3


def _collect(args) -> int:
    from .collect import collect

    report = collect(args.inventory, args.campaign_id, args.dest.expanduser())
    print(json.dumps({key: report[key] if isinstance(report[key], (bool, int)) else
                      len(report[key]) for key in ("files", "verified", "mismatches", "missing",
                                                   "extra", "clean")}))
    return 0 if report["clean"] else 3


PARSERS.append(_data_parsers)
HANDLERS.update(merge=_merge, completeness=_completeness, collect=_collect)


DEFAULT_SIZES = ("c-2", "s-8vcpu-16gb", "s-1vcpu-1gb")
SECRETS = Path(".secrets")


def _ops_parsers(actions) -> None:
    """CAMP-1f: read-only limits, the worker image build and the Mac watch agent."""
    from .contracts import ACCOUNTS

    limits_parser = actions.add_parser(
        "limits", help="Read-only: droplet limit, our resources, rate limit and size offers")
    limits_parser.add_argument("--account", choices=sorted(ACCOUNTS), required=True)
    limits_parser.add_argument("--sizes", nargs="+", default=list(DEFAULT_SIZES))
    image = actions.add_parser("image", help="Build the worker snapshot in every region")
    image.add_argument("image_action", choices=["build"])
    image.add_argument("--account", choices=sorted(ACCOUNTS), required=True)
    image.add_argument("--wheel", type=Path, required=True)
    image.add_argument("--requirements", type=Path, required=True)
    image.add_argument("--mac-key", type=Path,
                       default=Path("~/.ssh/proxy-index-campaign").expanduser())
    image.add_argument("--inventory", type=Path, default=SECRETS / "campaign-images.json")
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

    print(json.dumps(asyncio.run(limits(_cloud(args.account), PROJECT_NAME, args.sizes)),
                     indent=2))
    return 0


async def limits(cloud, project_name: str, sizes: list[str]) -> dict:
    """Counts only: never lists production droplets, prints no IPs and no token."""
    from ..regions import REGION_IDS
    from .contracts import ROLES

    async with cloud:
        account = await cloud.account()
        ours = {tag: len(await cloud.list_tag(tag)) for tag, _ in ROLES.values()}
        in_project = len(await cloud.project_droplet_ids(await cloud.project_id(project_name)))
        offered = await cloud.sizes()
        return {
            "droplet_limit": account["droplet_limit"], "account_status": account["status"],
            "our_tag_counts": ours, "project": {"name": project_name, "droplets": in_project},
            "ratelimit_remaining": cloud.ratelimit_remaining,
            "sizes": {size: [r for r in REGION_IDS if r in offered.get(size, [])]
                      for size in sizes},
        }


def _image(args) -> int:
    from .image import ImageJob, build, ensure_mac_key

    job = ImageJob(args.wheel, args.requirements, ensure_mac_key(args.mac_key), args.inventory)

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
    """CAMP-1d: tick, watchdog, network pause, the collector and the Mac watch."""
    from . import collector, tick, watch

    for module in (tick, collector, watch):
        module.register(actions, HANDLERS)


PARSERS.append(_scheduler_parsers)


def _plan_parsers(actions) -> None:
    """CAMP-1g: the campaign plan and the manager's deploy, enable, allow-me and teardown."""
    from .contracts import ACCOUNTS

    plan = actions.add_parser("plan", help="Validate inputs; write the manifests and the spec")
    plan.add_argument("--account", choices=sorted(ACCOUNTS), required=True)
    plan.add_argument("--start", required=True, help="ISO whole hour with offset, or now-hour")
    plan.add_argument("--networks", required=True, help="all16, or comma-separated names")
    plan.add_argument("--regions", nargs="+")
    plan.add_argument("--concurrency", type=int, default=2000)
    plan.add_argument("--size", default="c-2", help="worker droplet size slug (c-…, c2-…, s-…)")
    plan.add_argument("--image", required=True, help="snapshot name from `image build`")
    plan.add_argument("--grace-hours", type=int, default=48)
    plan.add_argument("--slot-minutes", type=int, default=60,
                      help="minutes between slots, a divisor of 60 (15: four slots an hour)")
    plan.add_argument("--slots", type=int, default=24, help="number of slots (1-24)")
    plan.add_argument("--ssh-key-name", help="default: the deployed worker key")
    plan.add_argument("--synthetic", action="store_true")
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--credentials-file", type=Path, default=SECRETS / "credentials.toml")
    plan.add_argument("--configs", type=Path, default=Path("configs"))
    plan.add_argument("--inventory", type=Path, default=SECRETS / "campaign-manager.json")
    plan.add_argument("--images", type=Path, default=SECRETS / "campaign-images.json")
    manager = actions.add_parser("manager", help="Deploy and operate the campaign manager")
    steps = manager.add_subparsers(dest="manager_action", required=True)
    deploy = steps.add_parser("deploy", help="Create or update the hardened manager")
    deploy.add_argument("--account", choices=sorted(ACCOUNTS), required=True)
    deploy.add_argument("--wheel", type=Path, required=True)
    deploy.add_argument("--requirements", type=Path, required=True)
    deploy.add_argument("--size", help="a new manager's droplet size slug, offered in lon1 "
                        "(default: the recorded manager's, else s-8vcpu-16gb)")
    deploy.add_argument("--networks", default="all16")
    deploy.add_argument("--credentials-file", type=Path, default=SECRETS / "credentials.toml")
    deploy.add_argument("--mac-key", type=Path,
                        default=Path("~/.ssh/proxy-index-campaign").expanduser())
    upload = steps.add_parser("upload-spec", help="Copy a planned campaign to the manager")
    upload.add_argument("root", type=Path)
    enable = steps.add_parser("enable", help="Start a campaign's timers (owner's go only)")
    enable.add_argument("campaign_id")
    enable.add_argument("--owner-go", type=Path, help="the owner's recorded answer file")
    stop = steps.add_parser("disable", help="Stop a campaign's timers and collector")
    stop.add_argument("campaign_id")
    allow = steps.add_parser("allow-me", help="Admit SSH from this Mac's current IPv4")
    teardown = steps.add_parser("teardown", help="Remove the manager and its resources")
    teardown.add_argument("--after-collect", type=Path, action="append", required=True)
    teardown.add_argument("--keep-worker-assets", action="store_true",
                          help="remove only the manager and its firewall; keep the worker "
                          "snapshots, firewall and keys for the next manager (they can only "
                          "be removed by a full teardown after the next deploy)")
    for step in (deploy, allow):
        step.add_argument("--mac-ip", help="default: as the echo target sees this Mac")
    for step in (deploy, upload, enable, stop, allow, teardown):
        step.add_argument("--inventory", type=Path, default=SECRETS / "campaign-manager.json")


def _plan(args) -> int:
    from ..regions import REGION_IDS
    from .planspec import PlanError, PlanInputs, parse_networks, plan

    try:
        inputs = PlanInputs(
            args.account, args.start, parse_networks(args.networks), args.image, args.output,
            args.credentials_file, args.inventory, args.images,
            tuple(args.regions or REGION_IDS), args.configs, args.ssh_key_name,
            args.concurrency, args.grace_hours, args.synthetic, args.size,
            args.slot_minutes, args.slots)

        async def run() -> dict:
            async with _cloud(args.account) as cloud:
                return await plan(cloud, inputs)

        print(json.dumps(asyncio.run(run()), indent=2))
    except PlanError as exc:
        print(f"Not planned: {exc}", file=sys.stderr)
        return 3
    return 0


async def _manager_step(args) -> dict:
    from . import deploy

    if args.manager_action == "upload-spec":
        return await deploy.upload_spec(args.inventory, args.root)
    if args.manager_action == "enable":
        return await deploy.enable(args.inventory, args.campaign_id, args.owner_go)
    if args.manager_action == "disable":
        return await deploy.disable(args.inventory, args.campaign_id)
    # teardown and allow-me act on the account the inventory records (deploy checks it too)
    account = args.account if args.manager_action == "deploy" else deploy.load_inventory(
        args.inventory).get("account")
    if account is None:
        raise deploy.DeployError("no manager is recorded")
    async with _cloud(account) as cloud:
        if args.manager_action == "teardown":
            return await deploy.teardown(cloud, args.inventory, args.after_collect,
                                         keep_worker_assets=args.keep_worker_assets)
        address = deploy.checked_ipv4(args.mac_ip) if args.mac_ip else deploy.mac_ipv4()
        if args.manager_action == "allow-me":
            return await deploy.allow_me(cloud, args.inventory, address)
        from .contracts import ACCOUNTS
        from .do_api import keychain_token
        from .image import ensure_mac_key
        from .planspec import parse_networks

        job = deploy.DeployJob(
            args.account, args.wheel, args.requirements, ensure_mac_key(args.mac_key),
            args.inventory, deploy.credential_files(args.credentials_file,
                                                    parse_networks(args.networks)),
            address, keychain_token(ACCOUNTS[args.account]),  # read only when streamed
            args.size)
        return await deploy.deploy(cloud, job)


def _manager(args) -> int:
    from .deploy import DeployError

    try:
        result = asyncio.run(_manager_step(args))
    except DeployError as exc:
        print(f"Manager {args.manager_action} refused or failed: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("clean", True) else 3


PARSERS.append(_plan_parsers)
HANDLERS.update(plan=_plan, manager=_manager)
