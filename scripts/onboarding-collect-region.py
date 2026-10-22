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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args()
    manifest = load(args.manifest)
    if args.region not in manifest["regions"]:
        raise ValueError("Unknown region")
    server = json.loads(args.inventory.read_text())["servers"][args.region]
    root = Path(server["root"]) / "onboarding-networks" / manifest["provider"] / (
        manifest["product"]) / manifest["campaign_id"]
    prefix = Path("regions") / args.region
    destination = args.manifest.parent
    if (destination / prefix).exists():
        raise ValueError("Local region already exists")
    options = ["-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-i",
               os.path.expanduser(server["identity_file"])]
    target = server.get("user", "root") + "@" + server["host"]
    check = ("import json,sys;from pathlib import Path;"
             "s=json.loads(Path(sys.argv[1]).read_text());"
             "assert s['status'] in ('completed','closed','stopped','failed')")
    command = (shlex.join(["python3", "-c", check, str(root / prefix / "state.json")])
               + " && " + shlex.join(["tar", "-C", str(root), "-cf", "-", str(prefix)]))
    private_dir(destination)
    archive = destination / f"{args.region}.tar.partial"
    with archive.open("xb") as stream:
        os.chmod(archive, 0o600)
        subprocess.run(["ssh", *options, target, command], stdout=stream, check=True)
    with tarfile.open(archive) as source:
        for entry in source.getmembers():
            path = Path(entry.name)
            if (not path.is_relative_to(prefix) or ".." in path.parts
                    or not (entry.isfile() or entry.isdir())):
                raise ValueError("Unexpected archive entry")
        source.extractall(destination, filter="data")
    archive.unlink()
    print(destination / prefix)


if __name__ == "__main__":
    main()
