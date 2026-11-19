"""A try's traffic rules (BROWSER-BYTES-1; docs/browser.html, Settings): what its context never
downloads, what it may take from the host's shared asset cache, and when navigation returns.

`[browser]` holds the defaults and a `[[target]]` may override each one. The defaults are
today's behaviour: only `block_resources` blocked, no URL rule, no cache, navigation returns at
the `load` event. Rules never touch the top document itself: a blocked try is still a real visit.

- `block_urls`: fnmatch patterns over the whole URL (`*` matches any characters, `/` too); a
  matching request is aborted, as a blocked resource type is.
- `cache_urls`: requests matching these patterns are served from the network's warm public-asset
  cache when it holds them (asset_cache.py); a miss loads in the browser as without the cache,
  and the collector keeps a public answer's body for later tries. No cookie, storage or profile
  is shared: only public static files.
- `wait_until`: `load` (every subresource of the page), `domcontentloaded` or `commit` (the
  first answer bytes); the transport then waits for the target's own selectors as before.
- `chrome_services`: false starts a Chrome try without Chrome's own calls to Google
  (background.quiet_chrome); true, the default, leaves Chrome as it is. Camoufox ignores it:
  its own services are always off.
"""

from __future__ import annotations

import asyncio
import fnmatch
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Any

from .asset_cache import AssetCache, request_allowed
from .field_checks import check_string

WAIT_UNTIL = ("load", "domcontentloaded", "commit")
MAX_PATTERNS = 200
# The body reads of kept responses still running (collector): held so none is lost unawaited.
READS: set[asyncio.Task] = set()


@dataclass(frozen=True, slots=True)
class Lean:
    """One try's rules; the default blocks nothing and waits for `load`."""

    types: tuple[str, ...] = ()
    block_urls: tuple[str, ...] = ()
    cache_urls: tuple[str, ...] = ()
    wait_until: str = "load"
    chrome_services: bool = True
    # The network's cache (asset_cache.network_cache); without one, nothing is cached.
    cache: AssetCache | None = field(default=None, compare=False, repr=False)

    def routes(self) -> bool:
        """Whether the context needs a route handler at all."""
        return bool(self.types or self.block_urls or self.cache_urls)

    def blocked(self, request: Any) -> bool:
        if _top_document(request):
            return False
        return request.resource_type in self.types or _matches(request.url, self.block_urls)

    def cacheable(self, request: Any) -> bool:
        if not _matches(request.url, self.cache_urls):
            return False
        return request_allowed(request.url, request.method, request.headers)


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
    """The context's route handler: abort, serve a kept file, or let the browser load it."""
    cache = lean.cache

    async def route(route: Any) -> None:
        request = route.request
        if lean.blocked(request):
            await route.abort()
            return
        hit = cache.get(request.url) if cache and lean.cacheable(request) else None
        if hit is not None:
            await route.fulfill(status=hit.status, headers=hit.headers, body=hit.body)
        else:
            await route.continue_()

    return route


def collector(lean: Lean) -> Callable[[Any], None]:
    """The context's response listener: a cacheable answer the browser loaded itself (never
    one the cache served) has its body kept in the network's cache, read in the background."""
    cache = lean.cache

    def on_response(response: Any) -> None:
        request = response.request
        if cache is None or not lean.cacheable(request) or response.status != 200:
            return
        if cache.get(response.url) is not None:
            return
        task = asyncio.ensure_future(_keep(response, cache))
        READS.add(task)
        task.add_done_callback(READS.discard)

    return on_response


async def _keep(response: Any, cache: AssetCache) -> None:
    try:
        headers = await response.all_headers()
        body = await response.body()
    except Exception:  # boundary: a page that closed first leaves nothing to keep
        return
    cache.put(response.url, response.status, headers, body)


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


def check_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{path}: must be true or false")
    return value


def check_wait(value: Any, path: str) -> str:
    if not isinstance(value, str) or value not in WAIT_UNTIL:
        raise ValueError(f"{path}: must be one of {list(WAIT_UNTIL)}")
    return value


# The optional [browser] and [[target]] fields of these rules, with their checks.
CHECKS = {
    "block_urls": check_patterns,
    "cache_urls": check_patterns,
    "wait_until": check_wait,
    "chrome_services": check_bool,
}
