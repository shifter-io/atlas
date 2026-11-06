#!/usr/bin/env python3
"""CAMP-1 Tier-1 source-limit check (GLOBAL "Common engineering standards").

Usage: limits_check.py <repo> <base-ref> [<head-ref>]   (head defaults to HEAD)

Checks every Python file under src/ or scripts/ that the diff base...head touches:
- file length: warning 400, hard 700 lines;
- each function or method whose lines intersect the diff: length warning 50, hard 80;
  parameters warning 5, hard 7 (self/cls excluded); nesting warning 3, hard 4;
- changed production lines (added + deleted in src/ and scripts/ .py): warning 400, hard 800.
Cognitive complexity is checked separately with ruff C901 (see PLAN.md, Tier 1).
Exit 1 when a hard limit is exceeded; warnings are printed but do not fail. A function that
was already over a hard limit at the base is reported as LEGACY (not failing) while it does not
grow (length may grow by at most 5 lines, parameters and nesting not at all).
"""

import ast
import re
import subprocess
import sys

NESTING = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    *((ast.Match,) if hasattr(ast, "Match") else ()),
)


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True, text=True
    ).stdout


def changed_lines(repo, base, head):
    """Map each touched .py file under src/ or scripts/ to its changed head lines."""
    touched, current = {}, None
    for line in git(repo, "diff", "-U0", f"{base}...{head}", "--", "src", "scripts").splitlines():
        if line.startswith("+++ "):
            name = line[4:].removeprefix("b/")
            current = name if name.endswith(".py") and name != "/dev/null" else None
            if current:
                touched.setdefault(current, set())
        elif line.startswith("@@") and current:
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            start, count = int(match.group(1)), int(match.group(2) or 1)
            touched[current].update(range(start, start + max(count, 1)))
    return touched


def depth(node, level=0):
    deepest = level
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        nested = level + 1 if isinstance(child, NESTING) else level
        deepest = max(deepest, depth(child, nested))
    return deepest


def metrics(source):
    """Qualified function name -> (first line, end line, length, params, nesting)."""
    found = {}

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = child.args
                params = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
                params = [p for p in params if p not in ("self", "cls")]
                params += [a.arg for a in (args.vararg, args.kwarg) if a]
                found[prefix + child.name] = (
                    child.lineno,
                    child.end_lineno,
                    child.end_lineno - child.lineno + 1,
                    len(params),
                    depth(child),
                )
                visit(child, f"{prefix}{child.name}.")

    visit(ast.parse(source), "")
    return found


def check_function(path, name, values, old, report):
    """Report each limit one function exceeds; `values` is its metrics() tuple."""
    first, _, length, params, nest = values
    for index, (value, warn, hard, label, slack) in enumerate(
        ((length, 50, 80, "lines", 5), (params, 5, 7, "params", 0), (nest, 3, 4, "nesting", 0))
    ):
        if value > hard:
            # A pre-existing over-limit function may be touched if it does not grow.
            legacy = old is not None and old[2 + index] > hard and value <= old[2 + index] + slack
            report(
                "LEGACY" if legacy else "HARD",
                f"{path}:{first} {name}: {value} {label} "
                f"(> {hard}; base {old[2 + index] if old else 'new'})",
            )
        elif value > warn:
            report("WARN", f"{path}:{first} {name}: {value} {label} (> {warn})")


def check_file(path, source, base_source, lines, report):
    total = source.count("\n") + 1
    base_total = base_source.count("\n") + 1 if base_source else 0
    if total > 700:
        report(
            "LEGACY" if base_total > 700 and total <= base_total else "HARD",
            f"{path}: {total} lines (> 700)",
        )
    elif total > 400:
        report("WARN", f"{path}: {total} lines (> 400)")
    before = metrics(base_source) if base_source else {}
    for name, values in metrics(source).items():
        first, last = values[0], values[1]
        if set(range(first, last + 1)) & lines:
            check_function(path, name, values, before.get(name), report)


def main():
    repo, base = sys.argv[1], sys.argv[2]
    head = sys.argv[3] if len(sys.argv) > 3 else "HEAD"
    hard = []

    def report(level, message):
        print(f"{level} {message}")
        if level == "HARD":
            hard.append(message)

    numstat = git(repo, "diff", "--numstat", f"{base}...{head}", "--", "src", "scripts")
    production = sum(
        int(a) + int(d)
        for a, d, name in (row.split("\t") for row in numstat.splitlines())
        if name.endswith(".py") and a != "-"
    )
    level = "HARD" if production > 800 else "WARN" if production > 400 else "OK"
    report(level, f"changed production lines: {production}")
    for path, lines in sorted(changed_lines(repo, base, head).items()):
        if not lines:
            continue
        source = git(repo, "show", f"{head}:{path}")
        try:
            base_source = git(repo, "show", f"{base}:{path}")
        except subprocess.CalledProcessError:
            base_source = ""
        check_file(path, source, base_source, lines, report)
    print(f"RESULT {'FAIL' if hard else 'PASS'} ({len(hard)} hard)")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
