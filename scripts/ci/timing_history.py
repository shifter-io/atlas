#!/usr/bin/env python3
"""CI time across runs (CI-TIME-1), from the timing artifacts the workflows keep for 90 days.

Usage: timing_history.py [--workflow ci|api] [--runs 30] [--branch BRANCH] [--event EVENT]
                         [--repo OWNER/REPO]
Lists the workflow's latest completed runs with `gh run list`, downloads each run's timing
artifact (`ci-timing` from ci.yml's tier1, `api-timing` from api.yml) with `gh run download`
and prints one line per run: date, commit, test step seconds, suite seconds (pytest, or the
slowest Go package), test count and the slowest test. Then the tests that appear most often
in the runs' 20 slowest. A run without the artifact (a docs-only change, a run from before
CI-TIME-1) is skipped and counted. Both workflows run on pull requests and in the merge queue,
never on pushes to main: `--event merge_group` shows what landed. Each run costs one `gh` call.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ARTIFACTS = {"ci": "ci-timing", "api": "api-timing"}
MOST_OFTEN = 10


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def list_runs(args):
    command = [
        "run",
        "list",
        "--workflow",
        f"{args.workflow}.yml",
        "--status",
        "completed",
        "--limit",
        str(args.runs),
        "--json",
        "databaseId,createdAt,headSha",
    ]
    for flag, value in (("--branch", args.branch), ("--event", args.event), ("-R", args.repo)):
        if value:
            command += [flag, value]
    listed = gh(*command)
    if listed.returncode != 0:
        raise SystemExit(f"gh run list failed: {listed.stderr.strip()}")
    return json.loads(listed.stdout or "[]")


def download(run, args, folder):
    """The run's timing record, or None when the run has no timing artifact."""
    target = Path(folder) / str(run["databaseId"])
    command = [
        "run",
        "download",
        str(run["databaseId"]),
        "--name",
        ARTIFACTS[args.workflow],
        "--dir",
        str(target),
    ]
    if args.repo:
        command += ["-R", args.repo]
    if gh(*command).returncode != 0 or not (target / "timing.json").is_file():
        return None
    data = json.loads((target / "timing.json").read_text())
    data.setdefault("date", run.get("createdAt", ""))
    data["sha"] = data.get("sha") or run.get("headSha", "")
    return data


def line(data):
    slowest = data["slowest"][0] if data.get("slowest") else {"test": "-", "seconds": 0}
    return (
        f"{data['date'][:16]:16}  {data['sha'][:10]:10}  {data['step_seconds']:>6}"
        f"  {data['suite_seconds']:>7}  {data['test_count']:>6}  "
        f"{slowest['seconds']:>7.1f}  {slowest['test']}"
    )


def report(records, skipped):
    print(
        f"{'date':16}  {'commit':10}  {'step s':>6}  {'suite s':>7}  {'tests':>6}  "
        f"{'slowest':>7}  test"
    )
    for data in records:
        print(line(data))
    print(f"{len(records)} runs with timing, {skipped} without")
    seen = Counter(t["test"] for data in records for t in data.get("slowest", []))
    worst = {}
    for data in records:
        for test in data.get("slowest", []):
            worst[test["test"]] = max(worst.get(test["test"], 0), test["seconds"])
    if seen:
        print(f"\nMost often in the {len(records)} runs' 20 slowest (times, max seconds, test):")
    for name, count in seen.most_common(MOST_OFTEN):
        print(f"{count:>5}  {worst[name]:>7.1f}  {name}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workflow", choices=sorted(ARTIFACTS), default="ci")
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--branch")
    parser.add_argument("--event")
    parser.add_argument("--repo")
    args = parser.parse_args(argv)
    records, skipped = [], 0
    with tempfile.TemporaryDirectory() as folder:
        for run in list_runs(args):
            data = download(run, args, folder)
            if data is None:
                skipped += 1
            else:
                records.append(data)
    report(records, skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
