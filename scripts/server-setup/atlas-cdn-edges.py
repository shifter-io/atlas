#!/usr/bin/env python3
"""The CDN's edge-server addresses for Caddy (SEC-RATE-1), refreshed daily by atlas-cdn-edges.timer.

Caddy names the visitor to siteapi (its per-visitor budget for uncached work) from the CDN's
X-Forwarded-For only on a request from one of the CDN's edge servers; any other request is its
own remote address. This script reads the URLs of the CDN's published edge-server lists (one per
line, `#` starts a comment) from ~/.config/atlas/cdn-edge-lists, fetches each (a JSON array of
addresses, or one address per line) and writes two files into <caddy>/edges/:

  cdn-edges.caddy      `trusted_proxies static <ranges>`, imported in the Caddyfile's servers block
  client-header.caddy  `header_up X-Atlas-Client-Ip {client_ip}`, imported in each reverse_proxy

They come together: before the first run Caddy sends siteapi no visitor, and siteapi keeps no
per-visitor budget. A list that fails to fetch or parse, or a result under --min addresses or
holding a private or too wide range, changes nothing and exits 1: the previous files stay. When
the files change, `caddy validate` checks the whole configuration (the previous files come back
when it fails), then Caddy reloads if it runs.

  atlas-cdn-edges [--caddy DIR] [--lists FILE] [--min N] [--dry-run]
"""

import argparse
import ipaddress
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HEADER = "X-Atlas-Client-Ip"
MAX_BYTES = 1 << 20
MAX_RANGES = 20_000
WIDEST = {4: 16, 6: 32}  # the widest range accepted per IP version (prefix length)
PRIVATE = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7")
NOT_EDGES = [ipaddress.ip_network(n) for n in (*PRIVATE, "::ffff:0:0/96")]
NOTE = "# Written by atlas-cdn-edges (SEC-RATE-1); do not edit: the next run replaces it.\n"

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class EdgeListError(Exception):
    """A list that cannot be fetched or read, or a result Caddy must not trust."""


def parse_list(text: str) -> list[Network]:
    """The ranges of one published list: a JSON array of strings, or one address per line."""
    text = text.strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
        except ValueError as e:
            raise EdgeListError(f"not JSON: {e}") from None
        if not all(isinstance(i, str) for i in items):
            raise EdgeListError("a JSON list entry is not a string")
    else:
        items = [line for line in text.splitlines() if line.strip()]
    out = []
    for item in items:
        try:
            out.append(ipaddress.ip_network(item.strip(), strict=False))
        except ValueError:
            raise EdgeListError(f"{item.strip()[:60]!r} is no address or range") from None
    return out


def checked(networks: list[Network], minimum: int) -> list[Network]:
    """The ranges sorted and without repeats, when Caddy may trust them all."""
    unique = sorted(set(networks), key=lambda n: (n.version, n))
    if not minimum <= len(unique) <= MAX_RANGES:
        raise EdgeListError(f"{len(unique)} addresses, want {minimum} to {MAX_RANGES}")
    for n in unique:
        special = n.is_loopback or n.is_link_local or n.is_multicast or n.is_unspecified
        if special or any(n.overlaps(p) for p in NOT_EDGES if p.version == n.version):
            raise EdgeListError(f"{n} is no public address")
        if n.prefixlen < WIDEST[n.version]:
            raise EdgeListError(f"{n} is wider than /{WIDEST[n.version]}")
    return unique


def rendered(networks: list[Network]) -> dict[str, str]:
    """The two files' contents by name."""
    return {
        "cdn-edges.caddy": NOTE + "trusted_proxies static " + " ".join(map(str, networks)) + "\n",
        "client-header.caddy": NOTE + f"header_up {HEADER} {{client_ip}}\n",
    }


def list_urls(path: Path) -> list[str]:
    """The list URLs in path: https, or http to the loopback (tests)."""
    try:
        lines = path.read_text().splitlines()
    except OSError as e:
        raise EdgeListError(f"cannot read {path}: {e.strerror}") from None
    urls = [line.split("#", 1)[0].strip() for line in lines]
    urls = [u for u in urls if u]
    if not urls:
        raise EdgeListError(f"{path} names no list")
    for u in urls:
        p = urllib.parse.urlsplit(u)
        if p.scheme != "https" and not (p.scheme == "http" and p.hostname in ("127.0.0.1", "::1")):
            raise EdgeListError(f"{u} is no https URL")
    return urls


def fetch(url: str) -> str:
    """One list's text, at most MAX_BYTES."""
    req = urllib.request.Request(url, headers={"Accept": "application/json, text/plain"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read(MAX_BYTES + 1)
    except OSError as e:
        raise EdgeListError(f"{url}: {e}") from None
    if len(body) > MAX_BYTES:
        raise EdgeListError(f"{url}: more than {MAX_BYTES} bytes")
    return body.decode("utf-8", "replace")


def run(cmd: list[str], env: dict[str, str] | None = None) -> bool:
    return subprocess.run(cmd, env=env, capture_output=True, check=False).returncode == 0


def validate(caddy: Path) -> bool:
    env = {**os.environ, "XDG_DATA_HOME": str(caddy / "data")}
    env["XDG_CONFIG_HOME"] = str(caddy / "config")
    config = str(caddy / "Caddyfile")
    return run(["caddy", "validate", "--config", config, "--adapter", "caddyfile"], env)


def install(caddy: Path, files: dict[str, str]) -> bool:
    """Writes files into caddy/edges when they differ, validates and reloads Caddy; False when
    nothing changed. A failed validation puts the previous files back."""
    edges = caddy / "edges"
    old = {name: (edges / name).read_text() if (edges / name).exists() else None for name in files}
    if all(old[name] == text for name, text in files.items()):
        return False
    edges.mkdir(mode=0o700, parents=True, exist_ok=True)
    put(edges, files)
    if not validate(caddy):
        put(edges, old)
        raise EdgeListError("caddy validate failed: the previous files are back")
    if not run(["systemctl", "--user", "try-reload-or-restart", "atlas-caddy"]):
        raise EdgeListError("the Caddy reload failed")
    return True


def put(edges: Path, files: dict[str, str | None]) -> None:
    """Writes (or, for None, removes) each file through a temporary one."""
    for name, text in files.items():
        if text is None:
            (edges / name).unlink(missing_ok=True)
            continue
        tmp = edges / f".{name}.tmp"
        tmp.write_text(text)
        tmp.chmod(0o644)
        tmp.replace(edges / name)


def main(argv: list[str] | None = None) -> int:
    home = Path.home()
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--caddy", type=Path, default=home / "atlas" / "caddy")
    ap.add_argument("--lists", type=Path, default=home / ".config" / "atlas" / "cdn-edge-lists")
    ap.add_argument("--min", type=int, default=10, help="fewest addresses accepted (default 10)")
    ap.add_argument("--dry-run", action="store_true", help="print the ranges, change nothing")
    args = ap.parse_args(argv)
    try:
        found = [n for u in list_urls(args.lists) for n in parse_list(fetch(u))]
        networks = checked(found, args.min)
        if args.dry_run:
            print(rendered(networks)["cdn-edges.caddy"], end="")
            return 0
        changed = install(args.caddy, rendered(networks))
    except EdgeListError as e:
        print(f"atlas-cdn-edges: {e}", file=sys.stderr)
        return 1
    done = "Caddy reloaded" if changed else "unchanged"
    print(f"atlas-cdn-edges: {len(networks)} ranges, {done}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
