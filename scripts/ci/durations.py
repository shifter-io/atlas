#!/usr/bin/env python3
"""Refresh tests/durations.json, the input of tests/conftest.py's longest-first xdist order.

Usage: durations.py [--from-run ID [--repo OWNER/REPO]] [--out FILE] [--min S] [RESULTS ...]
  RESULTS     pytest junit XML files, or folders holding junit*.xml (a downloaded
              `ci-timing` artifact: tier1 keeps its junit.xml and junit-serial.xml there)
  --from-run  download that ci.yml run's `ci-timing` artifact with `gh run download`
  --out       the file to write (default tests/durations.json)
  --min       keep the tests of at least S seconds (default 1); the rest count as fast
Writes {node id: seconds}, rounded to 0.1 s and sorted, as tests/conftest.py reads it. A
test's time is junit's: setup, call and teardown. A test in several inputs (runs) gets its
mean. The node id comes from junit's classname: its longest dotted prefix that names a .py
file of this checkout is the file, the rest its classes; a test whose file is gone is left out.

When: after splitting a slow test or adding one (anything over a second or so), take a full
tier1 run of main, the merge queue's latest green run:
  gh run list --workflow ci.yml --event merge_group --status success -L 1
  python3 scripts/ci/durations.py --from-run <run id>
and commit tests/durations.json. A test missing from the file only runs later than it
could; a stale entry is ignored. Standard library only, like timing.py, which reads junit.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from timing import TimingError, junit

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = "ci-timing"


def results(paths):
    """The junit files of `paths`: each file itself, each folder's junit*.xml."""
    files = []
    for path in paths:
        found = sorted(path.glob("junit*.xml")) if path.is_dir() else [path]
        if not found:
            raise TimingError(f"{path}: no junit*.xml")
        files += found
    return files


def node_id(test, root=ROOT):
    """`classname::name` from junit as a pytest node id, or None when its file is gone."""
    classname, _, name = test.partition("::")
    parts = classname.split(".")
    for cut in range(len(parts), 0, -1):
        path = "/".join(parts[:cut]) + ".py"
        if (root / path).is_file():
            return "::".join([path, *parts[cut:], name])
    return None


def durations(files, minimum, root=ROOT):
    """{node id: seconds} of the tests of at least `minimum` seconds, averaged over `files`."""
    seen = {}
    for path in files:
        for test, seconds in junit([path])[0].items():
            nodeid = node_id(test, root)
            if nodeid:
                seen.setdefault(nodeid, []).append(seconds)
    means = {nodeid: sum(times) / len(times) for nodeid, times in seen.items()}
    return {nodeid: round(s, 1) for nodeid, s in means.items() if s >= minimum}


def download(run, repo, folder):
    """Run `run`'s ci-timing artifact, downloaded into a new folder in `folder`."""
    target = folder / str(run)
    command = ["gh", "run", "download", str(run), "--name", ARTIFACT, "--dir", str(target)]
    if repo:
        command += ["-R", repo]
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0:
        raise TimingError(f"run {run}: no {ARTIFACT} artifact ({done.stderr.strip()})")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results", nargs="*", type=Path)
    parser.add_argument("--from-run")
    parser.add_argument("--repo")
    parser.add_argument("--out", type=Path, default=ROOT / "tests" / "durations.json")
    parser.add_argument("--min", type=float, default=1.0)
    args = parser.parse_args(argv)
    if not args.results and not args.from_run:
        parser.error("give junit files or folders, or --from-run")
    with tempfile.TemporaryDirectory() as folder:
        try:
            paths = list(args.results)
            if args.from_run:
                paths.append(download(args.from_run, args.repo, Path(folder)))
            slow = durations(results(paths), args.min)
        except TimingError as error:
            print(f"durations: {error}", file=sys.stderr)
            return 1
    args.out.write_text(json.dumps(slow, indent=0, sort_keys=True) + "\n")
    print(f"{len(slow)} tests of at least {args.min:g} s written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
