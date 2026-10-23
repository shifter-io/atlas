"""`proxy-index campaign …` commands. Append-only registry: each task adds its own subcommands."""

from __future__ import annotations

# Subcommand name -> handler(args) -> exit code; each task appends its own entries.
HANDLERS: dict = {}


def register(commands) -> None:
    parser = commands.add_parser("campaign", help="Private hourly droplet campaign (operator)")
    parser.add_subparsers(dest="campaign_action", required=True)


def dispatch(args) -> int:
    return HANDLERS[args.campaign_action](args)
