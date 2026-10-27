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
    previous = None
    with output.open("a") as stream:
        os.chmod(output, 0o600)
        while True:
            snapshot = json.loads((args.monitor / "snapshot.json").read_text())
            if snapshot["collected_at"] != previous:
                previous = snapshot["collected_at"]
                node = next(n for n in snapshot["servers"] if n["region"] == launch["region"])
                if node.get("assignment", {}).get("unit") == unit:
                    sample = {
                        "collected_at": previous,
                        "collected_epoch": snapshot["collected_epoch"],
                        "node": node,
                    }
                    stream.write(json.dumps(sample) + "\n")
                    stream.flush()
                    print(
                        json.dumps(
                            {
                                "at": previous,
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
                    state = node.get("state", {}).get("status")
                    if state in ("completed", "failed", "cancelled", "stopped"):
                        return 0 if state == "completed" else 1
            time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
