"""CALC-SCALE-3 (#1307): the input fingerprint's runs hashed with each run's whole scope (before)
and with each shared scope hashed once (fingerprints._scoped(), now), at the sizes of a 30-day
window: --runs runs (production: about 5 M in 30 days) sharing --scopes scopes of about 4 KB of
configuration each, as incremental._interned() shares them. Prints one JSON line per size:
    python tests/fingerprint_measure.py [--runs 150000 1500000] [--scopes 4000]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from atlas.calculated import fingerprints  # noqa: E402

AT = datetime(2026, 12, 1, tzinfo=UTC)


def runs(n, scopes):
    shared = [
        {
            "config": {f"setting_{k}": "x" * 40 for k in range(80)} | {"requested_country": "us"},
            "target": {"id": "generic_http", "url": f"https://example.test/{s}", "version": 1},
        }
        for s in range(scopes)
    ]
    for k in range(n):
        yield {
            "run_key": k,
            "run_id": f"{k:032x}",
            "scope_id": k % scopes,
            "scope": shared[k % scopes],
            "started_at": AT + timedelta(seconds=k),
            "product_id": k % 18,
            "provider": "example",
            "batch_id": k // 50,
            "counted": True,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, nargs="+", default=[150_000, 1_500_000])
    parser.add_argument("--scopes", type=int, default=4000)
    args = parser.parse_args()
    for n in args.runs:
        began = time.perf_counter()
        fingerprints.items(runs(n, args.scopes))
        before = time.perf_counter() - began
        began = time.perf_counter()
        fingerprints.items(fingerprints._scoped(runs(n, args.scopes)))
        now = time.perf_counter() - began
        found = {"runs": n, "before_seconds": round(before, 1), "now_seconds": round(now, 1)}
        print(json.dumps(found))


if __name__ == "__main__":
    main()
