"""How a node trusts the orchestrator over HTTPS (NODE-TLS-1).

`client_tls(url, ca_file)` checks a node's orchestrator URL and returns what the node's HTTP
session verifies the server with. With `ca_file`, the manager's self-signed certificate (the
`campaign-manager.orchestrator.crt` that `campaign manager orchestrator-enable` writes beside
the inventory), the session trusts exactly that one certificate and no public CA: the URL must
be https:// and its host the address the certificate names. Without it an https:// URL is
verified against the system's public CAs. Plain http:// is accepted only to this machine (a
local staging orchestrator): to another host the node's bearer token would cross the network
in clear text, so it is refused. Every refusal is a UserError that names no URL.
"""

from __future__ import annotations

import ipaddress
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from .errors import UserError

PEM_BEGIN = b"-----BEGIN CERTIFICATE-----"


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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


def client_tls(url: str, ca_file: str | Path | None = None) -> ssl.SSLContext | bool:
    """The aiohttp `ssl` value for the orchestrator at `url`: the pinned context, or True
    (public CAs; unused for loopback http). Refuses a URL the node must not use."""
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return pinned_context(ca_file) if ca_file else True
    if parts.scheme != "http" or not parts.hostname:
        raise UserError("the orchestrator URL must start with https://")
    if ca_file:
        raise UserError("an orchestrator CA file needs an https:// orchestrator URL")
    if not _loopback(parts.hostname):
        raise UserError(
            "plain http:// is refused for a remote orchestrator: use https:// "
            "(with --ca-file for the manager's own certificate)"
        )
    return True
