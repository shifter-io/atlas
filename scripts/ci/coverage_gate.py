#!/usr/bin/env python3
"""CAMP-1 Tier-1 gate: 100% line AND branch coverage for the delete and project paths.

Usage: coverage_gate.py <coverage.json> <repo> <target> [<target> ...]
  target = <path>                      the whole file must be fully covered
         | <path>:<Qual.func>,<...>    only these functions (methods as Class.method)
The JSON comes from `coverage json` on a run with branch coverage on (`--cov-branch`).
A missing line or a missing branch (by its source line) inside a target fails the gate.
A target file or function that does not exist also fails, so the gate cannot pass
vacuously. Exit 0 only when every target is complete.
"""

import ast
import json
import sys
from pathlib import Path


def spans(source):
    """Qualified function name -> (first line, last line)."""
    found = {}

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                first = min([child.lineno] + [d.lineno for d in child.decorator_list])
                found[prefix + child.name] = (first, child.end_lineno)
                visit(child, f"{prefix}{child.name}.")

    visit(ast.parse(source), "")
    return found


def main():
    data = json.loads(Path(sys.argv[1]).read_text())
    if not data.get("meta", {}).get("branch_coverage"):
        print("FAIL coverage data has no branch coverage (run pytest with --cov-branch)")
        return 1
    repo = Path(sys.argv[2])
    files = data["files"]
    failures = 0
    for target in sys.argv[3:]:
        path, _, names = target.partition(":")
        record = next((v for k, v in files.items() if k.endswith(path)), None)
        if record is None:
            print(f"FAIL {path}: not in coverage data")
            failures += 1
            continue
        missing_lines = set(record["missing_lines"])
        missing_branches = {start for start, _ in record.get("missing_branches", [])}
        if not names:
            ranges = {"<file>": (1, 10**9)}
        else:
            known = spans((repo / path).read_text())
            ranges = {}
            for name in names.split(","):
                if name not in known:
                    print(f"FAIL {path}:{name}: function not found")
                    failures += 1
                    continue
                ranges[name] = known[name]
        for name, (first, last) in ranges.items():
            lines = sorted(n for n in missing_lines if first <= n <= last)
            branches = sorted(n for n in missing_branches if first <= n <= last)
            if lines or branches:
                print(f"FAIL {path}:{name}: missing lines {lines} missing branches from {branches}")
                failures += 1
            else:
                print(f"OK   {path}:{name}: 100% lines and branches")
    print(f"RESULT {'FAIL' if failures else 'PASS'} ({failures} failing targets)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
