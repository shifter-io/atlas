"""Tier-1 commit identities (REPO-MERGE-1b): only GitHub noreply addresses in BASE..HEAD.

Usage: identity_check.py <repo> <base> <head>

Every commit's author and committer email, and the email of each Co-authored-by line of its
message, must be a GitHub noreply address (<id>+<login> at users.noreply.github.com). GitHub's own
web-flow identity, which commits squash merges and web edits, is allowed as the committer only.
Prints the commit and the field of each violation, never the identity. Exit 0 when every commit
passes, 1 otherwise.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator

NOREPLY = re.compile(r"[A-Za-z0-9._+\[\]-]+@users\.noreply\.github\.com", re.IGNORECASE)
WEB_FLOW = re.compile(r"noreply@github\.com", re.IGNORECASE)
# Anywhere in the message, not only git's final trailer block: a stray line counts as well.
CO_AUTHOR = re.compile(r"^\s*co-authored-by:(.*)$", re.IGNORECASE | re.MULTILINE)
EMAIL = re.compile(r"<([^<>]*)>")
# Hash, author email, committer email and raw message; `git log -z` ends each commit with NUL.
FORMAT = "%H%x1f%ae%x1f%ce%x1f%B"


def noreply(email: str) -> bool:
    return NOREPLY.fullmatch(email.strip()) is not None


def problems(author: str, committer: str, message: str) -> Iterator[str]:
    if not noreply(author):
        yield "author email is not a GitHub noreply address"
    if not (noreply(committer) or WEB_FLOW.fullmatch(committer.strip())):
        yield "committer email is not a GitHub noreply address"
    for number, value in enumerate(CO_AUTHOR.findall(message), 1):
        emails = EMAIL.findall(value)
        if len(emails) != 1 or not noreply(emails[0]):
            yield f"Co-authored-by line {number} has no GitHub noreply address"


def violations(repo: str, base: str, head: str) -> tuple[int, list[str]]:
    """The number of commits in base..head and one line per violation."""
    out = subprocess.run(
        ["git", "-C", repo, "log", "-z", f"--format={FORMAT}", f"{base}..{head}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    records = [record for record in out.split("\0") if record]
    found = []
    for record in records:
        commit, author, committer, message = record.split("\x1f", 3)
        found += [f"{commit[:12]} {problem}" for problem in problems(author, committer, message)]
    return len(records), found


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    count, found = violations(*argv[1:])
    for line in found:
        print(line)
    print(f"identity: {count} commits checked, {len(found)} violations")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
