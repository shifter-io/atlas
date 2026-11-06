#!/usr/bin/env python3
"""Distribute one frozen manifest and arm one explicitly selected region."""

import argparse
import json
import os
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from proxy_index.onboarding.manifest import load, moment, regional_window

# Refuse to replace a live regional assignment, and verify the installed build
# and configuration before any new service can issue traffic.
CHECK = """import json,pathlib,subprocess,sys
from proxy_index import __version__
from proxy_index.onboarding.files import digest,atomic_json
from proxy_index.onboarding.manifest import load
root=pathlib.Path(sys.argv[1]); manifest=load(pathlib.Path(sys.argv[2]))
assert manifest['framework_version']==__version__, 'Framework version mismatch'
assert digest(root/'configs/provider.toml')==manifest['provider_config_sha256']
synced=subprocess.check_output(['timedatectl','show',
    '--property=NTPSynchronized','--value'],text=True).strip()
assert synced=='yes', 'Clock is not synchronized'
marker=root/'active-run.json'
if marker.exists():
 previous=json.loads(marker.read_text()); state=root/previous['state_path']
 pid=subprocess.check_output(['systemctl','show',previous['unit'],'--property=MainPID',
                              '--value'],text=True).strip()
 assert pid in ('','0'), 'Previous assignment still has a live process'
 terminal=('completed','closed','stopped','failed','cancelled')
 assert not state.exists() or json.loads(state.read_text())['status'] in terminal, \
     'A regional run is already active'
"""

SAVE = (
    "import json,sys;from pathlib import Path;"
    "from proxy_index.onboarding.files import atomic_json;"
    "atomic_json(Path(sys.argv[1]),json.loads(sys.argv[2]))"
)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--calibration", action="store_true")
    return parser.parse_args()


def load_campaign(args):
    """Return the manifest and the region's window, refusing a campaign already due."""
    manifest = load(args.manifest)
    if args.region not in manifest["regions"]:
        raise ValueError("Region absent from manifest")
    window = regional_window(manifest, args.region)
    if not args.calibration and moment(window["starts_at"]) <= datetime.now(UTC):
        raise ValueError("Refusing to arm a campaign after its scheduled start")
    return manifest, window


def connect(server):
    """Return the ssh target and the options shared by ssh and scp."""
    target = server.get("user", "root") + "@" + server["host"]
    options = [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        os.path.expanduser(server["identity_file"]),
    ]
    return target, options


def remote(connection, command):
    target, options = connection
    subprocess.run(["ssh", *options, target, command], check=True)


def copy(connection, source, destination):
    target, options = connection
    subprocess.run(["scp", *options, source, target + ":" + shlex.quote(destination)], check=True)


def distribute(connection, args, root, directory):
    """Create the campaign directory and copy the manifest and any calibration driver."""
    remote(connection, "umask 077\nmkdir -p " + shlex.quote(directory))
    copy(connection, str(args.manifest), directory + "/campaign.json")
    if args.calibration:
        driver = Path(__file__).with_name("onboarding-calibration.py")
        copy(connection, str(driver), root + "/tools/onboarding-calibration.py")


def program(args, root, directory):
    """Return the regional run, or the calibration driver, with its arguments."""
    base = [root + "/venv/bin/proxy-index", "onboarding", "run"]
    if args.calibration:
        base = [root + "/venv/bin/python", root + "/tools/onboarding-calibration.py"]
    return [
        *base,
        "--manifest",
        directory + "/campaign.json",
        "--region",
        args.region,
        "--provider-config",
        root + "/configs/provider.toml",
        "--credentials-file",
        root + "/.secrets/credentials.toml",
    ]


def service(unit, root, window, arguments):
    """Return the systemd-run command that supervises the run until its window ends."""
    return [
        "systemd-run",
        "--unit=" + unit,
        "--property=LimitNOFILE=65536",
        "--property=UMask=0077",
        "--property=Restart=no",
        "--property=WorkingDirectory=" + root,
        "--property=RemainAfterExit=yes",
        "--property=RuntimeMaxSec="
        + str(int(moment(window["ends_at"]).timestamp() - datetime.now(UTC).timestamp() + 600)),
        *arguments,
    ]


def arm(connection, root, directory, command, marker):
    """Check the server, start the service and record the active-run pointer."""
    remote(
        connection,
        shlex.join([root + "/venv/bin/python", "-c", CHECK, root, directory + "/campaign.json"]),
    )
    remote(connection, shlex.join(command))
    remote(
        connection,
        shlex.join(
            [root + "/venv/bin/python", "-c", SAVE, root + "/active-run.json", json.dumps(marker)]
        ),
    )


def main():
    args = parse_args()
    manifest, window = load_campaign(args)
    server = json.loads(args.inventory.read_text())["servers"][args.region]
    root = server["root"]
    connection = connect(server)
    relative = (
        Path("onboarding-networks")
        / manifest["provider"]
        / manifest["product"]
        / manifest["campaign_id"]
    )
    directory = str(Path(root) / relative)
    distribute(connection, args, root, directory)
    unit = "proxy-index-onboarding-" + manifest["campaign_id"] + "-" + args.region
    command = service(unit, root, window, program(args, root, directory))
    marker = {
        "kind": "calibration" if args.calibration else "campaign",
        "region": args.region,
        "unit": unit + ".service",
        "state_path": str(relative / "regions" / args.region / "state.json"),
    }
    arm(connection, root, directory, command, marker)
    print(
        json.dumps(
            {
                "region": args.region,
                "unit": unit,
                "remote_directory": directory,
                "manifest": str(args.manifest),
                "calibration": args.calibration,
            }
        )
    )


if __name__ == "__main__":
    main()
