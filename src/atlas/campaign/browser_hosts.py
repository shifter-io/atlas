"""BROWSER-MTLS-1: a browser host's client certificate for the manager's browser port.

The home browser hosts have changing public addresses, so the manager's firewall opens the
browser port (manager_rules.BROWSER_PORT) to every address and the orchestrator admits only a
client certificate signed by the manager's browser CA whose CN is the host its token is bound
to. `campaign manager browser-cert --host browser-<n> --out <dir>` makes, over SSH on the
manager, a new key and a certificate for that host (clientAuth, valid CERT_DAYS days, signed by
the CA `orchestrator-enable` made) and writes both into <dir> with mode 0600. The key is never
printed and nothing stays on the manager. Rotating means issuing again; a compromised host means
`orchestrator-enable --new-browser-ca`, then issuing every host again.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import Any

from ..browser.contracts import HOST_NAME_PATTERN
from . import deploy, host_layout, manager_daily, manager_orch
from .image import Shell, run_shell

CERT_DAYS = 90
WORK = "/root/atlas-bcert-XXXXXX"  # mktemp's template, root-only
KEY_RE = re.compile(
    r"-----BEGIN (?:EC )?PRIVATE KEY-----\n[A-Za-z0-9+/=\n]+-----END (?:EC )?PRIVATE KEY-----\n"
)
CERT_RE = re.compile(r"-----BEGIN CERTIFICATE-----\n[A-Za-z0-9+/=\n]+-----END CERTIFICATE-----\n")
EXTENSIONS = (
    "basicConstraints=critical,CA:FALSE\\nkeyUsage=critical,digitalSignature\\n"
    "extendedKeyUsage=clientAuth\\nsubjectKeyIdentifier=hash\\nauthorityKeyIdentifier=keyid\\n"
)


def issue_script(host: str) -> str:
    """Root shell on the manager: a key and a CA-signed client certificate for `host` in a
    temporary root-only directory, printed (key, then certificate) and removed."""
    work, ca, ca_key = '"$work"', manager_orch.BROWSER_CA, manager_orch.BROWSER_CA_KEY
    return "\n".join(
        [
            "set -eu",
            f"test -s {ca}",
            f"test -s {ca_key}",
            "umask 077",
            f"work=$(mktemp -d {WORK})",
            f"trap 'rm -rf {work}' EXIT",
            f"printf '{EXTENSIONS}' > {work}/ext",
            "openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes "
            f"-subj /CN={host} -keyout {work}/key -out {work}/csr 2>/dev/null",
            f"openssl x509 -req -in {work}/csr -CA {ca} -CAkey {ca_key} "
            f'-set_serial "0x$(openssl rand -hex 16)" -days {CERT_DAYS} -sha256 '
            f"-extfile {work}/ext -out {work}/crt 2>/dev/null",
            f"cat {work}/key {work}/crt",
        ]
    )


def _pair(out: str) -> tuple[str, str]:
    """The one key and one certificate the script printed; refused otherwise (naming neither)."""
    keys, certs = KEY_RE.findall(out), CERT_RE.findall(out)
    if len(keys) != 1 or len(certs) != 1:
        raise deploy.DeployError("the manager did not return one key and one certificate")
    return keys[0], certs[0]


def _write_private(path: Path, data: str) -> None:
    """Replace `path` with `data`, owner-only (0600) from the start."""
    partial = path.with_name(f"{path.name}.partial")
    partial.unlink(missing_ok=True)
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(data)
    os.replace(partial, path)


def _save(out: Path, host: str, key: str, cert: str) -> tuple[Path, Path]:
    """<host>.key and <host>.crt in `out` (made owner-only when missing)."""
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path, cert_path = out / f"{host}.key", out / f"{host}.crt"
    _write_private(key_path, key)
    _write_private(cert_path, cert)
    return key_path, cert_path


async def issue(
    inventory_path: Path, host: str, out: Path, *, shell: Shell = run_shell
) -> dict[str, Any]:
    """Issue `host`'s client certificate on the recorded manager and write <host>.key and
    <host>.crt into `out`; returns their paths and the certificate's SHA-256."""
    if not re.fullmatch(HOST_NAME_PATTERN, host):
        raise deploy.DeployError("--host must be a browser host's logical name, browser-<n>")
    inventory = manager_daily.recorded(inventory_path)
    if not inventory.get("browser_port"):
        raise deploy.DeployError("the manager has no browser port: orchestrator-enable first")
    output = await deploy.Manager(inventory, inventory_path, shell).run(
        host_layout.script(inventory, issue_script(host)), "browser-cert"
    )
    key, cert = _pair(output)
    key_path, cert_path = await asyncio.to_thread(_save, out, host, key, cert)
    return {
        "host": host,
        "key": str(key_path),
        "cert": str(cert_path),
        "cert_sha256": hashlib.sha256(cert.encode()).hexdigest(),
        "valid_days": CERT_DAYS,
        "orchestrator_url": f"https://{inventory['host']}:{inventory['browser_port']}",
    }


def add_parser(steps) -> None:
    """`campaign manager browser-cert --host browser-<n> --out <dir>`."""
    step = steps.add_parser(
        "browser-cert", help="Issue a browser host's client certificate (key stays private)"
    )
    step.add_argument("--host", required=True, help="the host's logical name, browser-<n>")
    step.add_argument("--out", type=Path, required=True, help="directory for <host>.key/.crt")
    step.add_argument("--inventory", type=Path, required=True, help="the atlas account's inventory")
