#!/usr/bin/env python3
"""CI time per run and per test (CI-TIME-1): the slowest tests, the test step's time, budgets.

Usage: timing.py [--go] --start FILE --out FILE [--title TEXT] RESULTS [RESULTS ...]
       timing.py --go --log STREAM [STREAM ...]
  RESULTS   pytest junit XML files, or with --go `go test -json` streams
  --start   a file holding the test step's start time (`date +%s`); the step ends now
  --out     the timing record (JSON) to write: run, commit, event, times, slowest tests
  --log     print the streams' test output as `go test -v` text, for the job log
Writes the 20 slowest tests, the test count, the suite time and the step time as Markdown to
$GITHUB_STEP_SUMMARY (stdout when unset). Prints a ::warning:: for each test over 60 s and
for a step over 90 s (the goal); exits 1 when a test takes 90 s or more, when the step takes
over 240 s (the hard limit), or when a results file is missing, unreadable or holds no test.
A test's time is its whole time: setup, call and teardown in pytest; for a Go test with
subtests, the subtests too. Standard library only: CI runs it with the server's python3.
"""

import argparse
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

WARN_TEST_SECONDS = 60
MAX_TEST_SECONDS = 90
GOAL_STEP_SECONDS = 90
MAX_STEP_SECONDS = 240
SLOWEST = 20


class TimingError(Exception):
    """A results or start file that is missing, unreadable or holds no test."""


def read(path):
    try:
        return Path(path).read_text()
    except OSError as error:
        raise TimingError(f"{path}: cannot read ({error.strerror or error})") from error


def junit(paths):
    """Tests as {name: seconds} and the summed suite (wall) times of pytest junit files."""
    tests, suite = {}, 0.0
    for path in paths:
        try:
            root = ET.fromstring(read(path))
        except ET.ParseError as error:
            raise TimingError(f"{path}: not junit XML ({error})") from error
        for element in root.iter("testsuite"):
            suite += float(element.get("time") or 0)
            for case in element.iter("testcase"):
                name = f"{case.get('classname')}::{case.get('name')}"
                tests[name] = tests.get(name, 0.0) + float(case.get("time") or 0)
    return tests, suite


def go_events(paths):
    """The JSON events of `go test -json` streams; a line that is not JSON is skipped."""
    events = []
    for path in paths:
        for line in read(path).splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def short_packages(packages):
    """Each package path without the leading folders every package shares."""
    parts = [package.split("/") for package in packages]
    common = 0
    while parts and all(len(p) > common + 1 for p in parts):
        if len({p[common] for p in parts}) > 1:
            break
        common += 1
    return {package: "/".join(package.split("/")[common:]) for package in packages}


def go_tests(paths):
    """Tests as {name: seconds} and the slowest package's time of `go test -json` streams."""
    done = [e for e in go_events(paths) if e.get("Action") in ("pass", "fail", "skip")]
    short = short_packages({e.get("Package", "") for e in done})
    tests, slowest_package = {}, 0.0
    for event in done:
        seconds = float(event.get("Elapsed") or 0)
        if event.get("Test"):
            tests[f"{short[event.get('Package', '')]} {event['Test']}"] = seconds
        else:
            slowest_package = max(slowest_package, seconds)
    return tests, slowest_package


def go_log(paths):
    """The test output of `go test -json` streams, as `go test -v` prints it."""
    return "".join(e.get("Output", "") for e in go_events(paths))


def step_seconds(start_file):
    text = read(start_file).strip()
    try:
        return max(0, int(time.time()) - int(text))
    except ValueError as error:
        raise TimingError(f"{start_file}: not a start time ({text[:40]!r})") from error


def record(tests, suite_seconds, step, kind):
    slowest = sorted(tests.items(), key=lambda item: (-item[1], item[0]))[:SLOWEST]
    return {
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "sha": os.environ.get("HEAD_SHA") or os.environ.get("GITHUB_SHA", ""),
        "event": os.environ.get("GITHUB_EVENT_NAME", ""),
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": kind,
        "step_seconds": step,
        "suite_seconds": round(suite_seconds, 1),
        "test_count": len(tests),
        "slowest": [{"test": name, "seconds": round(seconds, 2)} for name, seconds in slowest],
    }


def summary(data, title):
    suite = "pytest" if data["kind"] == "pytest" else "slowest Go package"
    lines = [
        f"### {title}",
        "",
        f"Test step: **{data['step_seconds']} s** (goal {GOAL_STEP_SECONDS} s, limit "
        f"{MAX_STEP_SECONDS} s) · {suite}: {data['suite_seconds']} s · "
        f"{data['test_count']} tests · a test over {WARN_TEST_SECONDS} s warns, "
        f"{MAX_TEST_SECONDS} s or more fails",
        "",
        "| # | Test | Seconds |",
        "|---:|---|---:|",
    ]
    for rank, test in enumerate(data["slowest"], 1):
        name = test["test"].replace("|", "\\|")
        lines.append(f"| {rank} | `{name}` | {test['seconds']:.2f} |")
    return "\n".join(lines) + "\n"


def budgets(tests, step):
    """Print the annotations; return the failures (empty when every budget holds)."""
    failures = []
    for name, seconds in sorted(tests.items(), key=lambda item: -item[1]):
        if seconds >= MAX_TEST_SECONDS:
            failures.append(f"{name} took {seconds:.1f} s ({MAX_TEST_SECONDS} s or more: split it)")
        elif seconds > WARN_TEST_SECONDS:
            print(
                f"::warning title=Slow test::{name} took {seconds:.1f} s "
                f"(over {WARN_TEST_SECONDS} s; {MAX_TEST_SECONDS} s fails)"
            )
    if step > MAX_STEP_SECONDS:
        failures.append(f"the test step took {step} s (over the {MAX_STEP_SECONDS} s limit)")
    elif step > GOAL_STEP_SECONDS:
        print(
            f"::warning title=Slow test step::the test step took {step} s "
            f"(over the {GOAL_STEP_SECONDS} s goal; {MAX_STEP_SECONDS} s is the limit)"
        )
    for failure in failures:
        print(f"::error title=CI time::{failure}")
    return failures


def write_summary(text):
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        print(text)
        return
    with open(target, "a") as handle:
        handle.write(text)


def parse(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--go", action="store_true", help="go test -json streams")
    parser.add_argument("--log", action="store_true", help="print the Go test output")
    parser.add_argument("--start", type=Path, help="the test step's start time")
    parser.add_argument("--out", type=Path, help="the timing record (JSON)")
    parser.add_argument("--title", default="CI time")
    args = parser.parse_args(argv)
    if args.log and not args.go:
        parser.error("--log reads go test -json streams: add --go")
    if not args.log and not (args.start and args.out):
        parser.error("--start and --out are required")
    return args


def main(argv=None):
    args = parse(argv)
    try:
        if args.log:
            sys.stdout.write(go_log(args.results))
            return 0
        tests, suite = (go_tests if args.go else junit)(args.results)
        if not tests:
            raise TimingError(f"no test in {', '.join(map(str, args.results))}")
        step = step_seconds(args.start)
    except TimingError as error:
        print(f"::error title=CI time::{error}")
        return 1
    data = record(tests, suite, step, "go" if args.go else "pytest")
    args.out.write_text(json.dumps(data, indent=2) + "\n")
    write_summary(summary(data, args.title))
    failures = budgets(tests, step)
    if failures:
        print("CI time over budget:\n  " + "\n  ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
