#!/usr/bin/env python3
"""Sum the opt-in test timeline (CI_TIMELINE=1, tests/conftest.py) per xdist worker.

Usage: timeline.py <dir with timeline-*.jsonl> [--top N] [--durations FILE [--min S]]
Prints the run's wall time (first test start to last test end), then per worker its busy
time (setup + call + teardown), idle time (the rest of the wall) and test count, when each
worker ended (its last test's end, from the first test's start), the totals spent creating
scratch databases and initialising schemas, and the N longest tests with their share of the
wall time, and the fixed costs around the tests: pytest's start to the first test and the
last test to its end (the CI step's `pytest` span), when the workers loaded and finished
collecting, combining the coverage and each report, and each background check's wall and CPU
time with its end (from the first test's start). --durations writes each test of at least S
seconds (default 1) as {node id: seconds}, the format of tests/durations.json
(scripts/ci/durations.py builds that one from a tier1 run's junit).
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

PHASES = ("setup", "call", "teardown")
SPANS = ("create_database", "initialize")
COVERAGE = ("combine", "report", "xml_report", "json_report")


def load(directory):
    return [
        json.loads(line)
        for path in sorted(Path(directory).glob("timeline-*.jsonl"))
        for line in path.read_text().splitlines()
    ]


def summarize(records, top=10):
    phases = [r for r in records if r["kind"] in PHASES]
    if not phases:
        return {
            "wall": 0.0,
            "workers": {},
            "ends": {},
            "spans": {},
            "phases": {},
            "longest": [],
            "tests": {},
            "fixed": fixed(records, None, None),
        }
    begin = min(r["start"] for r in phases)
    wall = max(r["stop"] for r in phases) - begin
    workers = defaultdict(lambda: {"busy": 0.0, "tests": 0})
    tests = defaultdict(float)
    totals = defaultdict(float)
    ends = defaultdict(float)
    for r in phases:
        span = r["stop"] - r["start"]
        ends[r["worker"]] = max(ends[r["worker"]], r["stop"] - begin)
        workers[r["worker"]]["busy"] += span
        workers[r["worker"]]["tests"] += r["kind"] == "call"
        tests[r["nodeid"]] += span
        totals[r["kind"]] += span
    for worker in workers.values():
        worker["idle"] = wall - worker["busy"]
    spans = {kind: 0.0 for kind in SPANS}
    for r in records:
        if r["kind"] in SPANS:
            spans[r["kind"]] += r["stop"] - r["start"]
    longest = sorted(tests.items(), key=lambda item: -item[1])
    return {
        "wall": wall,
        "workers": dict(sorted(workers.items())),
        "ends": dict(sorted(ends.items())),
        "spans": spans,
        "phases": dict(totals),
        "longest": [(nodeid, seconds, seconds / wall) for nodeid, seconds in longest[:top]],
        "tests": dict(longest),
        "fixed": fixed(records, begin, begin + wall),
    }


def fixed(records, first, last):
    """The costs around the tests: pytest's start and end, collection, coverage, checks."""
    of = defaultdict(list)
    for r in records:
        of[r["kind"]].append(r)
    result = {"coverage": {}, "checks": {}}
    for kind in COVERAGE:
        if of[kind]:
            result["coverage"][kind] = sum(r["stop"] - r["start"] for r in of[kind])
    if of["pytest"] and first is not None:
        pytest = of["pytest"][0]
        result["before"] = first - pytest["start"]
        result["after"] = pytest["stop"] - last
        if of["collect"]:
            loaded = [r["start"] - pytest["start"] for r in of["collect"]]
            collected = [r["stop"] - pytest["start"] for r in of["collect"]]
            result["collect"] = (min(loaded), max(loaded), min(collected), max(collected))
    for r in of["check"]:
        result["checks"][r["name"]] = {
            "wall": r["stop"] - r["start"],
            "cpu": r["user"] + r["sys"],
            "end": None if first is None else r["stop"] - first,
        }
    return result


def fixed_report(costs):
    lines = []
    if "before" in costs:
        lines.append(
            f"pytest start to first test {costs['before']:.1f} s, "
            f"last test to end {costs['after']:.1f} s"
        )
    if "collect" in costs:
        first, last, done, all_done = costs["collect"]
        lines.append(
            f"workers loaded {first:.1f}-{last:.1f} s, collected {done:.1f}-{all_done:.1f} s "
            "after pytest start"
        )
    if costs["coverage"]:
        each = ", ".join(f"{kind} {s:.1f} s" for kind, s in costs["coverage"].items())
        lines.append(f"coverage: {each}")
    for name, check in sorted(costs["checks"].items()):
        end = "" if check["end"] is None else f", ends at {check['end']:.1f} s"
        lines.append(f"check {name}: wall {check['wall']:.1f} s, cpu {check['cpu']:.1f} s{end}")
    return lines


def report(summary):
    wall = summary["wall"]
    lines = [f"wall {wall:.1f} s"]
    busy = sum(w["busy"] for w in summary["workers"].values())
    for name, worker in summary["workers"].items():
        lines.append(
            f"{name}: busy {worker['busy']:.1f} s, idle {worker['idle']:.1f} s, "
            f"{worker['tests']} tests"
        )
    lines.append(f"busy total {busy:.1f} s over {len(summary['workers'])} workers")
    if summary["ends"]:
        ends = summary["ends"].values()
        each = ", ".join(f"{name} {end:.1f}" for name, end in summary["ends"].items())
        lines.append(f"workers end {min(ends):.1f}-{max(ends):.1f} s: {each}")
    for kind, seconds in {**summary["phases"], **summary["spans"]}.items():
        lines.append(f"{kind} {seconds:.1f} s ({seconds / busy:.1%} of busy)" if busy else kind)
    for nodeid, seconds, share in summary["longest"]:
        lines.append(f"{seconds:6.1f} s {share:6.1%} of wall  {nodeid}")
    return "\n".join(lines + fixed_report(summary["fixed"]))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--durations", type=Path)
    parser.add_argument("--min", type=float, default=1.0)
    args = parser.parse_args(argv)
    summary = summarize(load(args.directory), args.top)
    print(report(summary))
    if args.durations:
        slow = {test: round(s, 1) for test, s in summary["tests"].items() if s >= args.min}
        args.durations.write_text(json.dumps(slow, indent=0, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
