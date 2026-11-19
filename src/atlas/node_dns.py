"""A node's own DNS failures (PULSE-NODE-DNS-1): what the run keeps of them, and when a pulse run
is the node's fault rather than the network's.

A 'dns' error is the node's own lookup of the gateway's name failing (aiohttp's
ClientConnectorDNSError); it says nothing about the network. The run summary keeps the exact
texts of those errors (at most MAX_TEXTS distinct ones, the rest only counted) and the node's
resolver settings in `dns_failures`, so the cause can be told after the droplet is gone.

A pulse run with at least NODE_DNS_SHARE of its completed requests failing that way is the
node's fault (19 Nov: blocks of 25 or 50 per 100 requests in an hour's first minutes, the
other requests timing out on hanging lookups). The node runs it once more after
RETRY_SECONDS. When the retry fails the same way, control() looks up names the node's resolver
must know whatever the network does (the target's host, the orchestrator's): only when those
fail too is it the node's fault, reported failed with failure_text() and not uploaded, so the
run is never judged nor counted for the network. When one resolves, the gateway's own name is
what failed (its zone or its domain, the same for every node): the retry is uploaded as the
network's data, its `dns` errors proxy errors as before.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

MAX_TEXTS = 5
TEXT_CHARS = 200
NODE_DNS_SHARE = 0.2
RETRY_SECONDS = 5.0
# A control lookup that has not answered in this long has failed (a hanging resolver).
CONTROL_SECONDS = 5.0
RESOLV_CONF = Path("/etc/resolv.conf")
# The orchestrator keeps this much of a failed run's error (nodework_finish.fail_run).
ERROR_CHARS = 500


def detail(exc: BaseException) -> str:
    """The exact text of a DNS failure and its resolver error number. aiohttp builds the text
    from the connection's host, port and TLS mode only: it never holds credentials."""
    number = getattr(getattr(exc, "os_error", None), "errno", None)
    text = str(exc)[:TEXT_CHARS]
    return text if number is None else f"{text} (errno {number})"


def resolver(path: Path = RESOLV_CONF) -> dict[str, Any]:
    """The node's resolver settings: resolv.conf's nameservers and options, where the file
    links to (a systemd-resolved stub, say) and aiohttp's resolver class."""
    found: dict[str, Any] = {"resolver": aiohttp.DefaultResolver.__name__}
    try:
        link = os.readlink(path) if path.is_symlink() else None
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return found | {"resolv_conf_error": type(exc).__name__}
    nameservers, options = [], []
    for line in lines:
        words = line.split()
        if words[:1] == ["nameserver"] and len(words) > 1:
            nameservers.append(words[1])
        elif words[:1] == ["options"]:
            options += words[1:]
    return found | {"resolv_conf": link, "nameservers": nameservers[:4], "options": options}


@dataclass
class DnsFailures:
    """A run's DNS failure texts: the first MAX_TEXTS distinct ones counted, the rest as other."""

    texts: Counter[str] = field(default_factory=Counter)
    other: int = 0

    def add(self, text: str | None) -> None:
        if text is None:
            return
        if text in self.texts or len(self.texts) < MAX_TEXTS:
            self.texts[text] += 1
        else:
            self.other += 1

    def report(self) -> dict[str, Any] | None:
        """The summary's `dns_failures`; None when the run had none."""
        if not self.texts:
            return None
        return {"texts": dict(self.texts.most_common()), "other": self.other} | {
            "resolver": resolver()
        }


def local_fault(summary: Any) -> bool:
    """Whether the node's own DNS failed for at least NODE_DNS_SHARE of a run's requests."""
    completed = summary.completed
    return completed > 0 and summary.errors.get("dns", 0) >= NODE_DNS_SHARE * completed


def control_names(*urls: object) -> list[str]:
    """The host names of the URLs, once each; IP literals need no lookup and are left out."""
    names: list[str] = []
    for url in urls:
        host = urlsplit(url).hostname if isinstance(url, str) else None
        try:
            ipaddress.ip_address(host or "")
        except ValueError:
            if host and host not in names:
                names.append(host)
    return names


async def control(names: list[str]) -> dict[str, str] | None:
    """None when one of the names resolves (the node's resolver works); else how each lookup
    failed, {} without any name to look up (the node's fault, as the DNS errors say)."""
    loop, failed = asyncio.get_running_loop(), {}
    for name in names:
        try:
            async with asyncio.timeout(CONTROL_SECONDS):
                await loop.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
        except (OSError, TimeoutError) as exc:
            failed[name] = f"{type(exc).__name__} {getattr(exc, 'errno', None)}"
            continue
        return None
    return failed


def attempt(summary: Any) -> dict[str, Any]:
    """What the retry's summary keeps of the attempt it replaced (`dns_retry`)."""
    return {
        "run_id": summary.run_id,
        "started_at": summary.started_at,
        "requests": summary.completed,
        "dns": summary.errors.get("dns", 0),
        "dns_failures": summary.dns_failures,
    }


def failure_text(
    summary: Any, first: dict[str, Any] | None = None, checked: dict[str, str] | None = None
) -> str:
    """The failed run's error: `node_dns` and, as JSON within ERROR_CHARS, both attempts'
    requests and DNS errors, the control lookups, the resolver and as many of the latest
    texts as fit."""
    failures = summary.dns_failures or {}
    texts = list(failures.get("texts", {}).items())
    base = {
        "requests": summary.completed,
        "dns": summary.errors.get("dns", 0),
        "first": {key: first[key] for key in ("requests", "dns")} if first else None,
        "control": checked,
        "resolver": failures.get("resolver"),
    }
    for kept in range(len(texts), -1, -1):
        text = "node_dns " + json.dumps(base | {"texts": dict(texts[:kept])}, separators=(",", ":"))
        if len(text) <= ERROR_CHARS:
            return text
    return text[:ERROR_CHARS]
