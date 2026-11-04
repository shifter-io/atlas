"""Camoufox over Playwright: the only module that imports either (D7; docs/browser.html, Runner).

launch() starts one pinned browser process and checks its user agent; Browser.context() opens
a fresh context per try with the try's proxy and resource blocking and yields a Page, the
PageDriver the targets act through. A failed navigation raises NavigationError with the
browser's error name, which the transport maps with contracts.classify_navigation_error.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
from collections.abc import AsyncIterator, Collection
from pathlib import Path
from typing import Any

from ..errors import UserError
from ..models import ProxyConfig
from . import contracts, release

ERROR_NAME = re.compile(r"\b((?:NS_ERROR|SEC_ERROR|SSL_ERROR)_[A-Z0-9_]+)\b")
VERSION = re.compile(r"\d+(\.\d+)*")


class NavigationError(Exception):
    """A navigation that failed; `name` is the browser's error name, None when it gave none."""

    def __init__(self, name: str | None) -> None:
        super().__init__(name or "navigation failed")
        self.name = name


class UserAgentRefused(UserError):
    """D7: the user agent's Firefox version cannot be made equal to the engine's major."""


def host_os() -> str:
    """The fingerprint OS of this host: the OS it really is (macos or linux)."""
    return "macos" if sys.platform == "darwin" else "linux"


def error_name(exc: BaseException) -> str | None:
    """Playwright's timeouts are `TimeoutError`; network failures carry a Firefox error name."""
    if type(exc).__name__ == "TimeoutError":
        return "TimeoutError"
    match = ERROR_NAME.search(str(exc))
    return match.group(1) if match else None


def proxy_option(proxy: ProxyConfig) -> dict[str, str]:
    """Playwright's proxy option; credentials only travel to the browser, never into a log."""
    host = f"[{proxy.host}]" if ":" in proxy.host else proxy.host
    option = {"server": f"{proxy.scheme}://{host}:{proxy.port}"}
    if proxy.username is not None:
        option |= {"username": proxy.username, "password": proxy.password}
    return option


class Page:
    """One try's page: what a target may do to it (targets.PageDriver), plus the summed body
    size of every response its context saw (`bytes_down_est`)."""

    def __init__(self, context: Any, page: Any) -> None:
        self._page = page
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
        self.identity: dict[str, str] = {}

    def alive(self) -> bool:
        return self._browser.is_connected()

    @contextlib.asynccontextmanager
    async def context(
        self, proxy: ProxyConfig | None, block: Collection[str] = (), timeout_ms: int = 30_000
    ) -> AsyncIterator[Page]:
        """A fresh context through `proxy` that aborts requests of the `block` resource types,
        with `timeout_ms` as every action's default timeout; closed however the try ends."""
        options: dict[str, Any] = {"proxy": proxy_option(proxy)} if proxy else {}
        if self._user_agent is not None:
            options["user_agent"] = self._user_agent
        context = await self._browser.new_context(**options)
        page = None
        try:
            context.set_default_timeout(timeout_ms)
            if block:
                await context.route("**/*", _blocker(frozenset(block)))
            page = Page(context, await context.new_page())
            yield page
        finally:
            if page is not None:
                await page._drop_sizes()
            await context.close()

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
    proxy). Refuses (UserAgentRefused) when the UA cannot match the engine."""
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
