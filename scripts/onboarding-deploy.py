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


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("inventory", "wheel", "requirements", "provider-config", "credentials-file"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--region", required=True)
    return parser.parse_args()


def ssh_options(server):
    return [
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


def remote(options, target, script):
    subprocess.run(["ssh", *options, target, "sh -s"], input=script, text=True, check=True)


def prepare_script(quoted):
    return f"""set -eu
umask 077
mkdir -p {quoted}/releases {quoted}/.secrets {quoted}/configs {quoted}/tools
chmod 700 {quoted} {quoted}/.secrets
if [ ! -x {quoted}/tools/uv ]; then
 curl -LsSf https://astral.sh/uv/install.sh -o {quoted}/tools/install-uv.sh
 UV_UNMANAGED_INSTALL={quoted}/tools sh {quoted}/tools/install-uv.sh
fi
"""


def upload(args, options, target, root):
    for source, destination in (
        (args.wheel, root + "/releases/" + args.wheel.name),
        (args.requirements, root + "/releases/requirements.txt"),
        (args.provider_config, root + "/configs/provider.toml"),
        (args.credentials_file, root + "/.secrets/credentials.toml"),
    ):
        subprocess.run(
            ["scp", *options, str(source), target + ":" + shlex.quote(destination)], check=True
        )


def install_script(quoted, checksum, wheel):
    return f"""set -eu
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
{quoted}/venv/bin/atlas onboarding probe --help
"""


def main():
    args = parse_args()
    server = json.loads(args.inventory.read_text())["servers"][args.region]
    root = server["root"]
    target = f"{server.get('user', 'root')}@{server['host']}"
    options = ssh_options(server)
    quoted = shlex.quote(root)
    remote(options, target, prepare_script(quoted))
    upload(args, options, target, root)
    checksum = hashlib.sha256(args.wheel.read_bytes()).hexdigest()
    wheel = shlex.quote(root + "/releases/" + args.wheel.name)
    remote(options, target, install_script(quoted, checksum, wheel))
    print(json.dumps({"region": args.region, "wheel_sha256": checksum, "deployed": True}))


if __name__ == "__main__":
    main()
