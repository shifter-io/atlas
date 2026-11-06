#!/usr/bin/env python3
"""Retrieve a finished private region using one SSH stream, preserving its layout."""

import argparse
import json
import os
import shlex
import subprocess
import tarfile
from pathlib import Path

from proxy_index.onboarding.files import private_dir
from proxy_index.onboarding.manifest import load


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--region", required=True)
    return parser.parse_args()


def remote_root(server, manifest):
    return (
        Path(server["root"])
        / "onboarding-networks"
        / manifest["provider"]
        / (manifest["product"])
        / manifest["campaign_id"]
    )


def ssh_options(server):
    return [
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        os.path.expanduser(server["identity_file"]),
    ]


def remote_command(root, prefix):
    check = (
        "import json,sys;from pathlib import Path;"
        "s=json.loads(Path(sys.argv[1]).read_text());"
        "assert s['status'] in ('completed','closed','stopped','failed')"
    )
    return (
        shlex.join(["python3", "-c", check, str(root / prefix / "state.json")])
        + " && "
        + shlex.join(["tar", "-C", str(root), "-cf", "-", str(prefix)])
    )


def extract(archive, destination, prefix):
    with tarfile.open(archive) as source:
        for entry in source.getmembers():
            path = Path(entry.name)
            if (
                not path.is_relative_to(prefix)
                or ".." in path.parts
                or not (entry.isfile() or entry.isdir())
            ):
                raise ValueError("Unexpected archive entry")
        source.extractall(destination, filter="data")


def main():
    args = parse_args()
    manifest = load(args.manifest)
    if args.region not in manifest["regions"]:
        raise ValueError("Unknown region")
    server = json.loads(args.inventory.read_text())["servers"][args.region]
    root = remote_root(server, manifest)
    prefix = Path("regions") / args.region
    destination = args.manifest.parent
    if (destination / prefix).exists():
        raise ValueError("Local region already exists")
    options = ssh_options(server)
    target = server.get("user", "root") + "@" + server["host"]
    command = remote_command(root, prefix)
    private_dir(destination)
    archive = destination / f"{args.region}.tar.partial"
    with archive.open("xb") as stream:
        os.chmod(archive, 0o600)
        subprocess.run(["ssh", *options, target, command], stdout=stream, check=True)
    extract(archive, destination, prefix)
    archive.unlink()
    print(destination / prefix)


if __name__ == "__main__":
    main()
