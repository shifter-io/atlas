"""SEC-MINI-KEYS-1: the browser hosts' client CA, kept on the operator's Mac and never on a server.

`campaign manager browser-ca --ca-dir DIR` makes it once in DIR (owner-only, 0700): an EC P-256
key `ca.key` that never leaves DIR, its certificate `ca.crt` (CN atlas-browser-ca-<date>, CA_DAYS
days, signs certificates and revocation lists) and an empty revocation list `ca.crl`, with the
bookkeeping of the system openssl's `ca` command (openssl.cnf, index.txt, serial, crlnumber).

`browser-sign --ca-dir DIR --host browser-<n> --csr FILE --out OUT` signs a host's request (the
host made its key itself, `atlas browser keygen`): the subject is always CN=--host whatever the
request names, client authentication only, CERT_DAYS days. `OUT/<host>.crt` is written
owner-only, as the host needs it; DIR/issued keeps a copy.

`browser-revoke --ca-dir DIR --host browser-<n>` revokes every certificate of that host that is
still valid and writes a new `ca.crl`; `browser-trust --apply` installs it on the manager. The
list is pushed to the manager, never fetched, so its next update is CRL_DAYS days away: an
expired list would refuse every host.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import ssl
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..browser.contracts import HOST_NAME_PATTERN
from ..errors import UserError
from ..node_tls import CRL_BEGIN, PEM_BEGIN, check_private, openssl, write_private

CA_DAYS, CERT_DAYS, CRL_DAYS = 3650, 90, 3650
ACTIONS = ("browser-ca", "browser-sign", "browser-revoke")
CONFIG = f"""[ca]
default_ca = atlas_browser
[atlas_browser]
database = index.txt
new_certs_dir = issued
certificate = ca.crt
private_key = ca.key
serial = serial
crlnumber = crlnumber
default_md = sha256
default_days = {CERT_DAYS}
default_crl_days = {CRL_DAYS}
unique_subject = no
copy_extensions = none
policy = host_policy
x509_extensions = host_cert
[host_policy]
commonName = supplied
[host_cert]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = clientAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
[ca_cert]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
[req]
distinguished_name = req_name
[req_name]
"""


def _host(host: str) -> str:
    if not re.fullmatch(HOST_NAME_PATTERN, host):
        raise UserError("--host must be a browser host's logical name, browser-<n>")
    return host


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ca(ca_dir: Path) -> Path:
    """The CA folder, owner-only, with its owner-only key."""
    try:
        info = ca_dir.lstat()
        check_private(ca_dir / "ca.key")
    except (OSError, ValueError):
        raise UserError("--ca-dir holds no usable browser CA: browser-ca first") from None
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise UserError("--ca-dir must be an owner-only (700) folder")
    return ca_dir


def _crl(ca_dir: Path) -> dict[str, str]:
    """Write a fresh `ca.crl` from the CA's records."""
    openssl(
        ["ca", "-config", "openssl.cnf", "-gencrl", "-out", "ca.crl.partial"],
        "write the revocation list",
        ca_dir,
    )
    os.replace(ca_dir / "ca.crl.partial", ca_dir / "ca.crl")
    return {"crl": str(ca_dir / "ca.crl"), "crl_sha256": _sha256(ca_dir / "ca.crl")}


def make_ca(ca_dir: Path, now: datetime | None = None) -> dict[str, Any]:
    """Make the CA in a new or empty owner-only `ca_dir`; refuses one that holds a CA."""
    if (ca_dir / "ca.key").exists() or (ca_dir / "index.txt").exists():
        raise UserError("--ca-dir already holds a browser CA")
    ca_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ca_dir, 0o700)  # an existing empty folder may come with a wider mode
    (ca_dir / "issued").mkdir(mode=0o700, exist_ok=True)
    (ca_dir / "openssl.cnf").write_text(CONFIG)
    (ca_dir / "index.txt").write_text("")
    (ca_dir / "serial").write_text(f"{secrets.randbits(63) | 1 << 62:X}\n")
    (ca_dir / "crlnumber").write_text("1000\n")
    key = ["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "ca.key"]
    openssl(key, "make the CA key", ca_dir)
    os.chmod(ca_dir / "ca.key", 0o600)
    name = f"/CN=atlas-browser-ca-{(now or datetime.now(UTC)):%Y%m%d}"
    certificate = ["req", "-x509", "-new", "-key", "ca.key", "-config", "openssl.cnf"]
    certificate += ["-extensions", "ca_cert", "-subj", name, "-days", str(CA_DAYS), "-sha256"]
    openssl([*certificate, "-out", "ca.crt"], "make the CA certificate", _ca(ca_dir))
    ca_cert = ca_dir / "ca.crt"
    return {"ca_cert": str(ca_cert), "ca_sha256": _sha256(ca_cert), **_crl(ca_dir)}


def sign(ca_dir: Path, host: str, csr: Path, out: Path) -> dict[str, Any]:
    """Sign `host`'s request `csr`; write `out/<host>.crt` (owner-only)."""
    ca_dir, host = _ca(ca_dir), _host(host)
    if not csr.is_file():
        raise UserError("--csr is not a readable file")
    partial = ca_dir / "signed.partial"
    signing = ["ca", "-batch", "-config", "openssl.cnf", "-in", str(csr.resolve())]
    signing += ["-subj", f"/CN={host}", "-notext", "-out", partial.name]
    try:
        openssl(signing, "sign the request", ca_dir)
        pem = partial.read_bytes()
    finally:
        partial.unlink(missing_ok=True)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    cert = out / f"{host}.crt"
    write_private(cert, pem)
    return {
        "host": host,
        "cert": str(cert),
        "cert_sha256": hashlib.sha256(pem).hexdigest(),
        "csr_sha256": _sha256(csr),
        "valid_days": CERT_DAYS,
    }


def _serials(ca_dir: Path, host: str) -> dict[str, list[str]]:
    """The serials of `host`'s certificates by the status the CA's records give them (V valid,
    R revoked, E expired)."""
    found: dict[str, list[str]] = {}
    for line in (ca_dir / "index.txt").read_text().splitlines():
        fields = line.split("\t")
        if len(fields) == 6 and fields[5] == f"/CN={host}":
            found.setdefault(fields[0], []).append(fields[3])
    return found


def revoke(ca_dir: Path, host: str) -> dict[str, Any]:
    """Revoke every valid certificate of `host` and write the new `ca.crl`; with none valid but
    some revoked (a run stopped before its list), only write the list again."""
    ca_dir, host = _ca(ca_dir), _host(host)
    found = _serials(ca_dir, host)
    serials = found.get("V", [])
    if not serials and not found.get("R"):
        raise UserError(f"the CA has no certificate of {host} to revoke")
    for serial in serials:
        cert = f"issued/{serial}.pem"
        openssl(["ca", "-config", "openssl.cnf", "-revoke", cert], "revoke a certificate", ca_dir)
    return {"host": host, "revoked": serials, **_crl(ca_dir)}


def published(ca_dir: Path) -> tuple[bytes, bytes]:
    """The CA certificate and list `browser-trust` installs, refused unless they load as the
    browser port loads them (a broken file would stop the orchestrator)."""
    ca_dir = _ca(ca_dir)
    cert, crl = ca_dir / "ca.crt", ca_dir / "ca.crl"
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        data = cert.read_bytes(), crl.read_bytes()
        context.load_verify_locations(cafile=str(cert))
        context.load_verify_locations(cafile=str(crl))
    except (OSError, ssl.SSLError):
        raise UserError("the CA's ca.crt or ca.crl cannot be loaded") from None
    if data[0].count(PEM_BEGIN) != 1 or CRL_BEGIN not in data[1]:
        raise UserError("ca.crt must hold one certificate and ca.crl its revocation list")
    return data


async def run(args) -> dict[str, Any]:
    """One of ACTIONS; all local to this Mac."""
    if args.manager_action == "browser-ca":
        return await asyncio.to_thread(make_ca, args.ca_dir)
    if args.manager_action == "browser-sign":
        return await asyncio.to_thread(sign, args.ca_dir, args.host, args.csr, args.out)
    return await asyncio.to_thread(revoke, args.ca_dir, args.host)


def add_parsers(steps) -> None:
    """`campaign manager browser-ca|browser-sign|browser-revoke --ca-dir DIR …`."""
    made = steps.add_parser("browser-ca", help="Make the browser hosts' client CA on this Mac")
    signed = steps.add_parser("browser-sign", help="Sign a browser host's certificate request")
    signed.add_argument("--csr", type=Path, required=True, help="the host's <host>.csr")
    signed.add_argument("--out", type=Path, required=True, help="folder for <host>.crt")
    revoked = steps.add_parser("browser-revoke", help="Revoke a browser host's certificates")
    for step in (signed, revoked):
        step.add_argument("--host", required=True, help="the host's logical name, browser-<n>")
    for step in (made, signed, revoked):
        step.add_argument(
            "--ca-dir", type=Path, required=True, help="the CA's owner-only folder on this Mac"
        )
