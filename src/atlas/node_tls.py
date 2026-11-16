"""How a node trusts the orchestrator over HTTPS (NODE-TLS-1).

`client_tls(url, ca_file)` checks a node's orchestrator URL and returns what the node's HTTP
session verifies the server with. With `ca_file`, the manager's self-signed certificate (the
`campaign-manager.orchestrator.crt` that `campaign manager orchestrator-enable` writes beside
the inventory), the session trusts exactly that one certificate and no public CA: the URL must
be https:// and its host the address the certificate names. Without it an https:// URL is
verified against the system's public CAs. Plain http:// is accepted only to this machine (a
local staging orchestrator): to another host the node's bearer token would cross the network
in clear text, so it is refused. Every refusal is a UserError that names no URL.

A browser host also presents a client certificate and its key (BROWSER-MTLS-1: the manager's
browser port asks for one signed by its browser CA): both owner-only files, https only.
`openssl` runs the system tool that makes those keys, requests and certificates
(SEC-MINI-KEYS-1).
"""

from __future__ import annotations

import ipaddress
import os
import ssl
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .errors import UserError

PEM_BEGIN = b"-----BEGIN CERTIFICATE-----"
CRL_BEGIN = b"-----BEGIN X509 CRL-----"


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_private(path: str | Path) -> None:
    """An owner-only (mode 600) regular file, opened without following links; ValueError."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("must be a regular file")
    if os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError("must be owned by the current user with mode 600")


def openssl(args: list[str], what: str, cwd: Path) -> None:
    """Run the system `openssl` (OpenSSL, or the LibreSSL macOS ships) in `cwd`. Its output is
    dropped; a failure is a UserError naming `what` alone."""
    try:
        done = subprocess.run(
            ["openssl", *args], cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True
        )
    except OSError:
        raise UserError("the openssl command cannot be run") from None
    if done.returncode != 0:
        raise UserError(f"openssl could not {what}")


def write_private(path: Path, data: bytes) -> None:
    """Replace `path` with `data`, owner-only (0600) from the start."""
    partial = path.with_name(f"{path.name}.partial")
    partial.unlink(missing_ok=True)
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
    os.replace(partial, path)


def pinned_context(ca_file: str | Path) -> ssl.SSLContext:
    """A client context whose only trust anchor is the one PEM certificate in `ca_file`; the
    server's name is checked against it (TLS 1.2 or newer)."""
    try:
        data = Path(ca_file).read_bytes()
    except OSError:
        raise UserError("the orchestrator CA file cannot be read") from None
    if data.count(PEM_BEGIN) != 1:
        raise UserError("the orchestrator CA file must hold exactly one PEM certificate")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # hostname check, certificate required
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_verify_locations(cadata=data.decode("ascii"))
    except (ssl.SSLError, UnicodeDecodeError, ValueError):
        raise UserError("the orchestrator CA file is not a valid PEM certificate") from None
    return context


def client_tls(
    url: str,
    ca_file: str | Path | None = None,
    client: tuple[str | Path, str | Path] | None = None,
) -> ssl.SSLContext | bool:
    """The aiohttp `ssl` value for the orchestrator at `url`: the pinned context, or True
    (public CAs; unused for loopback http); with `client` (certificate, key), a context that
    presents them. Refuses a URL or file the node must not use."""
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        if client:
            return _with_client(pinned_context(ca_file) if ca_file else None, *client)
        return pinned_context(ca_file) if ca_file else True
    if parts.scheme != "http" or not parts.hostname:
        raise UserError("the orchestrator URL must start with https://")
    if ca_file or client:
        raise UserError("an orchestrator CA or client certificate needs an https:// URL")
    if not _loopback(parts.hostname):
        raise UserError(
            "plain http:// is refused for a remote orchestrator: use https:// "
            "(with --ca-file for the manager's own certificate)"
        )
    return True


def _with_client(
    context: ssl.SSLContext | None, cert: str | Path, key: str | Path
) -> ssl.SSLContext:
    """`context` (or one trusting the public CAs) presenting the client certificate `cert`
    with its key `key`, both owner-only files."""
    for name, path in (("certificate", cert), ("key", key)):
        try:
            check_private(path)
        except (OSError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "cannot be read"
            raise UserError(f"the client {name} file {reason}") from None
    context = context or ssl.create_default_context()
    try:
        context.load_cert_chain(cert, key)
    except (ssl.SSLError, ValueError):
        raise UserError("the client certificate and key are not a valid PEM pair") from None
    return context
