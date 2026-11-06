#!/usr/bin/env python3
"""Save private per-probe telemetry from the local fleet monitor; no proxy traffic."""

import argparse
import json
import os
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch", type=Path, required=True)
    parser.add_argument("--monitor", type=Path, default=Path(".state/onboarding-monitor"))
    args = parser.parse_args()
    launch = json.loads(args.launch.read_text())
    unit = launch["unit"].removesuffix(".service") + ".service"
    output = args.monitor / (unit.removesuffix(".service") + "-samples.jsonl")
    with output.open("a") as stream:
        os.chmod(output, 0o600)
        for snapshot in snapshots(args.monitor / "snapshot.json"):
            node = next(n for n in snapshot["servers"] if n["region"] == launch["region"])
            if node.get("assignment", {}).get("unit") != unit:
                continue
            record(stream, snapshot, node)
            state = node.get("state", {}).get("status")
            if state in ("completed", "failed", "cancelled", "stopped"):
                return 0 if state == "completed" else 1


def snapshots(path):
    """Yield each new monitor snapshot, reading the file every 2 seconds."""
    previous = None
    while True:
        snapshot = json.loads(path.read_text())
        if snapshot["collected_at"] != previous:
            previous = snapshot["collected_at"]
            yield snapshot
        time.sleep(2)


def record(stream, snapshot, node):
    """Append the node's sample to the private file and print a one-line summary."""
    sample = {
        "collected_at": snapshot["collected_at"],
        "collected_epoch": snapshot["collected_epoch"],
        "node": node,
    }
    stream.write(json.dumps(sample) + "\n")
    stream.flush()
    print(
        json.dumps(
            {
                "at": snapshot["collected_at"],
                "health": node["health"],
                "elapsed": node.get("elapsed_seconds"),
                "requests": node.get("requests"),
                "unique": node.get("unique_ips"),
                "errors": node.get("errors"),
                "rate": node.get("requests_per_second"),
                "cores": node.get("core_cpu_pct"),
                "workers": node.get("processes"),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
