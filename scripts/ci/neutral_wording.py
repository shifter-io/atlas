"""Neutral wording in public text (NEUTRAL-WORDING-1): no network singled out by a ranking effect.

Usage: neutral_wording.py <repo> <base> <head> [--body-file FILE]

Reads every commit message in BASE..HEAD and, with --body-file, a pull request body. A sentence
fails when a capitalised network name (the `network` of each formulas/*.json, also with its hyphen
as a space or left out) stands within WINDOW words of a ranking-effect word (rank, rises, gains,
benefits, moves up, overtakes, beats and their forms; not "ranking", a method word). Public text
describes a ranking or scoring change as a method change for every network (AGENTS.md, "Public
repository rules"). Prints where each match is (the commit or "PR body", the network and the
word), never the sentence. Exit 0 when nothing matches, 1 otherwise.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

WINDOW = 6
EFFECT = re.compile(
    r"^(rank|ranks|ranked|rises?|rose|risen|rising|gains?|gained|gaining|benefits?|benefited|"
    r"benefitted|benefiting|benefitting|overtakes?|overtook|overtaken|overtaking|beats?|beaten|"
    r"beating|climbs?|climbed|climbing|jumps?|jumped|jumping|wins?|won|winning)$",
    re.IGNORECASE,
)
MOVES_UP = re.compile(r"^(moves?|moved|moving)$", re.IGNORECASE)
WORD = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n\s*\n|\n\s*[-*|]")


def networks(repo: Path) -> dict[str, str]:
    """{spelling (lowercase): network} for each formulas/*.json, with and without its hyphens."""
    names = {}
    for path in sorted((repo / "formulas").glob("*.json")):
        net = json.loads(path.read_text(encoding="utf-8")).get("network")
        if isinstance(net, str) and net:
            for spelling in {net, net.replace("-", ""), net.replace("-", " ")}:
                names[spelling.lower()] = net
    return names


def findings(text: str, names: dict[str, str]) -> list[tuple[str, str]]:
    """(network, effect word) for each capitalised network name near a ranking-effect word."""
    out = []
    for sentence in SENTENCE.split(text):
        words = WORD.findall(sentence)
        effects = [
            i
            for i, w in enumerate(words)
            if EFFECT.match(w)
            or (MOVES_UP.match(w) and i + 1 < len(words) and words[i + 1].lower() == "up")
        ]
        for i, word in enumerate(words):
            pair = f"{word} {words[i + 1]}" if i + 1 < len(words) else ""
            name = next((names[s.lower()] for s in (pair, word) if s and s.lower() in names), None)
            if name is None or not word[0].isupper():
                continue
            near = [words[j] for j in effects if abs(j - i) <= WINDOW]
            if near:
                out.append((name, near[0]))
    return sorted(set(out))


def commit_messages(repo: Path, base: str, head: str) -> list[tuple[str, str]]:
    cmd = ["git", "-C", str(repo), "log", "-z", "--format=%H%x1f%B", f"{base}..{head}"]
    raw = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return [tuple(entry.split("\x1f", 1)) for entry in raw.split("\0") if "\x1f" in entry]


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 5) or (len(argv) == 5 and argv[3] != "--body-file"):
        print(__doc__.splitlines()[2], file=sys.stderr)
        return 2
    repo, base, head = Path(argv[0]), argv[1], argv[2]
    names = networks(repo)
    sources = [(f"commit {sha[:12]}", msg) for sha, msg in commit_messages(repo, base, head)]
    if len(argv) == 5:
        sources.append(("PR body", Path(argv[4]).read_text(encoding="utf-8")))
    bad = [(where, net, word) for where, text in sources for net, word in findings(text, names)]
    for where, net, word in bad:
        print(
            f"neutral wording: {where}: the network {net!r} next to {word!r}; describe the method "
            "change for every network instead (AGENTS.md, Public repository rules)"
        )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
