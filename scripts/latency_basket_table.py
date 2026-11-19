"""The LATENCY-BASKET-1 before/after table (#1385) of a build's networks, read only.

Each network window of a build under [latency] keeps the pooled latency it replaced
(metrics.latency_basket.pooled_ms), so one recount gives both figures: per window (24h, 7d, 30d)
and network, the pooled p50 (before) and the basket p50 (after), the basket coverage, the Speed
both ways (100 x the fastest of the kind / own, over the networks of the kind with a value) and
the balanced Atlas Score's change that Speed alone makes (its weight x the Speed change / 100).

  python scripts/latency_basket_table.py --database-file <credentials.toml>  # the current build
  python scripts/latency_basket_table.py --payload <snapshot.json>          # a saved payload
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

CURRENT = (
    "SELECT s.payload FROM calculated_snapshots s JOIN current_calculation USING(build_id) "
    "WHERE s.payload ? 'networks' LIMIT 1"
)
TARGET = "generic_http"
COLUMNS = (
    "Window | Network | Kind | Before p50 | After p50 | Covered | Unserved | Speed before | "
    "Speed after | Score change"
)


def speeds(values):
    """{key: 100 x the fastest value / own} of values ({key: p50 or None}), None without one."""
    best = min((v for v in values.values() if v), default=None)
    return {k: round(100 * best / v, 1) if v and best else None for k, v in values.items()}


def table(networks, weight):
    """The table's rows ([window, name, kind, before, after, covered, unserved, speed before,
    speed after, score change]) of a payload's networks, by window, kind and after."""
    found = defaultdict(dict)
    for n in networks:
        for label, w in n["windows"].items():
            m = w["targets"].get(TARGET, {}).get("metrics", {})
            if "latency_basket" in m:
                found[label, n["kind"]][n["name"]] = (m["latency_basket"], m["latency_ms"]["p50"])
    rows = []
    for (label, kind), members in sorted(found.items()):
        before = speeds({k: b["pooled_ms"]["p50"] for k, (b, _) in members.items()})
        after = speeds({k: p50 for k, (_, p50) in members.items()})
        for name, (basket, p50) in members.items():
            old, new = before[name], after[name]
            change = round(weight * (new - old) / 100, 1) if old is not None and new else None
            covered = f"{basket['covered']} / {basket['size']}"
            unserved = " ".join(basket["unserved"])
            rows.append(
                [label, name, kind, basket["pooled_ms"]["p50"], p50, covered, unserved]
                + [old, new, change]
            )
    rows.sort(key=lambda r: (r[0], r[2], r[4] is None, r[4] or 0))
    return rows


def markdown(rows):
    lines = [f"| {COLUMNS} |", "|" + "---|" * len(COLUMNS.split("|"))]
    lines += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--database-file", type=Path)
    source.add_argument("--payload", type=Path)
    args = parser.parse_args()
    if args.payload:
        payload = json.loads(args.payload.read_text())
    else:
        from atlas.storage_read import connect

        with connect(args.database_file) as conn:
            conn.read_only = True
            payload = conn.execute(CURRENT).fetchone()[0]
    weight = payload["methodology"]["settings"]["profiles"]["balanced"]["speed"]
    total = sum(payload["methodology"]["settings"]["profiles"]["balanced"].values())
    print(markdown(table(payload["networks"], 100 * weight / total)))


if __name__ == "__main__":
    main()
