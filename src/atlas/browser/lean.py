"""A try's traffic rules (BROWSER-BYTES-1; docs/browser.html, Settings): what its context never
downloads, what it may take from the host's shared asset cache, and when navigation returns.

`[browser]` holds the defaults and a `[[target]]` may override each one. The defaults are
today's behaviour: only `block_resources` blocked, no URL rule, no cache, navigation returns at
the `load` event. Rules never touch the top document itself: a blocked try is still a real visit.

- `block_urls`: fnmatch patterns over the whole URL (`*` matches any characters, `/` too); a
  matching request is aborted, as a blocked resource type is.
- `cache_urls`: GET requests matching these patterns are fetched once through the try's own
  proxy and kept in memory for later tries of this host process, but only a response that is
  public (a Cache-Control `max-age`, never `private`, `no-store` or `no-cache`), varies by
  nothing beyond the encoding and sets no cookie unless it says `public` (a CDN's visitor
  cookie on a public file: the first try gets it, as without the cache; the entry never keeps
  it); each entry for at most its own max-age and CACHE_TTL_S. At most FETCHES such downloads
  run at once per network (each is its own proxied connection). No cookie, storage or profile
  is shared: only public static files.
- `wait_until`: `load` (every subresource of the page), `domcontentloaded` or `commit` (the
  first answer bytes); the transport then waits for the target's own selectors as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Any

from .field_checks import check_string

WAIT_UNTIL = ("load", "domcontentloaded", "commit")
MAX_PATTERNS = 200
CACHE_TTL_S = 900
CACHE_ENTRY_BYTES = 5_000_000
CACHE_BYTES = 50_000_000
FETCHES = 6
MAX_AGE = re.compile(r"max-age=(\d+)")
# Never kept: the body is already decoded, and its length is the fulfilled body's own.
DROPPED = frozenset({"content-encoding", "content-length", "transfer-encoding", "set-cookie"})
VARY_OK = frozenset({"accept-encoding", "origin"})


@dataclass(frozen=True, slots=True)
class Entry:
    status: int
    headers: dict[str, str]
    body: bytes
    expires: float


@dataclass(slots=True)
class AssetCache:
    """Public static responses by URL, for every try of one host process (an LRU in bytes)."""

    limit: int = CACHE_BYTES
    clock: Callable[[], float] = time.monotonic
    _entries: OrderedDict[str, Entry] = field(default_factory=OrderedDict)
    _size: int = 0
    fetches: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(FETCHES))

    def get(self, url: str) -> Entry | None:
        entry = self._entries.get(url)
        if entry is not None and entry.expires <= self.clock():
            self._drop(url)
            return None
        if entry is not None:
            self._entries.move_to_end(url)
        return entry

    def put(self, url: str, status: int, headers: dict[str, str], body: bytes) -> None:
        ttl = cache_seconds(status, headers)
        if ttl <= 0 or len(body) > CACHE_ENTRY_BYTES:
            return
        self._drop(url)
        self._entries[url] = Entry(status, served_headers(headers), body, self.clock() + ttl)
        self._size += len(body)
        while self._size > self.limit:
            self._drop(next(iter(self._entries)))

    def _drop(self, url: str) -> None:
        entry = self._entries.pop(url, None)
        if entry is not None:
            self._size -= len(entry.body)


# One cache per network (provider, product), never shared between networks: each network's
# tries do the same work, whatever another network on the host downloaded before.
CACHES: dict[tuple[str | None, str | None], AssetCache] = {}


def network_cache(provider: str | None, product: str | None) -> AssetCache:
    """This host process's cache of one network, made on first use."""
    return CACHES.setdefault((provider, product), AssetCache())


def served_headers(headers: dict[str, str], cookies: bool = False) -> dict[str, str]:
    """The headers a decoded body is fulfilled with: no encoding or length, and a cookie only
    with `cookies` (the try's own download, never a stored entry)."""
    dropped = DROPPED - {"set-cookie"} if cookies else DROPPED
    return {name: value for name, value in headers.items() if name.lower() not in dropped}


def cache_seconds(status: int, headers: dict[str, str]) -> int:
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
    if age is None:
        return 0
    return min(int(age.group(1)), CACHE_TTL_S)


@dataclass(frozen=True, slots=True)
class Lean:
    """One try's rules; the default blocks nothing and waits for `load`."""

    types: tuple[str, ...] = ()
    block_urls: tuple[str, ...] = ()
    cache_urls: tuple[str, ...] = ()
    wait_until: str = "load"
    # The network's cache (network_cache); without one, each context keeps its own.
    cache: AssetCache | None = field(default=None, compare=False, repr=False)

    def routes(self) -> bool:
        """Whether the context needs a route handler at all."""
        return bool(self.types or self.block_urls or self.cache_urls)

    def blocked(self, request: Any) -> bool:
        if _top_document(request):
            return False
        return request.resource_type in self.types or _matches(request.url, self.block_urls)

    def cacheable(self, request: Any) -> bool:
        return request.method == "GET" and _matches(request.url, self.cache_urls)


def of(block: Lean | Collection[str]) -> Lean:
    """A context's rules: a Lean as given, or (the old form) only these resource types."""
    return block if isinstance(block, Lean) else Lean(types=tuple(block))


def _top_document(request: Any) -> bool:
    # The frame is read only for a document: a service worker's request has none.
    if request.resource_type != "document" or not request.is_navigation_request():
        return False
    return request.frame.parent_frame is None


def _matches(url: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(url, pattern) for pattern in patterns)


def router(lean: Lean) -> Callable[[Any], Any]:
    """The context's route handler: abort, serve from the cache, fetch into it, or continue."""
    cache = lean.cache if lean.cache is not None else AssetCache()

    async def route(route: Any) -> None:
        request = route.request
        if lean.blocked(request):
            await route.abort()
        elif lean.cacheable(request):
            await _cached(route, request.url, cache)
        else:
            await route.continue_()

    return route


async def _cached(route: Any, url: str, cache: AssetCache) -> None:
    entry = cache.get(url)
    if entry is not None:
        await route.fulfill(status=entry.status, headers=entry.headers, body=entry.body)
        return
    try:
        async with cache.fetches:
            response = await route.fetch()
            body = await response.body()
    except Exception:  # boundary: a failed fetch is a failed request, never a hung one
        with contextlib.suppress(Exception):
            await route.abort()
        return
    cache.put(url, response.status, response.headers, body)
    headers = served_headers(response.headers, cookies=True)
    await route.fulfill(status=response.status, headers=headers, body=body)


def check_patterns(value: Any, path: str) -> tuple[str, ...]:
    """A list of 0 to MAX_PATTERNS URL patterns, each 1 to 300 characters without spaces."""
    if not isinstance(value, list) or len(value) > MAX_PATTERNS:
        raise ValueError(f"{path}: must be a list of 0 to {MAX_PATTERNS} entries")
    checked = [
        check_string(item, f"{path}[{index}]", r"\S+", "a URL pattern", 300)
        for index, item in enumerate(value)
    ]
    if len(set(checked)) != len(checked):
        raise ValueError(f"{path}: a pattern is listed twice")
    return tuple(checked)


def check_wait(value: Any, path: str) -> str:
    if not isinstance(value, str) or value not in WAIT_UNTIL:
        raise ValueError(f"{path}: must be one of {list(WAIT_UNTIL)}")
    return value


# The optional [browser] and [[target]] fields of these rules, with their checks.
CHECKS = {"block_urls": check_patterns, "cache_urls": check_patterns, "wait_until": check_wait}
