"""browser.lean: a try's traffic rules, its route handler and its response collector."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from browser_transport_cases import OPTIONS, SETTINGS

from atlas.browser import asset_cache, lean
from atlas.browser.asset_cache import AssetCache
from atlas.browser.lean import Lean
from atlas.browser.transport import lean_rules

PUBLIC = {"cache-control": "public, max-age=3600", "content-encoding": "br", "x-kept": "1"}
STATIC = "https://static.example/js/app.js"


class Frame:
    def __init__(self, parent: object = None) -> None:
        self.parent_frame = parent


class Request:
    def __init__(self, url, kind="script", method="GET", top=False, headers=None) -> None:
        self.url, self.resource_type, self.method = url, kind, method
        self.headers = headers or {}
        self.frame = Frame(None if top or kind != "document" else Frame())
        self._navigation = kind == "document"

    def is_navigation_request(self) -> bool:
        return self._navigation


class Response:
    def __init__(self, request, status=200, headers=None, body=b"body", fails=False) -> None:
        self.request, self.url, self.status = request, request.url, status
        self.headers, self._body, self.fails = PUBLIC if headers is None else headers, body, fails

    async def all_headers(self) -> dict:
        return self.headers

    async def body(self) -> bytes:
        if self.fails:
            raise RuntimeError("Target page, context or browser has been closed")
        return self._body


class Route:
    def __init__(self, request: Request) -> None:
        self.request = request
        self.done: tuple = ()

    async def abort(self, error_code: str | None = None) -> None:
        self.done = ("abort", error_code)

    async def continue_(self) -> None:
        self.done = ("continue",)

    async def fulfill(self, **response) -> None:
        self.done = ("fulfill", response)


async def collect(rules: Lean, *responses: Response) -> None:
    on_response = lean.collector(rules)
    for response in responses:
        on_response(response)
    await asyncio.gather(*lean.READS)


def test_the_top_document_is_never_blocked_but_frames_and_matches_are():
    rules = Lean(types=("image",), block_urls=("*://ads.example/*", "*/log?*"))
    assert not rules.blocked(Request("https://ads.example/x", "document", top=True))
    assert rules.blocked(Request("https://ads.example/frame", "document"))
    assert rules.blocked(Request("https://site.example/a.png", "image"))
    assert rules.blocked(Request("https://site.example/log?x=1", "xhr"))
    assert not rules.blocked(Request("https://site.example/app.js"))
    assert not Lean().routes() and Lean(cache_urls=("x",)).routes()
    assert lean.of(("font",)) == Lean(types=("font",)) and lean.of(rules) is rules


@pytest.mark.parametrize(
    ("request_", "cacheable"),
    [
        (Request(STATIC), True),
        (Request(STATIC, method="POST"), False),
        (Request("https://static.example/css/a.css"), False),
        (Request("http://static.example/js/app.js"), False),
        (Request("https://user:pw@static.example/js/app.js"), False),
        (Request(STATIC, headers={"Authorization": "Bearer x"}), False),
        (Request(STATIC, headers={"range": "bytes=0-9"}), False),
    ],
)
def test_only_a_matching_credential_free_get_is_cacheable(request_, cacheable):
    rules = Lean(cache_urls=("*/js/*", "http://static.example/*", "*@static.example/*"))
    assert rules.cacheable(request_) is cacheable


async def test_a_miss_loads_in_the_browser_and_its_public_answer_serves_the_next_try():
    rules = Lean(("image",), ("*/ping*",), ("*/js/*",), cache=AssetCache())
    handler = lean.router(rules)
    blocked, kept = Route(Request("https://x.example/a.png", "image")), Route(Request(STATIC))
    await handler(blocked)
    await handler(kept)
    # Nothing is downloaded for the cache: a miss goes on to the network as without it.
    assert blocked.done == ("abort", None) and kept.done == ("continue",)
    await collect(rules, Response(kept.request))
    later = Route(Request(STATIC))
    await handler(later)
    assert later.done[0] == "fulfill" and later.done[1]["body"] == b"body"
    assert later.done[1]["headers"] == {"cache-control": "public, max-age=3600", "x-kept": "1"}


async def test_the_collector_keeps_only_cacheable_new_public_answers():
    cache = AssetCache()
    rules = Lean(cache_urls=("*/js/*",), cache=cache)
    other = Request("https://static.example/other.js")
    closed = Request("https://static.example/js/closed.js")
    private = Request("https://static.example/js/private.js")
    moved = Request("https://static.example/js/moved.js")
    await collect(
        rules,
        Response(other),
        Response(closed, fails=True),
        Response(private, headers={"cache-control": "private, max-age=60"}),
        Response(moved, status=301),
    )
    assert all(cache.get(r.url) is None for r in (other, closed, private, moved))
    await collect(rules, Response(Request(STATIC)))
    await collect(rules, Response(Request(STATIC), body=b"never read"))  # already kept
    assert cache.get(STATIC).body == b"body"
    await collect(Lean(cache_urls=("*",)), Response(Request(STATIC)))  # no cache: nothing


def test_patterns_and_wait_are_checked():
    assert lean.check_patterns(["*/a/*"], "p") == ("*/a/*",)
    for bad in ("*/a/*", ["*/a/*", "*/a/*"], ["has space"], [""], ["x"] * 201):
        with pytest.raises(ValueError):
            lean.check_patterns(bad, "p")
    assert lean.check_wait("commit", "w") == "commit"
    with pytest.raises(ValueError):
        lean.check_wait("networkidle", "w")


def test_a_target_overrides_each_rule_of_the_browser_defaults():
    browser = replace(OPTIONS, block_urls=("*/ads/*",), cache_urls=("*/js/*",))
    assert lean_rules(browser, SETTINGS) == Lean(("image", "media"), ("*/ads/*",), ("*/js/*",))
    own = replace(SETTINGS, block_resources=(), block_urls=(), wait_until="commit")
    assert lean_rules(browser, own) == Lean((), (), ("*/js/*",), "commit")
    cache = AssetCache()
    assert lean_rules(browser, SETTINGS, cache).cache is cache


def test_each_network_has_its_own_cache():
    asset_cache.configure(None, 1)
    first = asset_cache.network_cache("net-a", "residential")
    assert asset_cache.network_cache("net-a", "residential") is first
    assert asset_cache.network_cache("net-b", "residential") is not first
    assert first.max_age_s == 3600 and first.directory is None
    asset_cache.configure(None, 0.25)
