"""`proxy-index campaign …` commands. Append-only registry: each task adds its own subcommands."""

from __future__ import annotations

import asyncio
import json

from ..regions import REGION_IDS
from .contracts import ACCOUNTS, BUILDER_TAG, MANAGER_TAG, WORKER_TAG
from .do_api import DigitalOcean, keychain_token

DEFAULT_SIZES = ("c-2", "s-8vcpu-16gb", "s-1vcpu-1gb")


def register(commands) -> None:
    parser = commands.add_parser("campaign", help="Private hourly droplet campaign (operator)")
    actions = parser.add_subparsers(dest="campaign_action", required=True)
    limits_parser = actions.add_parser(
        "limits", help="Read-only: droplet limit, our resources, rate limit and size offers"
    )
    limits_parser.add_argument("--account", choices=sorted(ACCOUNTS), required=True)
    limits_parser.add_argument("--sizes", nargs="+", default=list(DEFAULT_SIZES))


def dispatch(args) -> int:
    return {"limits": _limits}[args.campaign_action](args)


def _limits(args) -> int:
    account = ACCOUNTS[args.account]
    cloud = DigitalOcean(keychain_token(account))
    print(json.dumps(asyncio.run(limits(cloud, account.project_name, args.sizes)), indent=2))
    return 0


async def limits(cloud: DigitalOcean, project_name: str, sizes: list[str]) -> dict:
    """Counts only: never lists production droplets, prints no IPs and no token."""
    async with cloud:
        account = await cloud.account()
        ours = {tag: len(await cloud.list_tag(tag)) for tag in (WORKER_TAG, MANAGER_TAG,
                                                                BUILDER_TAG)}
        project = await cloud.project_id(project_name)
        in_project = len(await cloud.project_droplet_ids(project))
        offered = await cloud.sizes()
        return {
            "droplet_limit": account["droplet_limit"],
            "account_status": account["status"],
            "our_tag_counts": ours,
            "project": {"name": project_name, "droplets": in_project},
            "ratelimit_remaining": cloud.ratelimit_remaining,
            "sizes": {size: [r for r in REGION_IDS if r in offered.get(size, [])]
                      for size in sizes},
        }
