"""`proxy-index campaign …` commands. Append-only registry: each task adds its own subcommands."""

from __future__ import annotations

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
