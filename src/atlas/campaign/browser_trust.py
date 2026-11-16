"""SEC-MINI-KEYS-1: the manager trusts the browser CA on the operator's Mac and holds no CA key.

`campaign manager browser-trust --ca-dir DIR --inventory INV [--keep-old-ca] [--apply]`
installs the CA certificate and revocation list made on the Mac (`browser_ca`) on the manager:
the browser port's client CA bundle (manager_orch.BROWSER_CA) and its list (BROWSER_CRL), both
owner-only. With --keep-old-ca the bundle also keeps the CA the manager made before
(BROWSER-MTLS-1, saved as RETIRED_CA while its key is still there), so the hosts not yet
switched to a key of their own still connect. That CA cannot sign a revocation list, so the list
file stays empty and nothing is revoked until a run without the flag, once every host has its
new certificate: then the bundle is the Mac's CA alone and the list its list.

Before the change the current files are kept as `.prev`. A running orchestrator is restarted and
must answer /health again: then, and only then, the CA key the manager made is deleted. If it
does not answer, the `.prev` files come back, the orchestrator is restarted on them and the key
stays: a refusal (`rolled-back`). RETIRED_CA is never deleted, so --keep-old-ca can undo a run
without it. The inventory records `browser_trust`: both SHA-256, `old_ca` and when.

Revoking a host: `browser-revoke` on the Mac, then this again to install the new list.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import browser_ca, deploy, host_layout, manager_daily, manager_orch
from .image import Shell, run_shell

ACTIONS = ("browser-trust", *browser_ca.ACTIONS)
RETIRED_CA = f"{manager_orch.TLS_DIR}/browser-ca-retired.crt"
WORK = "/root/atlas-trust-XXXXXX"  # mktemp's template, root-only
HEALTH_TRIES = 30  # two seconds apart


def _bundle_lines(keep_old: bool) -> list[str]:
    """Stage the bundle and the list: the Mac's CA (and the retired one, with no list)."""
    ca, key, old = manager_orch.BROWSER_CA, manager_orch.BROWSER_CA_KEY, RETIRED_CA
    if not keep_old:
        return ['cp "$stage/ca.crt" "$stage/bundle"', 'cp "$stage/ca.crl" "$stage/crl"']
    return [
        f"if [ -s {key} ] && [ -s {ca} ]; then cp {ca} {old}; fi",  # the CA this manager made
        f"test -s {old}",
        f'cat "$stage/ca.crt" {old} > "$stage/bundle"',
        ': > "$stage/crl"',
    ]


def trust_script(host: str, port: int, keep_old: bool) -> str:
    """Root shell on the manager with a tar (ca.crt, ca.crl) on stdin: install the bundle and the
    list, restart a running orchestrator and check /health, then remove the old CA key; undo
    and print `rolled-back` if the orchestrator does not come back."""
    files = {"bundle": manager_orch.BROWSER_CA, "crl": manager_orch.BROWSER_CRL}
    unit, curl = manager_orch.ORCHESTRATOR_UNIT, manager_orch.health_curl(host, port)
    keep, put, back = [], [], []
    for staged, f in files.items():
        keep.append(f"if [ -e {f} ]; then cp -p {f} {f}.prev; else rm -f {f}.prev; fi")
        put.append(f'install -m 600 "$stage/{staged}" {f}.partial && mv -f {f}.partial {f}')
        back.append(f"if [ -e {f}.prev ]; then mv -f {f}.prev {f}; else rm -f {f}; fi")
    return "\n".join(
        [
            "set -eu",
            "umask 077",
            f"stage=$(mktemp -d {WORK})",
            "trap 'rm -rf \"$stage\"' EXIT",
            'tar -x --no-same-owner -C "$stage"',
            'test -s "$stage/ca.crt"',
            'test -s "$stage/ca.crl"',
            f"install -d -m 700 {manager_orch.TLS_DIR}",
            *_bundle_lines(keep_old),
            *keep,
            *put,
            f"if systemctl is-active --quiet {unit}; then",
            f"  systemctl restart {unit}",
            "  up=no",
            f"  for i in $(seq {HEALTH_TRIES}); do",
            f"    if {curl} > /dev/null 2>&1; then up=yes; break; fi; sleep 2",
            "  done",
            '  if [ "$up" = no ]; then',
            *(f"    {line}" for line in back),
            f"    systemctl restart {unit} || true",
            "    echo rolled-back",
            "    exit 4",
            "  fi",
            "fi",
            f"rm -f {manager_orch.BROWSER_CA_KEY}",  # the key leaves the server
            "echo trusted",
        ]
    )


async def trust(
    ca_dir: Path,
    inventory_path: Path,
    *,
    keep_old: bool = False,
    apply: bool = False,
    shell: Shell = run_shell,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Install the CA of `ca_dir` on the recorded manager (a dry run without `apply`)."""
    inventory = manager_daily.recorded(inventory_path)
    cert, crl = await asyncio.to_thread(browser_ca.published, ca_dir)
    record = {
        "ca_sha256": hashlib.sha256(cert).hexdigest(),
        "crl_sha256": hashlib.sha256(crl).hexdigest(),
        "old_ca": keep_old,
    }
    unit = host_layout.script(inventory, manager_orch.ORCHESTRATOR_UNIT)
    result = {"applied": apply, **record, "restarts": unit, "revocation_checked": not keep_old}
    if not apply:
        return result
    port = manager_orch.listen_ports(host_layout.of(inventory))["node_api_port"]
    remote = deploy.Manager(inventory, inventory_path, shell)
    script = host_layout.script(inventory, trust_script(inventory["host"], port, keep_old))
    stdin = deploy._tar([("ca.crt", cert, 0o600), ("ca.crl", crl, 0o600)])
    code, out = await remote.shell(remote.argv(script), stdin)
    lines = out.strip().splitlines()
    if "rolled-back" in lines:
        raise deploy.DeployError(
            "the orchestrator did not answer with the new CA: the previous files are back"
        )
    if code != 0 or lines[-1:] != ["trusted"]:
        raise deploy.DeployError(f"manager step failed: browser-trust (exit {code})")
    stamp = (now or datetime.now(UTC)).isoformat()
    deploy.save(inventory_path, inventory, browser_trust=record | {"at": stamp})
    return result


async def run(args) -> dict[str, Any]:
    """One of ACTIONS: `browser-trust` over SSH, the CA's steps on this Mac."""
    if args.manager_action == "browser-trust":
        return await trust(args.ca_dir, args.inventory, keep_old=args.keep_old_ca, apply=args.apply)
    return await browser_ca.run(args)


def add_parser(steps) -> None:
    """`campaign manager browser-trust`, and the CA's steps (`browser_ca`)."""
    browser_ca.add_parsers(steps)
    step = steps.add_parser(
        "browser-trust", help="Install the Mac's browser CA and its revocation list (dry run)"
    )
    step.add_argument("--ca-dir", type=Path, required=True, help="the CA's folder on this Mac")
    step.add_argument(
        "--keep-old-ca",
        action="store_true",
        help="also trust the CA the manager made, until every host has its new certificate",
    )
    step.add_argument("--apply", action="store_true", help="act; without it, only print")
    step.add_argument("--inventory", type=Path, required=True, help="the atlas account's inventory")
