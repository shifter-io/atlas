#!/usr/bin/env python3
"""Launch one explicitly selected remote probe; server inventory remains local."""

import argparse
import json
import os
import re
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path

MARKER_SCRIPT = """import json,os,pathlib,sys
root=pathlib.Path(sys.argv[1]); value=json.loads(sys.argv[2])
p=root/"active-run.json.partial"
with p.open("w") as f:
 os.chmod(p,0o600);json.dump(value,f);f.flush();os.fsync(f.fileno())
p.replace(root/"active-run.json")
"""


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--country", required=True)
    parser.add_argument("--network", required=True, help="provider/product")
    timing = parser.add_mutually_exclusive_group(required=True)
    timing.add_argument("--start", help="Future whole UTC hour")
    timing.add_argument("--now", action="store_true", help="Start this manual probe immediately")
    parser.add_argument("--concurrency", type=int, default=1000)
    parser.add_argument("--processes", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9_-]+/[a-z0-9_-]+", args.network):
        raise ValueError("Network must be provider/product")
    if not re.fullmatch(r"[a-z]{2}", args.country):
        raise ValueError("Country must be lowercase ISO alpha-2")
    return args


def ssh_options(server):
    return [
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        os.path.expanduser(server["identity_file"]),
    ]


def service(args, root, unit, output):
    """Return the systemd-run command that supervises the probe."""
    command = [
        "systemd-run",
        "--unit=" + unit,
        "--property=LimitNOFILE=65536",
        "--property=UMask=0077",
        "--property=Restart=no",
        "--property=RuntimeMaxSec=90000",
        "--property=WorkingDirectory=" + root,
        "--property=RemainAfterExit=yes",
        root + "/venv/bin/atlas",
        "onboarding",
        "probe",
        "--provider-config",
        root + "/configs/provider.toml",
        "--credentials-file",
        root + "/.secrets/credentials.toml",
        "--region",
        args.region,
        "--country",
        args.country,
        "--output",
        output,
        "--concurrency",
        str(args.concurrency),
        "--processes",
        str(args.processes),
        "--timeout",
        str(args.timeout),
    ]
    if args.start:
        command += ["--start", args.start]
    return command


def main():
    args = parse_args()
    start = datetime.fromisoformat(args.start) if args.start else datetime.now(UTC)
    name = f"{args.country}-{start.strftime('%Y%m%dt%H%M%Sz')}-c{args.concurrency}"
    server = json.loads(args.inventory.read_text())["servers"][args.region]
    root = server["root"]
    unit = f"proxy-index-onboarding-{name}"
    output = f"{root}/onboarding-networks/{args.network}/probes/{name}"
    marker = {
        "kind": "probe",
        "region": args.region,
        "unit": unit + ".service",
        "state_path": str(Path(output).relative_to(root) / "state.json"),
    }
    options = ssh_options(server)
    target = f"{server.get('user', 'root')}@{server['host']}"
    script = (
        "set -eu\numask 077\n"
        + shlex.join(service(args, root, unit, output))
        + "\n"
        + shlex.join(["python3", "-c", MARKER_SCRIPT, root, json.dumps(marker)])
        + "\n"
    )
    subprocess.run(["ssh", *options, target, "sh -s"], input=script, text=True, check=True)
    print(
        json.dumps(
            {
                "region": args.region,
                "unit": unit,
                "remote_output": output,
                "scheduled_at": args.start,
                "concurrency": args.concurrency,
                "processes": args.processes,
                "timeout_seconds": args.timeout,
            }
        )
    )


if __name__ == "__main__":
    main()
