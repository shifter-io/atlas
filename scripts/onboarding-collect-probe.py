#!/usr/bin/env python3
"""Retrieve one completed probe and independently verify its observation artifacts."""

import argparse
import gzip
import json
import os
import shlex
import subprocess
from pathlib import Path, PurePosixPath

from milf_framework.onboarding.export import verify_chunk
from milf_framework.onboarding.files import atomic_json, private_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--launch", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("onboarding-networks"))
    args = parser.parse_args()
    launch = json.loads(args.launch.read_text())
    server = json.loads(args.inventory.read_text())["servers"][launch["region"]]
    remote = PurePosixPath(launch["remote_output"])
    base = PurePosixPath(server["root"]) / "onboarding-networks"
    relative = remote.relative_to(base)
    if ".." in relative.parts:
        raise ValueError("Invalid probe directory")
    destination = args.output_root / str(relative)
    if destination.exists():
        raise ValueError("Local probe directory already exists")
    private_dir(destination.parent)
    options = [
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        os.path.expanduser(server["identity_file"]),
    ]
    target = f"{server.get('user', 'root')}@{server['host']}"
    subprocess.run(
        ["scp", "-pr", *options, target + ":" + shlex.quote(str(remote)), str(destination.parent)],
        check=True,
    )
    summary = json.loads((destination / "summary.json").read_text())
    job = {
        "country": summary["country"],
        "scheduled_at": summary["scheduled_at"],
        "started_at": summary["started_at"],
    }
    rows = 0
    unique = set()
    chunks = 0
    for metadata_path in sorted((destination / "observations").glob("*.jsonl.meta.json")):
        meta = json.loads(metadata_path.read_text())
        name = meta["file"]
        if Path(name).name != name:
            raise ValueError("Invalid chunk filename")
        chunk = metadata_path.parent / name
        if chunk.with_suffix(".meta.json") != metadata_path:
            raise ValueError("Invalid chunk sidecar")
        rows += verify_chunk(chunk, meta, job, summary["ends_at"])
        with gzip.open(chunk, "rt") as stream:
            for line in stream:
                unique.add(json.loads(line)["ip"])
        chunks += 1
    measurement = summary["measurement"]
    if rows != summary["observations"] or rows != measurement["successes"]:
        raise ValueError("Observation count does not reconcile")
    if len(unique) != measurement["unique_ips"]:
        raise ValueError("Unique IP count does not reconcile")
    result = {
        "verified": True,
        "chunks": chunks,
        "observations": rows,
        "unique_ips": len(unique),
        "directory": str(destination),
        "partial_files": len(list(destination.rglob("*.partial"))),
    }
    atomic_json(destination / "verification.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
