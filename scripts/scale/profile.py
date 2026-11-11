#!/usr/bin/env python3
"""SIM-PROFILE-1: a statistics and size profile of the real 24-hour data, without IPs.

Usage:
  profile.py build --enrich <LC-ENRICH-1 out dir> --browser <BP-VERIFY-3 hours dir> \
                   [--browser <BP-VERIFY-4 hours dir>] --out <profile.json> [--formulas formulas]
  profile.py reference --database <toml> --runs <marker:<summary key> | batches:<id>-<id>> \
                       --out <profile.json> [--basis real|modelled]

`build` writes the profile (mode 600) from the hour files (profile_hours.py) and the browser
hour folders (profile_browser.py); `reference` adds the database's sizes per run kind
(profile_reference.py). Aggregates and DB-IP labels only, never an IP, host name or path;
SIMULATE.md documents every field. The real profile is never committed or posted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import profile_browser
import profile_overlap
import profile_reference
import psycopg
from profile_hours import Network, ProfileError

PROFILE_VERSION = 1
FORMULAS = Path(__file__).resolve().parents[2] / "formulas"


def write(path: Path, profile: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write((json.dumps(profile, indent=1, sort_keys=True) + "\n").encode())


def networks(formulas: Path) -> list[str]:
    found = sorted(path.stem for path in formulas.glob("*.json"))
    if not found:
        raise ProfileError("no formula (formulas/*.json)")
    return found


def enrich_part(enrich: Path, wanted: list[str]) -> tuple[dict, dict, list[dict], dict]:
    """(network entries, real_24h totals, gaps, overlap) from the LC-ENRICH-1 folders, one
    network at a time (its hourly hashes are freed before the next; its distinct ones are kept
    for the overlap)."""
    folders = {path.parent.name: path.parent for path in enrich.glob("*/manifest.json")}
    if not folders:  # a mistyped or empty --enrich: not a profile where every network is a gap
        raise ProfileError(f"{enrich.name}: no network folder with a manifest.json")
    entries, totals, gaps, days = {}, {}, [], {}
    for name in wanted:
        if name not in folders:
            gaps.append({"source": "enrich", "network": name, "what": "no network folder"})
            continue
        network = Network(folders[name])
        network.read()
        entries[name], totals[name] = network.summary()
        days[name] = network.days
        for hour in entries[name]["hours"]:
            if hour["status"] != "present":
                what = f"no IPs ({hour['status']})"
                gaps.append(
                    {"source": "enrich", "network": name, "hour": hour["hour"], "what": what}
                )
    for name in sorted(set(folders) - set(wanted)):
        gaps.append({"source": "enrich", "network": name, "what": "no formula: not profiled"})
    return entries, totals, gaps, profile_overlap.overlap(days)


def build(enrich: Path, browser: list[Path], out: Path, formulas: Path = FORMULAS) -> dict:
    wanted = networks(formulas)
    entries, totals, gaps, overlap = enrich_part(enrich, wanted)
    targets, browser_totals, browser_gaps = profile_browser.profile(browser, wanted)
    for name in wanted:
        entries.setdefault(name, {})["browser"] = targets.get(name, {})
        totals.setdefault(name, {}).update(browser_totals.get(name, {}))
    profile = {
        "profile_version": PROFILE_VERSION,
        "networks": entries,
        "real_24h": totals,
        "overlap": overlap,
        "gaps": gaps + browser_gaps,
    }
    write(out, profile)
    return profile


def reference(database: Path, runs: str, out: Path, basis: str = "real") -> dict:
    profile = json.loads(out.read_bytes()) if out.exists() else {"profile_version": 1}
    if profile.get("profile_version") != PROFILE_VERSION:
        raise ProfileError(f"profile_version {profile.get('profile_version')!r} is not 1")
    entries = profile_reference.read(database, runs)
    if not entries:
        raise ProfileError(f"--runs {runs!r}: no daily or browser run")
    for entry in entries.values():
        entry["basis"] = basis  # modelled: runs ingested from a modelled bundle, not real ones
    profile.setdefault("reference", {}).update(entries)
    write(out, profile)
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    built = commands.add_parser("build")
    built.add_argument("--enrich", type=Path, required=True)
    built.add_argument("--browser", type=Path, action="append", required=True)
    built.add_argument("--out", type=Path, required=True)
    built.add_argument("--formulas", type=Path, default=FORMULAS)
    referenced = commands.add_parser("reference")
    referenced.add_argument("--database", type=Path, required=True)
    referenced.add_argument("--runs", required=True)
    referenced.add_argument("--out", type=Path, required=True)
    referenced.add_argument("--basis", choices=("real", "modelled"), default="real")
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            profile = build(args.enrich, args.browser, args.out, args.formulas)
            done = f"{len(profile['networks'])} networks, {len(profile['gaps'])} gaps"
        else:
            entries = reference(args.database, args.runs, args.out, args.basis)
            done = ", ".join(f"{kind}: {entry['runs']} runs" for kind, entry in entries.items())
    except (ProfileError, OSError, ValueError, psycopg.Error) as error:
        print(f"profile: {error}", file=sys.stderr)
        return 1
    print(f"profile: {args.command}: {done}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
