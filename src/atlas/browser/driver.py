"""Camoufox and Chrome over Playwright: the only module that imports either (D7; docs/browser.html,
Runner).

launch() starts one pinned Camoufox process and checks its user agent; Browser.context() opens
a fresh context per try with the try's proxy, resource blocking and the run country's place
(geo: locale, languages, time zone, geolocation) and yields a Page, the PageDriver the targets
act through. launch_chrome() detects the host's Google Chrome instead;
its ChromeBrowser.context() starts a new Chrome with a fresh profile and fingerprint per try.
A failed navigation raises NavigationError with the browser's error name, which the transport
maps with contracts.classify_navigation_error.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import UserError
from ..models import ProxyConfig
from . import background, chrome, contracts, geo, release
from .pool import CLOSE_S

# Firefox's error names, or Chrome's `net::ERR_*` (the name is the part after `net::`).
ERROR_NAME = re.compile(
    r"\b((?:NS_ERROR|SEC_ERROR|SSL_ERROR)_[A-Z0-9_]+)\b|\bnet::(ERR_[A-Z0-9_]+)\b"
)
PROFILE_PREFIX = "atlas-chrome-"
VERSION = re.compile(r"\d+(\.\d+)*")


class NavigationError(Exception):
    """A navigation that failed; `name` is the browser's error name, None when it gave none."""

    def __init__(self, name: str | None) -> None:
        super().__init__(name or "navigation failed")
        self.name = name


class BrowserHung(Exception):
    """A try's context did not close within pool.CLOSE_S: its browser is abandoned."""


class UserAgentRefused(UserError):
    """D7: the user agent's version cannot be made equal to the engine's major."""


def host_os() -> str:
    """The fingerprint OS of this host: the OS it really is (macos or linux)."""
    return "macos" if sys.platform == "darwin" else "linux"


def error_name(exc: BaseException) -> str | None:
    """Playwright's timeouts are `TimeoutError`; network failures carry a Firefox error name
    or Chrome's `net::ERR_*`."""
    if type(exc).__name__ == "TimeoutError":
        return "TimeoutError"
    match = ERROR_NAME.search(str(exc))
    return (match.group(1) or match.group(2)) if match else None


def proxy_option(proxy: ProxyConfig) -> dict[str, str]:
    """Playwright's proxy option; credentials only travel to the browser, never into a log."""
    host = f"[{proxy.host}]" if ":" in proxy.host else proxy.host
    option = {"server": f"{proxy.scheme}://{host}:{proxy.port}"}
    if proxy.username is not None:
        option |= {"username": proxy.username, "password": proxy.password}
    return option


class Page:
    """One try's page: what a target may do to it (targets.PageDriver), plus the summed body
    size of every response its context saw (`bytes_down_est`). A Chrome page carries its try's
    `fingerprint` (chrome.Fingerprint.record()); a Camoufox page carries None."""

    def __init__(self, context: Any, page: Any) -> None:
        self._page = page
        self.fingerprint: dict[str, Any] | None = None
        self._sizes: list[asyncio.Future] = []
        context.on("requestfinished", self._finished)

    def _finished(self, request: Any) -> None:
        self._sizes.append(asyncio.ensure_future(request.sizes()))

    async def bytes_down_est(self) -> int:
        sizes = await asyncio.gather(*self._sizes, return_exceptions=True)
        return sum(max(s.get("responseBodySize", 0), 0) for s in sizes if isinstance(s, dict))

    async def _drop_sizes(self) -> None:
        """Before the context closes: no size lookup outlives it or goes unretrieved."""
        for future in self._sizes:
            future.cancel()
        await asyncio.gather(*self._sizes, return_exceptions=True)

    async def goto(self, url: str, timeout_ms: int) -> int | None:
        try:
            response = await self._page.goto(url, timeout=timeout_ms)
        except Exception as exc:
            raise NavigationError(error_name(exc)) from None
        return response.status if response is not None else None

    async def fill(self, selector: str, value: str) -> None:
        await self._page.fill(selector, value)

    async def press(self, selector: str, key: str) -> None:
        await self._page.press(selector, key)

    async def click(self, selector: str) -> None:
        await self._page.click(selector)

    async def wait_for(self, selector: str, timeout_ms: int) -> bool:
        try:
            await self._page.wait_for_selector(selector, timeout=timeout_ms, state="attached")
        except Exception as exc:
            if error_name(exc) != "TimeoutError":
                raise
            return False
        return True

    async def count(self, selector: str) -> int:
        return await self._page.locator(selector).count()

    async def text(self, selector: str) -> str:
        locator = self._page.locator(selector)
        return await locator.first.inner_text() if await locator.count() else ""

    async def url(self) -> str:
        return self._page.url

    async def title(self) -> str:
        return await self._page.title()

    async def screenshot(self) -> bytes:
        return await self._page.screenshot(type="jpeg", quality=contracts.SCREENSHOT_QUALITY)

    async def evaluate(self, expression: str) -> Any:
        return await self._page.evaluate(expression)


class Browser:
    """One Camoufox process and its Playwright driver; `identity` is the manifest's camoufox
    block (release, engine_version, user_agent) once launch() has checked it."""

    def __init__(self, playwright: Any, browser: Any) -> None:
        self._playwright, self._browser = playwright, browser
        self._user_agent: str | None = None
        self._seed, self._tries = str(uuid.uuid4()), 0
        self.identity: dict[str, str] = {}
        self._abandoned = False

    def alive(self) -> bool:
        return not self._abandoned and self._browser.is_connected()

    def abandon(self) -> None:
        """Hung: never leased again; the pool closes it when its lease ends."""
        self._abandoned = True

    @contextlib.asynccontextmanager
    async def context(
        self,
        proxy: ProxyConfig | None,
        block: Collection[str] = (),
        timeout_ms: int = 30_000,
        *,
        country: str | None = None,
        seed: str | None = None,
    ) -> AsyncIterator[Page]:
        """A fresh context through `proxy` that aborts requests of the `block` resource types,
        with `timeout_ms` as every action's default timeout; closed however the try ends.
        With a `country`, its place is drawn from `seed` (default: this handle's seed and try
        count); without one, the launch's `[browser] locale` stays."""
        options: dict[str, Any] = {"proxy": proxy_option(proxy)} if proxy else {}
        if self._user_agent is not None:
            options["user_agent"] = self._user_agent
        if country is not None:
            if seed is None:
                seed, self._tries = f"{self._seed}:{self._tries}", self._tries + 1
            options |= geo.place(geo.seeded(seed), country).camoufox_options()
        context = await self._browser.new_context(**options)
        async with _try_page(self, context, context.new_page, block, timeout_ms) as page:
            yield page

    async def _user_agent_seen(self) -> str:
        async with self.context(None) as page:
            return await page.evaluate("navigator.userAgent")

    async def check_user_agent(self, os_name: str) -> None:
        """D7: the UA's Firefox major must be the engine's; override it once, else refuse."""
        engine = VERSION.match(self._browser.version or "")
        if engine is None:
            raise UserAgentRefused("the browser reported no engine version")
        engine_version, major = engine.group(), contracts.engine_major(engine.group())
        seen = await self._user_agent_seen()
        if contracts.user_agent_major(seen) != major:
            self._user_agent = contracts.user_agent(engine_version, os_name)
            seen = await self._user_agent_seen()
            if contracts.user_agent_major(seen) != major:
                raise UserAgentRefused(f"the user agent cannot claim Firefox {major}")
        self.identity = {
            "release": release.RELEASE,
            "engine_version": engine_version,
            "user_agent": seen,
        }

    async def close(self) -> None:
        try:
            await self._browser.close()
        finally:
            await self._playwright.stop()


@contextlib.asynccontextmanager
async def _try_page(
    owner: Browser | ChromeBrowser,
    context: Any,
    new_page: Callable[[], Awaitable[Any]],
    block: Collection[str],
    timeout_ms: int,
) -> AsyncIterator[Page]:
    """The try's Page on `context`: default timeout and resource blocking set, the context
    closed however the try ends, each closing step for at most CLOSE_S (_closing)."""
    page = None
    try:
        context.set_default_timeout(timeout_ms)
        if block:
            await context.route("**/*", _blocker(frozenset(block)))
        page = Page(context, await new_page())
        yield page
    finally:
        try:
            if page is not None:
                await _closing(owner, page._drop_sizes())
        finally:
            await _closing(owner, context.close())


async def _closing(owner: Browser | ChromeBrowser, step: Awaitable[Any]) -> None:
    """A closing step, also of a cancelled try: a hung browser never answers, so past CLOSE_S
    the owner is abandoned (the pool replaces it) and BrowserHung raised."""
    try:
        async with asyncio.timeout(CLOSE_S):
            await step
    except TimeoutError:
        owner.abandon()
        raise BrowserHung("the browser did not close the try's context") from None


def _blocker(blocked: frozenset[str]):
    async def route(route: Any) -> None:
        if route.request.resource_type in blocked:
            await route.abort()
        else:
            await route.continue_()

    return route


async def launch(
    executable: str | Path, *, locale: str, headless: bool, os_name: str | None = None
) -> Browser:
    """Start the pinned browser (release.executable) with Camoufox's launch options: the
    fingerprint OS is the host's, no geoip lookup (it would send a request outside the
    proxy), its background services off and a browser-wide proxy that refuses everything
    (background.py), so only a try's context, through its own proxy, reaches the network.
    Refuses (UserAgentRefused) when the UA cannot match the engine."""
    from camoufox.async_api import AsyncNewBrowser
    from playwright.async_api import async_playwright

    os_name = os_name or host_os()
    playwright = await async_playwright().start()
    handle = None
    try:
        browser = await AsyncNewBrowser(
            playwright,
            executable_path=str(executable),
            os=os_name,
            locale=locale,
            headless=headless,
            geoip=False,
            proxy=proxy_option(background.NO_NETWORK),
            firefox_user_prefs=dict(background.CAMOUFOX_PREFS),
        )
        handle = Browser(playwright, browser)
        await handle.check_user_agent(os_name)
        return handle
    except BaseException:
        if handle is not None:
            await handle.close()
        else:
            await playwright.stop()
        raise


# navigator.userAgentData is missing outside secure contexts: then no brands.
CLAIMS = "() => [navigator.userAgent, (navigator.userAgentData || {brands: []}).brands]"
# The secure context CLAIMS is read in (about:blank is not one): a page fulfilled locally under
# the reserved .invalid domain, so neither the network nor the try's proxy ever sees it.
CHECK_URL = "https://user-agent-check.invalid/"
CHECK_PAGE = "<!doctype html><title>user agent check</title>"


@dataclass(frozen=True, slots=True)
class ChromeSetup:
    """What every try of one ChromeBrowser shares: the detected executable and version, the
    run's country, headless, the seed its tries' fingerprints are drawn from, the host OS."""

    executable: str
    version: str
    country: str
    headless: bool
    seed: str
    os_name: str


class ChromeBrowser:
    """The host's Google Chrome: one Playwright driver, and a new Chrome process with a fresh
    profile and fingerprint per try. `identity` is {engine, version, user_agent} once
    launch_chrome() has checked it."""

    def __init__(self, playwright: Any, setup: ChromeSetup) -> None:
        self._playwright, self.setup = playwright, setup
        self._tries, self._closed = 0, False
        self.identity: dict[str, str] = {}

    def alive(self) -> bool:
        return not self._closed

    def abandon(self) -> None:
        """Hung: never leased again; the pool's close stops its driver and the hung Chrome."""
        self._closed = True

    @contextlib.asynccontextmanager
    async def context(
        self,
        proxy: ProxyConfig | None,
        block: Collection[str] = (),
        timeout_ms: int = 30_000,
        *,
        country: str | None = None,
        seed: str | None = None,
    ) -> AsyncIterator[Page]:
        """Chrome on a new temporary profile (mode 700, deleted however the try ends) through
        `proxy`, with the fingerprint for `country` (default: the launch's) drawn from `seed`
        (default: the launch's seed and this handle's try count) and `block` resource types
        aborted."""
        if seed is None:
            seed, self._tries = f"{self.setup.seed}:{self._tries}", self._tries + 1
        fingerprint = chrome.fingerprint(seed, country or self.setup.country)
        options = fingerprint.context_options() | ({"proxy": proxy_option(proxy)} if proxy else {})
        profile = tempfile.mkdtemp(prefix=PROFILE_PREFIX)
        try:
            context = await self._playwright.chromium.launch_persistent_context(
                profile,
                executable_path=self.setup.executable,
                headless=self.setup.headless,
                **options,
            )

            async def first_page() -> Any:
                await context.add_init_script(fingerprint.init_script())
                page = context.pages[0] if context.pages else await context.new_page()
                await self._agree(context, page, fingerprint)
                return page

            async with _try_page(self, context, first_page, block, timeout_ms) as page:
                page.fingerprint = fingerprint.record()
                yield page
        finally:
            shutil.rmtree(profile, ignore_errors=True)

    def _agrees(self, claims: Any) -> bool:
        major = int(self.setup.version.split(".")[0])
        user_agent, brands = claims if isinstance(claims, list) and len(claims) == 2 else ("", [])
        return chrome.user_agent_major(user_agent) == chrome.brands_major(brands) == major

    async def _agree(self, context: Any, page: Any, fingerprint: chrome.Fingerprint) -> None:
        """D7: the user agent and the brands claim the detected major on CHECK_URL; a headless
        Chrome is made to (and its Sec-CH-UA headers with it) through CDP, else
        UserAgentRefused."""
        await page.route(CHECK_URL, _fulfil_check)
        try:
            await self._agree_on_check(context, page, fingerprint)
        finally:
            await page.unroute(CHECK_URL)

    async def _agree_on_check(
        self, context: Any, page: Any, fingerprint: chrome.Fingerprint
    ) -> None:
        if self._agrees(await _check_claims(page)):
            return
        session = await context.new_cdp_session(page)
        version, os_name = self.setup.version, self.setup.os_name
        override = {
            "userAgent": chrome.user_agent(version, os_name),
            "acceptLanguage": fingerprint.languages,
            "userAgentMetadata": chrome.user_agent_metadata(version, os_name),
        }
        await session.send("Emulation.setUserAgentOverride", override)
        if not self._agrees(await _check_claims(page)):
            raise UserAgentRefused(f"the user agent cannot claim Chrome {version.split('.')[0]}")

    async def close(self) -> None:
        self._closed = True
        await self._playwright.stop()


async def _fulfil_check(route: Any) -> None:
    await route.fulfill(status=200, content_type="text/html", body=CHECK_PAGE)


async def _check_claims(page: Any) -> Any:
    """CLAIMS on a fresh load of CHECK_URL (page-routed by _agree), a secure context."""
    await page.goto(CHECK_URL)
    return await page.evaluate(CLAIMS)


async def launch_chrome(
    executable: str | Path = chrome.DEFAULT_EXECUTABLE,
    *,
    country: str,
    headless: bool,
    seed: str | None = None,
    os_name: str | None = None,
) -> ChromeBrowser:
    """The host's Chrome at `executable`: refused (chrome.ChromeRefused) when missing or too
    old, and (UserAgentRefused) when a first profile cannot claim its version. That profile's
    Chrome has no network (background.NO_NETWORK): its check page is fulfilled locally."""
    from playwright.async_api import async_playwright

    version = await asyncio.to_thread(chrome.detect, executable)
    chrome.fingerprint("", country)  # refuses a country without a table entry
    setup = ChromeSetup(
        str(executable), version, country, headless, seed or str(uuid.uuid4()), os_name or host_os()
    )
    handle = ChromeBrowser(await async_playwright().start(), setup)
    try:
        async with handle.context(background.NO_NETWORK, seed=f"{setup.seed}:launch") as page:
            seen = await page.evaluate("navigator.userAgent")
        handle.identity = {"engine": "chrome", "version": version, "user_agent": seen}
        return handle
    except BaseException:
        await handle.close()
        raise
