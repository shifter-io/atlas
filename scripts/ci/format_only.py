#!/usr/bin/env python3
"""Formatting-only change attestation (CI-FMT-1, temporary: CI-FMT-1C deletes it).

Usage: format_only.py <repo> <base-ref> [<head-ref>]   (head defaults to HEAD)

With MB = merge-base(base, head), the change MB..head is formatting-only when:
1. it changes at least one file;
2. every changed file is modified in place (status M, no rename detection), keeps its mode
   (100644 or 100755) and is a .py file under src/, tests/ or scripts/; so pyproject.toml,
   uv.lock and every other configuration file are the base's;
3. every head file is byte for byte what this environment's ruff (`python -m ruff format
   --force-exclude --stdin-filename <path>`, run in <repo>) makes of the MB file;
4. the MB and head files parse to the same AST after Black's three equivalence
   normalisations: a string that is a whole expression statement is compared with each line
   stripped and then the whole stripped, a tuple `del` target is flattened into its
   elements, and `Constant.kind` is ignored.
Prints `OK <path>` per checked file (stopping at the first `NOT <path>: <reason>`), then
`ruff <version>, base <MB>, <n> files`, then exactly `FORMAT_ONLY=true` or
`FORMAT_ONLY=false` as the last line. Any git, ruff or parse error is printed and gives
`FORMAT_ONLY=false`. Exit 0 for both verdicts, 2 on wrong usage. tier1 reads this script
from the merge base, so a change cannot rewrite the rule that exempts it.
"""

import ast
import re
import subprocess
import sys

RUFF = [sys.executable, "-m", "ruff"]
PATH = re.compile(r"(src|tests|scripts)/.+\.py")
MODES = ("100644", "100755")


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True).stdout


class Normalise(ast.NodeTransformer):
    """Black's equivalence normalisations, and nothing broader."""

    def visit_Expr(self, node):
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            lines = (line.strip() for line in node.value.value.splitlines())
            node.value.value = "\n".join(lines).strip()
        return self.generic_visit(node)

    def visit_Delete(self, node):
        node.targets = [
            element
            for target in node.targets
            for element in (target.elts if isinstance(target, ast.Tuple) else [target])
        ]
        return self.generic_visit(node)

    def visit_Constant(self, node):
        node.kind = None
        return node


def normal_ast(source, path="<source>"):
    return ast.dump(Normalise().visit(ast.parse(source, path)))


def check(repo, meta, path):
    """None when this diff entry is formatting-only, else the reason it is not."""
    old_mode, new_mode, old_blob, new_blob, status = meta.lstrip(":").split(" ")
    if status != "M":
        return f"status {status}"
    if old_mode != new_mode or new_mode not in MODES:
        return f"mode {old_mode} -> {new_mode}"
    if not PATH.fullmatch(path):
        return "not a .py file under src/, tests/ or scripts/"
    before, after = git(repo, "cat-file", "blob", old_blob), git(repo, "cat-file", "blob", new_blob)
    formatted = subprocess.run(
        [*RUFF, "format", "--force-exclude", "--stdin-filename", path, "-"],
        input=before,
        cwd=repo,
        capture_output=True,
    )
    if formatted.returncode:
        return f"ruff format failed: {formatted.stderr.decode(errors='replace').strip()}"
    if formatted.stdout != after:
        return "not the ruff format output of the base"
    if normal_ast(before, path) != normal_ast(after, path):
        return "the AST differs"
    return None


def attest(repo, base, head):
    merge_base = git(repo, "merge-base", base, head).decode().strip()
    raw = git(repo, "diff", "--raw", "-z", "--no-renames", "--no-abbrev", merge_base, head)
    fields = raw.decode(errors="surrogateescape").split("\0")[:-1]
    entries = list(zip(fields[::2], fields[1::2], strict=True))
    version = subprocess.run(
        [*RUFF, "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    attested = bool(entries)
    if not entries:
        print("NOT: no changed file")
    for meta, path in entries:
        reason = check(repo, meta, path)
        if reason:
            print(f"NOT {path}: {reason}")
            attested = False
            break
        print(f"OK {path}")
    print(f"{version}, base {merge_base}, {len(entries)} files")
    return attested


def main():
    if len(sys.argv) not in (3, 4):
        print("usage: format_only.py <repo> <base-ref> [<head-ref>]", file=sys.stderr)
        return 2
    repo, base = sys.argv[1], sys.argv[2]
    head = sys.argv[3] if len(sys.argv) > 3 else "HEAD"
    try:
        attested = attest(repo, base, head)
    except Exception as error:  # every failure means every gate applies
        detail = getattr(error, "stderr", None)
        detail = detail.decode(errors="replace").strip() if isinstance(detail, bytes) else ""
        print(f"error: {error} {detail}".rstrip())
        attested = False
    print(f"FORMAT_ONLY={'true' if attested else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
