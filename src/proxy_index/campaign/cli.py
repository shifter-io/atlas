"""`proxy-index campaign …` commands. Append-only registry: each task adds its own subcommands."""

from __future__ import annotations

import asyncio
import json
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
