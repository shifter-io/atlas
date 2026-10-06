#!/usr/bin/env python3
"""Distribute one frozen manifest and arm one explicitly selected region."""
import argparse
import json
import os
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from milf_framework.onboarding.manifest import load, moment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--calibration", action="store_true")
    args = parser.parse_args()
    manifest = load(args.manifest)
    if args.region not in manifest["regions"]:
        raise ValueError("Region absent from manifest")
    if not args.calibration and moment(manifest["starts_at"]) <= datetime.now(UTC):
        raise ValueError("Refusing to arm a campaign after its scheduled start")
    server = json.loads(args.inventory.read_text())["servers"][args.region]
    root = server["root"]
    target = server.get("user", "root") + "@" + server["host"]
    options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "IdentitiesOnly=yes",
               "-i", os.path.expanduser(server["identity_file"])]
    relative = (Path("onboarding-networks") / manifest["provider"] / manifest["product"] /
                manifest["campaign_id"])
    directory = str(Path(root) / relative)

    def remote(command):
        subprocess.run(["ssh", *options, target, command], check=True)

    remote("umask 077\nmkdir -p " + shlex.quote(directory))
    subprocess.run(["scp", *options, str(args.manifest),
                    target + ":" + shlex.quote(directory + "/campaign.json")], check=True)
    unit = "milf-onboarding-" + manifest["campaign_id"] + "-" + args.region
    base = [root + "/venv/bin/milf-framework", "onboarding", "run"]
    if args.calibration:
        driver = Path(__file__).with_name("onboarding-calibration.py")
        subprocess.run(["scp", *options, str(driver),
                        target + ":" + shlex.quote(root + "/tools/onboarding-calibration.py")],
                       check=True)
        base = [root + "/venv/bin/python", root + "/tools/onboarding-calibration.py"]
    command = ["systemd-run", "--unit=" + unit, "--property=LimitNOFILE=65536",
               "--property=UMask=0077", "--property=Restart=no",
               "--property=WorkingDirectory=" + root, "--property=RemainAfterExit=yes",
               "--property=RuntimeMaxSec=" + str(int(
                   moment(manifest["ends_at"]).timestamp() - datetime.now(UTC).timestamp() + 600)),
               *base, "--manifest", directory + "/campaign.json", "--region", args.region,
               "--provider-config", root + "/configs/provider.toml",
               "--credentials-file", root + "/.secrets/credentials.toml"]
    # Refuse to replace a live regional assignment, and verify the installed build
    # and configuration before any new service can issue traffic.
    check = '''import json,pathlib,subprocess,sys
from milf_framework import __version__
from milf_framework.onboarding.files import digest,atomic_json
from milf_framework.onboarding.manifest import load
root=pathlib.Path(sys.argv[1]); manifest=load(pathlib.Path(sys.argv[2]))
assert manifest['framework_version']==__version__, 'Framework version mismatch'
assert digest(root/'configs/provider.toml')==manifest['provider_config_sha256']
synced=subprocess.check_output(['timedatectl','show',
    '--property=NTPSynchronized','--value'],text=True).strip()
assert synced=='yes', 'Clock is not synchronized'
marker=root/'active-run.json'
if marker.exists():
 previous=json.loads(marker.read_text()); state=root/previous['state_path']
 terminal=('completed','closed','stopped','failed','cancelled')
 assert not state.exists() or json.loads(state.read_text())['status'] in terminal, \
     'A regional run is already active'
'''
    remote(shlex.join([root + "/venv/bin/python", "-c", check, root,
                       directory + "/campaign.json"]))
    remote(shlex.join(command))
    marker = {"kind": "calibration" if args.calibration else "campaign", "region": args.region,
              "unit": unit + ".service",
              "state_path": str(relative / "regions" / args.region / "state.json")}
    save = ("import json,sys;from pathlib import Path;"
            "from milf_framework.onboarding.files import atomic_json;"
            "atomic_json(Path(sys.argv[1]),json.loads(sys.argv[2]))")
    remote(shlex.join([root + "/venv/bin/python", "-c", save, root + "/active-run.json",
                       json.dumps(marker)]))
    print(json.dumps({"region": args.region, "unit": unit, "remote_directory": directory,
                      "manifest": str(args.manifest), "calibration": args.calibration}))


if __name__ == "__main__":
    main()
