"""Rename the product's old name to Atlas in a checkout (RENAME-ATLAS-1); scripts/rename_to_atlas.sh
runs it. Idempotent: a second run changes nothing, and on a branch with new files that carry the old
name it renames those too.

Every spelling of the old name (`proxy_index`, `proxy-index`, `Proxy Index`, `PROXY_INDEX`,
`ProxyIndex`, `proxyIndex`, `proxyindex`) becomes `atlas`, `Atlas` or `ATLAS` by its case, and
"a <old name>" becomes "an Atlas". Paths are moved with `git mv` (a plain move for files git does
not track yet); contents are rewritten in place and left unstaged. What keeps the old name is in
scripts/rename_to_atlas.keep, one group per reason (docs/plans/atlas-rename.md explains each):
`file <glob>` keeps a whole file's bytes, `text <glob> <regex>` keeps the matching spans of a
file's text and of its path.

  rename_to_atlas.sh            rename this checkout (the git top level of the current directory)
  rename_to_atlas.sh --dry-run  print what would move or change, change nothing
  rename_to_atlas.sh --check    print every old name outside the keep-list; exit 1 if any
  rename_to_atlas.sh --report   print every kept span and every rewritten token, with counts
"""

from __future__ import annotations

import argparse
import collections
import fnmatch
import gzip
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

KEEP_FILE = Path(__file__).with_name("rename_to_atlas.keep")
# Each spelling's case decides the new one: all capitals, a capital first letter, or lower case.
OLD = re.compile(r"(?:\b(?P<article>[Aa]) )?(?P<name>proxy[-_ ]?index)", re.IGNORECASE)
# Directories never read: version control, environments, caches and the private local folders.
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
             ".secrets", ".state", "results", "artifacts", "reports", "onboarding-networks",
             "dist", "build"}


@dataclass
class Keep:
    """The keep-list: whole files by glob, and (glob, compiled regex, group) text rules."""

    files: list[tuple[str, str]] = field(default_factory=list)
    texts: list[tuple[str, re.Pattern[str], str]] = field(default_factory=list)

    def whole(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, glob) for glob, _ in self.files)

    def rules(self, path: str) -> list[tuple[re.Pattern[str], str]]:
        return [(rx, group) for glob, rx, group in self.texts if fnmatch.fnmatchcase(path, glob)]


def load_keep(path: Path = KEEP_FILE) -> Keep:
    keep, group = Keep(), "?"
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if line.startswith("## "):
            group = line[3:].split(":", 1)[0].strip()
        elif line.startswith("file "):
            keep.files.append((line[5:].strip(), group))
        elif line.startswith("text "):
            glob, _, regex = line[5:].strip().partition(" ")
            try:
                keep.texts.append((glob, re.compile(regex.strip()), group))
            except re.error as error:
                raise SystemExit(f"{path.name}:{number}: {error}") from error
        elif line.strip() and not line.startswith("#"):
            raise SystemExit(f"{path.name}:{number}: not a comment, group, file or text line")
    return keep


def kept_spans(text: str, rules) -> list[tuple[int, int, str]]:
    """The (start, end, group) spans the rules keep, merged where they overlap."""
    spans = sorted((m.start(), m.end(), group) for rx, group in rules for m in rx.finditer(text)
                   if m.end() > m.start())
    merged: list[tuple[int, int, str]] = []
    for start, end, group in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]), merged[-1][2])
        else:
            merged.append((start, end, group))
    return merged


def new_name(match: re.Match[str]) -> str:
    name, article = match["name"], match["article"]
    letters = [c for c in name if c.isalpha()]
    atlas = "ATLAS" if all(c.isupper() for c in letters) else "Atlas" if name[0].isupper() else "atlas"
    return f"{article}n {atlas}" if article else atlas


def rewrite(text: str, rules, seen: collections.Counter | None = None) -> str:
    """text with every old name outside the kept spans renamed (seen counts what was kept and
    rewritten, for --report)."""
    out, at = [], 0
    for start, end, group in kept_spans(text, rules) + [(len(text), len(text), "")]:
        part = text[at:start]
        if seen is not None:
            for m in OLD.finditer(part):
                seen[("rewritten", m["name"], new_name(m))] += 1
            if end > start and OLD.search(text[start:end]):
                seen[("kept", group, text[start:end])] += 1
        out.append(OLD.sub(new_name, part))
        out.append(text[start:end])
        at = end
    return "".join(out)


def leftovers(text: str, rules) -> list[tuple[int, str]]:
    """(line number, line) of each old name outside the kept spans."""
    found, at = [], 0
    for start, end, _ in kept_spans(text, rules) + [(len(text), len(text), "")]:
        for m in OLD.finditer(text, at, start):
            line = text.count("\n", 0, m.start()) + 1
            found.append((line, text.splitlines()[line - 1] if text else ""))
        at = end
    return found


def files(root: Path) -> list[str]:
    """Repository-relative paths: git's tracked and untracked-but-not-ignored files, or every file
    outside SKIP_DIRS where there is no git (a synced copy on a test server)."""
    try:
        listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                                 "--exclude-standard"], capture_output=True, check=True).stdout
        return sorted({p for p in listed.decode().split("\0") if p and (root / p).is_file()})
    except (OSError, subprocess.CalledProcessError):
        found = []
        for top, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
            found += [os.path.relpath(os.path.join(top, n), root) for n in names]
        return sorted(found)


def read_text(path: Path) -> str | None:
    """The file's text (a .gz file's uncompressed text), or None for a symlink or binary file."""
    if path.is_symlink():
        return None
    data = path.read_bytes()
    if path.suffix == ".gz":
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError):
            return None
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode()
    except UnicodeDecodeError:
        return None


def check(root: Path, keep: Keep) -> list[str]:
    """`path:line: text` for every old name outside the keep-list, in paths and contents."""
    found = []
    for path in files(root):
        if keep.whole(path):
            continue
        rules = keep.rules(path)
        if leftovers(path, rules):
            found.append(f"{path}: the path")
        text = read_text(root / path)
        if text is not None:
            found += [f"{path}:{n}: {line.strip()[:160]}" for n, line in leftovers(text, rules)]
    return found


def moves(root: Path, keep: Keep, paths: list[str]) -> list[tuple[str, str]]:
    """(old, new) for every path the rename changes; a whole-file keep keeps its content, not its
    path, so its path is renamed with the rest."""
    found = []
    for path in paths:
        target = rewrite(path, keep.rules(path))
        if target != path:
            found.append((path, target))
    return found


def tracked(root: Path) -> set[str]:
    try:
        listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True,
                                check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return set()
    return {p for p in listed.decode().split("\0") if p}


def move(root: Path, pairs: list[tuple[str, str]], dry_run: bool) -> list[str]:
    """Move each (old, new): `git mv` for a tracked file, a plain move for an untracked one. A
    whole directory moves in one `git mv` when every file in it moves along and its new name is
    free. Returns the problems (a target that exists already)."""
    problems, under_git, done = [], tracked(root), set()
    for old_dir, new_dir in directory_moves(root, pairs):
        if dry_run:
            print(f"move {old_dir}/ -> {new_dir}/")
        else:
            subprocess.run(["git", "-C", str(root), "mv", old_dir, new_dir], check=True)
        done |= {old for old, _ in pairs if old.startswith(old_dir + "/")}
    for old, new in pairs:
        if old in done:
            continue
        if (root / new).exists():
            problems.append(f"{old}: not moved, {new} exists")
        elif dry_run:
            print(f"move {old} -> {new}")
        elif old in under_git:
            (root / new).parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "-C", str(root), "mv", old, new], check=True)
        else:
            os.renames(root / old, root / new)
    return problems


def directory_moves(root: Path, pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(old dir, new dir) for each renamed directory that moves whole: every file below it moves to
    the same place below the new name, and the new name does not exist yet."""
    candidates = set()
    for old, new in pairs:
        a, b = Path(old).parts, Path(new).parts
        differ = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        if len(a) == len(b) and len(differ) == 1 and differ[0] < len(a) - 1:
            i = differ[0]
            candidates.add(("/".join(a[: i + 1]), "/".join(b[: i + 1])))
    everything, moving = files(root), dict(pairs)
    found = []
    for old_dir, new_dir in sorted(candidates):
        inside = [p for p in everything if p.startswith(old_dir + "/")]
        whole = all(moving.get(p) == new_dir + p[len(old_dir):] for p in inside)
        if whole and not (root / new_dir).exists():
            found.append((old_dir, new_dir))
    return [(o, n) for o, n in found if not any(o.startswith(p + "/") for p, _ in found)]


def sort_lock(text: str) -> str:
    """uv.lock with its packages in name order again (uv writes them sorted, and `uv sync --locked`
    refuses a lock it would write differently): the renamed project moves to its place. Every
    package block ends with a newline, so sorted blocks join back unchanged."""
    head, *blocks = text.split("\n[[package]]\n")
    blocks.sort(key=lambda block: block.split("\n", 1)[0])
    return "\n[[package]]\n".join([head, *blocks])


def rename(root: Path, keep: Keep, dry_run: bool, seen=None) -> tuple[int, list[str]]:
    """Move the paths, then rewrite the contents; returns (paths and files changed, problems)."""
    pairs = moves(root, keep, files(root))
    problems = move(root, pairs, dry_run)
    target = dict(pairs) if dry_run else {}
    changed = len(pairs)
    for path in files(root):
        new = target.get(path, path)
        if keep.whole(new) or path.endswith(".gz"):
            continue
        text = read_text(root / path)
        if text is None:
            continue
        renamed = rewrite(text, keep.rules(new), seen)
        if new == "uv.lock":
            renamed = sort_lock(renamed)
        if renamed != text:
            changed += 1
            if dry_run:
                print(f"edit {new}")
            else:
                (root / path).write_bytes(renamed.encode())
    return changed, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rename_to_atlas.sh", description=__doc__.split("\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the changes, change nothing")
    mode.add_argument("--check", action="store_true", help="list old names outside the keep-list")
    mode.add_argument("--report", action="store_true", help="count kept spans and rewrites")
    parser.add_argument("--root", type=Path, default=None, help="checkout (default: git top level)")
    args = parser.parse_args(argv)
    root = args.root or Path(subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                            capture_output=True, text=True, check=True).stdout.strip())
    keep = load_keep()
    if args.check:
        found = check(root, keep)
        print("\n".join(found) or "no old name outside the keep-list")
        return 1 if found else 0
    seen: collections.Counter = collections.Counter()
    changed, problems = rename(root, keep, args.dry_run or args.report, seen if args.report else None)
    if args.report:
        for (kind, a, b), n in sorted(seen.items(), key=lambda kv: (kv[0][0], -kv[1], kv[0])):
            print(f"{n:6d}  {kind:9s} {a!s:24.24s} {b}")
        return 0
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"{changed} change(s){' (dry run)' if args.dry_run else ''}"
          + ("" if args.dry_run or not changed else "; review with `git status`, then commit"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
