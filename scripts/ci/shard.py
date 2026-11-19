"""PUBLIC-CI-1: one shard of the Python test files, for the public run on GitHub's runners.

Usage: shard.py <index> <count>   (index 1..count; prints the shard's test files, one per line)

Each file weighs the seconds tests/durations.json records for its tests (at least DEFAULT, for a
file it does not list); the heaviest file goes first to the lightest shard, ties by name, so every
run splits the same files the same way and every file is in exactly one shard. Exit 2 on bad usage.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = 1.0


def weights(root: Path = ROOT) -> dict[str, float]:
    files = {path.relative_to(root).as_posix(): 0.0 for path in (root / "tests").glob("test_*.py")}
    durations = root / "tests" / "durations.json"
    recorded = json.loads(durations.read_text()) if durations.is_file() else {}
    for node, seconds in recorded.items():
        name = node.split("::", 1)[0]
        if name in files:
            files[name] += float(seconds)
    return {name: max(seconds, DEFAULT) for name, seconds in files.items()}


def shards(files: dict[str, float], count: int) -> list[list[str]]:
    bins: list[tuple[float, int, list[str]]] = [(0.0, i, []) for i in range(count)]
    for name in sorted(files, key=lambda n: (-files[n], n)):
        total, index, names = min(bins)
        names.append(name)
        bins[index] = (total + files[name], index, names)
    return [sorted(names) for _, _, names in bins]


def main(argv: list[str]) -> int:
    try:
        index, count = int(argv[1]), int(argv[2])
    except (IndexError, ValueError):
        index = count = 0
    if len(argv) != 3 or not 1 <= index <= count:
        print(__doc__, file=sys.stderr)
        return 2
    print("\n".join(shards(weights(), count)[index - 1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
