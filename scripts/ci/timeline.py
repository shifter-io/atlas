#!/usr/bin/env python3
"""Sum the opt-in test timeline (CI_TIMELINE=1, tests/conftest.py) per xdist worker.

Usage: timeline.py <dir with timeline-*.jsonl> [--top N] [--durations FILE [--min S]]
Prints the run's wall time (first test start to last test end), then per worker its busy
time (setup + call + teardown), idle time (the rest of the wall) and test count, the
totals spent creating scratch databases and initialising schemas, and the N longest tests
with their share of the wall time. --durations writes each test of at least S seconds
(default 1) as {node id: seconds}, the input of tests/conftest.py's longest-first order.
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

PHASES = ("setup", "call", "teardown")
SPANS = ("create_database", "initialize")


def load(directory):
    return [
        json.loads(line)
        for path in sorted(Path(directory).glob("timeline-*.jsonl"))
        for line in path.read_text().splitlines()
    ]


def summarize(records, top=10):
    phases = [r for r in records if r["kind"] in PHASES]
    if not phases:
        return {"wall": 0.0, "workers": {}, "spans": {}, "phases": {}, "longest": [], "tests": {}}
    begin = min(r["start"] for r in phases)
    wall = max(r["stop"] for r in phases) - begin
    workers = defaultdict(lambda: {"busy": 0.0, "tests": 0})
    tests = defaultdict(float)
    totals = defaultdict(float)
    for r in phases:
        span = r["stop"] - r["start"]
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
        "spans": spans,
        "phases": dict(totals),
        "longest": [(nodeid, seconds, seconds / wall) for nodeid, seconds in longest[:top]],
        "tests": dict(longest),
    }


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
    for kind, seconds in {**summary["phases"], **summary["spans"]}.items():
        lines.append(f"{kind} {seconds:.1f} s ({seconds / busy:.1%} of busy)" if busy else kind)
    for nodeid, seconds, share in summary["longest"]:
        lines.append(f"{seconds:6.1f} s {share:6.1%} of wall  {nodeid}")
    return "\n".join(lines)


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
