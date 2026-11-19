"""SEC-MINI-KEYS-1: a browser host makes its own key; only its signing request leaves it.

`atlas browser keygen --host browser-<n> --out DIR` makes, with the system openssl, a new EC P-256
key `DIR/<host>.key` (owner-only, never printed) and a certificate signing request
`DIR/<host>.csr` for CN=<host>. The request holds no secret: the operator signs it on their Mac
with the browser CA (`campaign manager browser-sign`) and copies only the certificate back, as
`DIR/<host>.crt` beside the key. An existing key is never replaced: a new key goes into a new
DIR, so the running node keeps its current pair until it is restarted on the new one, and can
be restarted on the old one again.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..errors import UserError
from ..node_tls import openssl
from .contracts import HOST_NAME_PATTERN


def keygen(host: str, out: Path) -> dict[str, Any]:
    """Make `host`'s key and signing request in `out`; returns their paths and the request's
    SHA-256 (to compare with what the operator signs)."""
    if not re.fullmatch(HOST_NAME_PATTERN, host):
        raise UserError("--host must be a browser host's logical name, browser-<n>")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    key, csr = out / f"{host}.key", out / f"{host}.csr"
    if key.exists() or key.is_symlink():
        raise UserError(f"{key.name} is already in --out: make the new key in a new folder")
    work = Path(tempfile.mkdtemp(prefix=".keygen-", dir=out))  # owner-only (0700)
    try:
        new = ["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "key"]
        openssl(new, "make the key", work)
        os.chmod(work / "key", 0o600)
        request = ["req", "-new", "-key", "key", "-subj", f"/CN={host}", "-sha256", "-out", "csr"]
        openssl(request, "make the signing request", work)
        os.replace(work / "csr", csr)
        os.replace(work / "key", key)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {
        "host": host,
        "key": str(key),
        "csr": str(csr),
        "csr_sha256": hashlib.sha256(csr.read_bytes()).hexdigest(),
    }


def add_parser(actions) -> None:
    """`atlas browser keygen --host browser-<n> --out DIR`."""
    step = actions.add_parser(
        "keygen", help="Make this host's key and certificate signing request (key stays here)"
    )
    step.add_argument("--host", required=True, help="this host's logical name, browser-<n>")
    step.add_argument("--out", type=Path, required=True, help="a new owner-only folder")
