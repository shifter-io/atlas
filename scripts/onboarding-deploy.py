#!/usr/bin/env python3
"""Deploy an explicitly selected onboarding host from a private local inventory.

Build the wheel and export locked requirements locally first. This script transfers
only those artifacts, the selected provider config and its private credential file.
The inventory stays local. It never launches proxy traffic.
"""

import argparse
import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("inventory", "wheel", "requirements", "provider-config", "credentials-file"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args()
    server = json.loads(args.inventory.read_text())["servers"][args.region]
    root = server["root"]
    target = f"{server.get('user', 'root')}@{server['host']}"
    options = [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        os.path.expanduser(server["identity_file"]),
    ]

    def remote(script):
        subprocess.run(["ssh", *options, target, "sh -s"], input=script, text=True, check=True)

    quoted = shlex.quote(root)
    remote(f"""set -eu
umask 077
mkdir -p {quoted}/releases {quoted}/.secrets {quoted}/configs {quoted}/tools
chmod 700 {quoted} {quoted}/.secrets
if [ ! -x {quoted}/tools/uv ]; then
 curl -LsSf https://astral.sh/uv/install.sh -o {quoted}/tools/install-uv.sh
 UV_UNMANAGED_INSTALL={quoted}/tools sh {quoted}/tools/install-uv.sh
fi
""")
    for source, destination in (
        (args.wheel, root + "/releases/" + args.wheel.name),
        (args.requirements, root + "/releases/requirements.txt"),
        (args.provider_config, root + "/configs/provider.toml"),
        (args.credentials_file, root + "/.secrets/credentials.toml"),
    ):
        subprocess.run(
            ["scp", *options, str(source), target + ":" + shlex.quote(destination)], check=True
        )
    checksum = hashlib.sha256(args.wheel.read_bytes()).hexdigest()
    wheel = shlex.quote(root + "/releases/" + args.wheel.name)
    remote(f"""set -eu
umask 077
chmod 600 {quoted}/.secrets/credentials.toml
export UV_CACHE_DIR={quoted}/.cache
export UV_PYTHON_INSTALL_DIR={quoted}/python
printf '%s  %s\\n' {shlex.quote(checksum)} {wheel} | sha256sum -c -
if [ ! -x {quoted}/venv/bin/python ]; then
 {quoted}/tools/uv venv --python 3.12 {quoted}/venv
fi
{quoted}/tools/uv pip install --python {quoted}/venv/bin/python --require-hashes \
 -r {quoted}/releases/requirements.txt
{quoted}/tools/uv pip install --python {quoted}/venv/bin/python --no-deps --reinstall {wheel}
{quoted}/venv/bin/proxy-index onboarding probe --help
""")
    print(json.dumps({"region": args.region, "wheel_sha256": checksum, "deployed": True}))


if __name__ == "__main__":
    main()
