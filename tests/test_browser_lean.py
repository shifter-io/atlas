"""browser.lean: a try's traffic rules, its route handler and the host's public asset cache."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from browser_transport_cases import OPTIONS, SETTINGS

from atlas.browser import lean
from atlas.browser.lean import AssetCache, Lean, network_cache
from atlas.browser.transport import lean_rules

PUBLIC = {"Cache-Control": "public, max-age=3600", "Content-Encoding": "br", "X-Kept": "1"}
STATIC = "https://static.example/js/app.js"


class Frame:
    def __init__(self, parent: object = None) -> None:
        self.parent_frame = parent


class Request:
    def __init__(self, url: str, kind: str = "script", method: str = "GET", top=False) -> None:
        self.url, self.resource_type, self.method = url, kind, method
        self.frame = Frame(None if top or kind != "document" else Frame())
        self._navigation = kind == "document"

    def is_navigation_request(self) -> bool:
        return self._navigation


class Response:
    def __init__(self, status: int = 200, headers: dict | None = None) -> None:
        self.status, self.headers = status, PUBLIC if headers is None else headers

    async def body(self) -> bytes:
        return b"body"


class Route:
    def __init__(self, request: Request, response: Response | Exception | None = None) -> None:
        self.request, self.response = request, response or Response()
        self.done: tuple = ()
        self.fetches = 0

    async def abort(self, error_code: str | None = None) -> None:
        self.done = ("abort", error_code)

    async def continue_(self) -> None:
        self.done = ("continue",)

    async def fetch(self) -> Response:
        self.fetches += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def fulfill(self, **response) -> None:
        self.done = ("fulfill", response)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("status", "headers", "seconds"),
    [
        (200, {"Cache-Control": "public, max-age=60"}, 60),
        (200, {"cache-control": "max-age=86400"}, lean.CACHE_TTL_S),
        (200, {"Cache-Control": "public, max-age=60", "Vary": "Accept-Encoding, Origin"}, 60),
        (200, {"Cache-Control": "public, max-age=60", "Vary": "Cookie"}, 0),
        (200, {"Cache-Control": "private, max-age=60"}, 0),
        (200, {"Cache-Control": "public, no-cache, max-age=60"}, 0),
        (200, {"Cache-Control": "no-store"}, 0),
        (200, {"Cache-Control": "public"}, 0),
        (200, {"Cache-Control": "public, max-age=60", "Set-Cookie": "a=b"}, 60),
        (200, {"Cache-Control": "max-age=60", "Set-Cookie": "a=b"}, 0),
        (304, {"Cache-Control": "public, max-age=60"}, 0),
    ],
)
def test_only_a_public_200_without_cookies_is_cached(status, headers, seconds):
    assert lean.cache_seconds(status, headers) == seconds


def test_the_cache_expires_drops_decoding_headers_and_keeps_its_byte_limit():
    clock = Clock()
    cache = AssetCache(limit=8, clock=clock)
    cache.put("a", 200, PUBLIC, b"1234")
    entry = cache.get("a")
    assert entry is not None and entry.body == b"1234"
    assert entry.headers == {"Cache-Control": "public, max-age=3600", "X-Kept": "1"}
    cache.put("b", 200, PUBLIC, b"5678")
    cache.get("a")  # now the most recent: "b" goes first
    cache.put("c", 200, PUBLIC, b"9")
    assert cache.get("b") is None and cache.get("a") is not None and cache.get("c") is not None
    cache.put("a", 200, PUBLIC, b"12")  # replaced, not counted twice
    assert cache._size == 3
    cache.put("big", 200, PUBLIC, b"x" * (lean.CACHE_ENTRY_BYTES + 1))
    cache.put("private", 200, {"Cache-Control": "private"}, b"x")
    assert cache.get("big") is None and cache.get("private") is None
    clock.now = lean.CACHE_TTL_S
    assert cache.get("a") is None and cache._size == 1


def test_the_top_document_is_never_blocked_but_frames_and_matches_are():
    rules = Lean(types=("image",), block_urls=("*://ads.example/*", "*/log?*"))
    assert not rules.blocked(Request("https://ads.example/x", "document", top=True))
    assert rules.blocked(Request("https://ads.example/frame", "document"))
    assert rules.blocked(Request("https://site.example/a.png", "image"))
    assert rules.blocked(Request("https://site.example/log?x=1", "xhr"))
    assert not rules.blocked(Request("https://site.example/app.js"))
    assert Lean(cache_urls=("*/js/*",)).cacheable(Request(STATIC))
    assert not Lean(cache_urls=("*/js/*",)).cacheable(Request(STATIC, method="POST"))
    assert not Lean().routes() and Lean(cache_urls=("x",)).routes()
    assert lean.of(("font",)) == Lean(types=("font",)) and lean.of(rules) is rules


async def test_the_router_aborts_serves_from_cache_fetches_into_it_or_continues():
    cache = AssetCache()
    handler = lean.router(Lean(("image",), ("*/ping*",), ("*/js/*",), cache=cache))
    blocked = Route(Request("https://x.example/a.png", "image"))
    kept = Route(Request("https://x.example/"))
    await handler(blocked)
    await handler(kept)
    assert blocked.done == ("abort", None) and kept.done == ("continue",)
    first, second = Route(Request(STATIC)), Route(Request(STATIC))
    await handler(first)
    await handler(second)
    assert first.fetches == 1 and first.done[1]["body"] == b"body"
    # The miss is fulfilled as the hit is: a decoded body has no encoding or wire length.
    assert first.done[1]["headers"] == {"Cache-Control": "public, max-age=3600", "X-Kept": "1"}
    # A public file's visitor cookie reaches the try that downloaded it, never a later one.
    cookie = {"Cache-Control": "public, max-age=60", "Set-Cookie": "cf=1"}
    own = Route(Request(STATIC + "?c"), Response(headers=cookie))
    later = Route(Request(STATIC + "?c"))
    await handler(own)
    await handler(later)
    assert own.done[1]["headers"]["Set-Cookie"] == "cf=1" and later.fetches == 0
    assert "Set-Cookie" not in later.done[1]["headers"]
    assert second.fetches == 0 and second.done[1]["body"] == b"body"
    assert second.done[1]["status"] == 200 and "Content-Encoding" not in second.done[1]["headers"]
    uncached = Route(Request(STATIC + "?v=2"), Response(headers={"Cache-Control": "no-store"}))
    await handler(uncached)
    assert uncached.fetches == 1 and cache.get(STATIC + "?v=2") is None


def test_patterns_and_wait_are_checked():
    assert lean.check_patterns(["*/a/*"], "p") == ("*/a/*",)
    for bad in ("*/a/*", ["*/a/*", "*/a/*"], ["has space"], [""], ["x"] * 201):
        with pytest.raises(ValueError):
            lean.check_patterns(bad, "p")
    assert lean.check_wait("commit", "w") == "commit"
    with pytest.raises(ValueError):
        lean.check_wait("networkidle", "w")
    assert lean.check_bool(False, "c") is False
    for bad in ("false", 0, None):
        with pytest.raises(ValueError):
            lean.check_bool(bad, "c")


def test_a_target_overrides_each_rule_of_the_browser_defaults():
    browser = replace(OPTIONS, block_urls=("*/ads/*",), cache_urls=("*/js/*",))
    assert lean_rules(browser, SETTINGS) == Lean(("image", "media"), ("*/ads/*",), ("*/js/*",))
    cache = AssetCache()
    assert lean_rules(browser, SETTINGS, cache).cache is cache
    own = replace(SETTINGS, block_resources=(), block_urls=(), wait_until="commit")
    assert lean_rules(browser, own) == Lean((), (), ("*/js/*",), "commit")
    assert lean_rules(browser, SETTINGS).chrome_services is True
    quiet = lean_rules(browser, replace(SETTINGS, chrome_services=False))
    assert quiet.chrome_services is False and not Lean(chrome_services=False).routes()


async def test_a_failed_fetch_aborts_the_request_and_caches_nothing():
    cache = AssetCache()
    route = Route(Request(STATIC), ConnectionResetError("reset"))
    await lean.router(Lean(cache_urls=("*/js/*",), cache=cache))(route)
    assert route.done == ("abort", None) and cache.get(STATIC) is None


async def test_each_network_has_its_own_cache_and_a_context_without_one_shares_nothing():
    rules = Lean(cache_urls=("*/js/*",))
    first = network_cache("net-a", "residential")
    assert network_cache("net-a", "residential") is first
    await lean.router(replace(rules, cache=first))(Route(Request(STATIC)))
    other = Route(Request(STATIC))
    await lean.router(replace(rules, cache=network_cache("net-b", "residential")))(other)
    assert other.fetches == 1
    alone = Route(Request(STATIC))
    await lean.router(rules)(alone)
    assert alone.fetches == 1


async def test_at_most_fetches_downloads_run_at_once_per_network():
    cache, running, peak = AssetCache(), 0, 0

    class Slow(Route):
        async def fetch(self) -> Response:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1
            return Response(headers={"Cache-Control": "no-store"})

    handler = lean.router(Lean(cache_urls=("*",), cache=cache))
    await asyncio.gather(*(handler(Slow(Request(f"{STATIC}?{i}"))) for i in range(20)))
    assert peak == lean.FETCHES


class SlowRoute(Route):
    """A route whose download takes a moment, so other tries ask for it meanwhile."""

    async def fetch(self) -> Response:
        await asyncio.sleep(0.01)
        return await Route.fetch(self)


async def test_tries_starting_together_download_each_file_once():
    handler = lean.router(Lean(cache_urls=("*",), cache=AssetCache()))
    routes = [SlowRoute(Request(STATIC)) for _ in range(5)]
    await asyncio.gather(*(handler(route) for route in routes))
    assert sum(route.fetches for route in routes) == 1
    assert all(route.done[0] == "fulfill" and route.done[1]["body"] == b"body" for route in routes)


@pytest.mark.parametrize(
    "first",
    [ConnectionResetError("reset"), Response(headers={"Cache-Control": "no-store"})],
    ids=["failed", "not_cacheable"],
)
async def test_a_failed_or_uncacheable_download_leaves_each_waiting_try_its_own(first):
    cache = AssetCache()
    handler = lean.router(Lean(cache_urls=("*",), cache=cache))
    leader = SlowRoute(Request(STATIC), first)
    waiters = [SlowRoute(Request(STATIC)) for _ in range(3)]
    await asyncio.gather(handler(leader), *(handler(route) for route in waiters))
    assert leader.fetches == 1 and all(route.fetches == 1 for route in waiters)
    assert all(route.done[0] == "fulfill" for route in waiters) and cache.pending == {}
