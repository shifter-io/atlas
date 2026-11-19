"""The host's warm public-asset cache (BROWSER-BYTES-1; docs/browser.html, Settings).

A target's `cache_urls` name the static files (scripts, styles, fonts) its tries may take from
this cache instead of the network. Nothing is ever downloaded for the cache itself: a miss loads
in the browser exactly as without the cache, and lean.collector keeps the body the browser got;
a later try of the same network is served that body. One cache per network (provider, product),
so no network's tries use what another network downloaded.

Only a public file is kept: a GET answered 200 with a Cache-Control max-age (never `private`,
`no-store` or `no-cache`), varying by nothing beyond the encoding or origin, for a request with no
`Authorization` or `Range` header and no credentials in its URL. A response that sets a cookie is
kept only when it says `public` (a CDN's visitor cookie), and no entry ever keeps a cookie. Each
entry lives for its own max-age, at most `[browser] cache_hours` (configure()).

With a directory (the host state's `asset-cache/`, configure()) entries survive the process, so
the next day's runs start warm: one body file and one meta file per URL (named by its SHA-256,
mode 600), at most DISK_BYTES per network, the oldest dropped first. Without one, the cache is
memory only (MEMORY_BYTES per network, least recently used dropped first).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_AGE_S = 900
MAX_HOURS = 48
ENTRY_BYTES = 5_000_000
MEMORY_BYTES = 50_000_000
DISK_BYTES = 300_000_000
MAX_AGE = re.compile(r"max-age=(\d+)")
# Never kept: the body is already decoded, its length is the fulfilled body's own, no cookie.
DROPPED = frozenset({"content-encoding", "content-length", "transfer-encoding", "set-cookie"})
VARY_OK = frozenset({"accept-encoding", "origin"})
REQUEST_REFUSED = frozenset({"authorization", "range"})


@dataclass(frozen=True, slots=True)
class Entry:
    status: int
    headers: dict[str, str]
    body: bytes
    expires: float


def served_headers(headers: dict[str, str]) -> dict[str, str]:
    """The headers a kept body is served with: no encoding, length or cookie."""
    return {name: value for name, value in headers.items() if name.lower() not in DROPPED}


def cache_seconds(status: int, headers: dict[str, str], cap: float = DEFAULT_AGE_S) -> int:
    """How long a response may be served from the cache: 0 unless it is a public 200."""
    lowered = {name.lower(): value.lower() for name, value in headers.items()}
    control = lowered.get("cache-control", "")
    vary = {v.strip() for v in lowered.get("vary", "").split(",") if v.strip()}
    if status != 200 or not vary <= VARY_OK:
        return 0
    if "set-cookie" in lowered and "public" not in control:
        return 0
    if any(word in control for word in ("private", "no-store", "no-cache")):
        return 0
    age = MAX_AGE.search(control)
    return 0 if age is None else int(min(int(age.group(1)), cap))


def request_allowed(url: str, method: str, headers: dict[str, str]) -> bool:
    """A request whose answer may be kept: a GET with no credentials of its own."""
    parts = urlsplit(url)
    if method != "GET" or parts.scheme != "https" or parts.username or parts.password:
        return False
    return not REQUEST_REFUSED & {name.lower() for name in headers}


@dataclass(slots=True)
class AssetCache:
    """One network's public files by URL: memory (an LRU in bytes), and a directory if given."""

    directory: Path | None = None
    max_age_s: float = DEFAULT_AGE_S
    clock: Callable[[], float] = time.time
    _entries: OrderedDict[str, Entry] = field(default_factory=OrderedDict)
    _size: int = 0
    _disk: int | None = None

    def get(self, url: str) -> Entry | None:
        entry = self._entries.get(url) or self._load(url)
        if entry is not None and entry.expires <= self.clock():
            self._drop(url)
            return None
        if entry is not None:
            self._remember(url, entry)
        return entry

    def put(self, url: str, status: int, headers: dict[str, str], body: bytes) -> bool:
        """Keep a public answer; whether it was kept."""
        ttl = cache_seconds(status, headers, self.max_age_s)
        if ttl <= 0 or len(body) > ENTRY_BYTES:
            return False
        entry = Entry(status, served_headers(headers), body, self.clock() + ttl)
        self._remember(url, entry)
        if self.directory is not None:
            self._write(url, entry)
        return True

    def _remember(self, url: str, entry: Entry) -> None:
        old = self._entries.pop(url, None)
        self._size += len(entry.body) - (len(old.body) if old else 0)
        self._entries[url] = entry
        while self._size > MEMORY_BYTES:
            _, dropped = self._entries.popitem(last=False)
            self._size -= len(dropped.body)

    def _drop(self, url: str) -> None:
        entry = self._entries.pop(url, None)
        if entry is not None:
            self._size -= len(entry.body)
        if self.directory is not None:
            for path in _paths(self.directory, url):
                with contextlib.suppress(OSError):
                    path.unlink()

    def _load(self, url: str) -> Entry | None:
        if self.directory is None:
            return None
        meta_path, body_path = _paths(self.directory, url)
        try:
            meta = json.loads(meta_path.read_text())
            if meta["url"] != url:
                return None
            body = body_path.read_bytes()
            # A lower cache_hours applies at once to files kept under a higher one.
            expires = min(float(meta["expires"]), body_path.stat().st_mtime + self.max_age_s)
            return Entry(int(meta["status"]), dict(meta["headers"]), body, expires)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write(self, url: str, entry: Entry) -> None:
        directory = self.directory
        if directory is None:
            return
        meta = {"url": url, "status": entry.status, "headers": entry.headers}
        meta["expires"] = entry.expires
        meta_path, body_path = _paths(directory, url)
        try:  # a full or unwritable disk only loses the copy that outlives the process
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            _atomic(body_path, entry.body)
            _atomic(meta_path, json.dumps(meta).encode())
        except OSError:
            return
        self._disk = (self._disk if self._disk is not None else _usage(directory)) + len(entry.body)
        if self._disk > DISK_BYTES:
            self._disk = _prune(directory, DISK_BYTES)


def _paths(directory: Path, url: str) -> tuple[Path, Path]:
    name = hashlib.sha256(url.encode()).hexdigest()
    return directory / f"{name}.json", directory / f"{name}.body"


def _atomic(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as out:
        out.write(data)
    os.replace(temporary, path)


def _usage(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.glob("*.body"))


def _prune(directory: Path, limit: int) -> int:
    """Drop the oldest bodies (and their meta) until the directory holds at most limit/2."""
    bodies = sorted(directory.glob("*.body"), key=lambda path: path.stat().st_mtime)
    total = sum(path.stat().st_size for path in bodies)
    for body in bodies:
        if total <= limit // 2:
            break
        total -= body.stat().st_size
        for path in (body, body.with_suffix(".json")):
            with contextlib.suppress(OSError):
                path.unlink()
    return total


# The host's settings for every network's cache (configure()), and the caches made so far.
SETTINGS: dict[str, Any] = {"directory": None, "max_age_s": DEFAULT_AGE_S}
CACHES: dict[tuple[str | None, str | None], AssetCache] = {}


def configure(directory: Path | None, hours: float) -> None:
    """Where the host keeps its caches (None: memory only) and their longest life."""
    SETTINGS.update(directory=directory, max_age_s=hours * 3600)
    CACHES.clear()


def network_cache(provider: str | None, product: str | None) -> AssetCache:
    """This host's cache of one network, made on first use."""
    key = (provider, product)
    if key not in CACHES:
        root = SETTINGS["directory"]
        directory = None if root is None else Path(root) / f"{provider}.{product}"
        CACHES[key] = AssetCache(directory, SETTINGS["max_age_s"])
    return CACHES[key]
