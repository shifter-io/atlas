"""browser.driver over fake Playwright objects: no browser is installed in CI."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from atlas.browser import background, driver, geo, release
from atlas.browser.driver import Browser, NavigationError, Page, UserAgentRefused
from atlas.browser.geo import GeoRefused
from atlas.models import ProxyConfig

REFUSED = "NS_ERROR_PROXY_CONNECTION_REFUSED"
FAILED = "ERR_PROXY_CONNECTION_FAILED"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:{0}.0) Gecko/20100101 Firefox/{0}.0"


class TimeoutError(Exception):  # Playwright's timeout class has this name
    pass


class Response:
    status = 201


class Locator:
    def __init__(self, matches: int) -> None:
        self.matches = matches
        self.first = self

    async def count(self) -> int:
        return self.matches

    async def inner_text(self) -> str:
        return "Visible text"


class Frame:
    parent_frame = None


class Request:
    def __init__(self, resource_type: str = "image", size: object = 10) -> None:
        self.resource_type, self.size = resource_type, size
        self.url, self.method, self.frame = "https://example.com/x", "GET", Frame()

    def is_navigation_request(self) -> bool:
        return self.resource_type == "document"

    async def sizes(self):
        if isinstance(self.size, Exception):
            raise self.size
        if self.size is None:
            await asyncio.Event().wait()  # never answers
        return {"responseBodySize": self.size}


class Route:
    def __init__(self, resource_type: str) -> None:
        self.request = Request(resource_type)
        self.done = ""

    async def abort(self, error_code=None) -> None:
        self.done = "abort"

    async def continue_(self) -> None:
        self.done = "continue"


class PWPage:
    url = "https://example.com/x"

    def __init__(self, browser: PWBrowser) -> None:
        self.browser = browser
        self.calls: list[tuple] = []

    async def goto(self, url, timeout, wait_until):  # noqa: ASYNC109 - Playwright's signature
        self.calls.append(("goto", wait_until))
        if self.browser.goto_error:
            raise self.browser.goto_error
        return Response() if url != "about:none" else None

    async def fill(self, selector, value):
        self.calls.append(("fill", selector, value))

    async def press(self, selector, key):
        self.calls.append(("press", selector, key))

    async def click(self, selector):
        self.calls.append(("click", selector))

    async def wait_for_selector(self, selector, timeout, state):  # noqa: ASYNC109
        if self.browser.wait_error:
            raise self.browser.wait_error

    def locator(self, selector):
        return Locator(0 if selector == "none" else 2)

    async def title(self):
        return "Title"

    async def screenshot(self, type, quality):
        return f"{type}:{quality}".encode()

    async def evaluate(self, expression):
        return self.browser.user_agents.pop(0)


class PWContext:
    def __init__(self, browser: PWBrowser, options: dict) -> None:
        self.browser, self.options = browser, options
        self.handlers: dict = {}
        self.routes: list = []

    def on(self, event, handler):
        self.handlers[event] = handler

    def set_default_timeout(self, timeout):
        self.timeout = timeout

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def new_page(self):
        return PWPage(self.browser)

    async def close(self):
        self.browser.closed_contexts += 1


class PWBrowser:
    def __init__(self, version="158.0.1", user_agents=()) -> None:
        self.version = version
        self.user_agents = list(user_agents)
        self.contexts: list[PWContext] = []
        self.closed_contexts = 0
        self.goto_error: Exception | None = None
        self.wait_error: Exception | None = None
        self.closed = False

    async def new_context(self, **options):
        self.contexts.append(PWContext(self, options))
        return self.contexts[-1]

    def is_connected(self):
        return not self.closed

    async def close(self):
        self.closed = True


class PWDriver:
    stopped = False

    async def stop(self):
        self.stopped = True


@pytest.mark.parametrize(
    ("exc", "name"),
    [
        (TimeoutError("Timeout 30000ms exceeded"), "TimeoutError"),
        (RuntimeError(f"page.goto: {REFUSED} at x"), REFUSED),
        (RuntimeError("SSL_ERROR_RX_RECORD_TOO_LONG"), "SSL_ERROR_RX_RECORD_TOO_LONG"),
        (RuntimeError(f"page.goto: net::{FAILED} at x"), FAILED),
        (RuntimeError("net::ERR_CERT_DATE_INVALID"), "ERR_CERT_DATE_INVALID"),
        (RuntimeError("ERR_NAME_NOT_RESOLVED without net::"), None),
        (RuntimeError("Target closed"), None),
    ],
)
def test_error_name(exc, name):
    assert driver.error_name(exc) == name


def test_proxy_option():
    assert driver.proxy_option(ProxyConfig("http", "192.0.2.1", 8080, "u", "p")) == {
        "server": "http://192.0.2.1:8080",
        "username": "u",
        "password": "p",
    }
    assert driver.proxy_option(ProxyConfig("socks5", "2001:db8::1", 1080)) == {
        "server": "socks5://[2001:db8::1]:1080"
    }


def test_host_os(monkeypatch):
    monkeypatch.setattr(driver.sys, "platform", "darwin")
    assert driver.host_os() == "macos"
    monkeypatch.setattr(driver.sys, "platform", "linux")
    assert driver.host_os() == "linux"


async def test_page_acts_on_the_playwright_page():
    browser = PWBrowser()
    context = PWContext(browser, {})
    page = Page(context, PWPage(browser))
    assert await page.goto("https://example.com/", 1000) == 201
    assert await page.goto("about:none", 1000) is None
    await page.fill("#q", "x")
    await page.press("#q", "Enter")
    await page.click("#go")
    assert await page.wait_for("#a", 10) is True
    assert await page.count("#a") == 2
    assert await page.text("body") == "Visible text" and await page.text("none") == ""
    assert await page.url() == "https://example.com/x" and await page.title() == "Title"
    assert await page.screenshot() == b"jpeg:60"
    for request in (Request(size=100), Request(size=-1), Request(size=OSError("gone"))):
        context.handlers["requestfinished"](request)
    assert await page.bytes_down_est() == 100


async def test_page_navigation_and_wait_failures():
    browser = PWBrowser()
    page = Page(PWContext(browser, {}), PWPage(browser))
    browser.goto_error = RuntimeError("NS_ERROR_UNKNOWN_HOST")
    with pytest.raises(NavigationError) as caught:
        await page.goto("https://example.com/", 1000)
    assert caught.value.name == "NS_ERROR_UNKNOWN_HOST"
    browser.wait_error = TimeoutError("waited")
    assert await page.wait_for("#a", 10) is False
    browser.wait_error = RuntimeError("closed")
    with pytest.raises(RuntimeError):
        await page.wait_for("#a", 10)
    assert str(NavigationError(None)) == "navigation failed"


async def test_context_options_blocking_and_close_on_every_path():
    pw = PWBrowser()
    browser = Browser(PWDriver(), pw)
    proxy = ProxyConfig("http", "192.0.2.1", 8080, "u", "p")
    with pytest.raises(ValueError):
        async with browser.context(proxy, ("image",), 5000) as page:
            assert isinstance(page, Page)
            pw.contexts[0].handlers["requestfinished"](Request(size=None))
            pending = page._sizes[0]
            raise ValueError("try failed")
    assert pending.cancelled()
    context = pw.contexts[0]
    assert context.options == {"proxy": driver.proxy_option(proxy)} and context.timeout == 5000
    [(pattern, handler)] = context.routes
    routes = [Route("image"), Route("document")]
    for route in routes:
        await handler(route)
    assert [route.done for route in routes] == ["abort", "continue"]
    async with browser.context(None):
        pass
    assert pw.contexts[1].options == {} and pw.contexts[1].routes == []
    assert pw.closed_contexts == 2 and browser.alive()


async def test_a_country_gives_its_locale_zone_and_city_drawn_from_the_seed():
    pw = PWBrowser()
    browser = Browser(PWDriver(), pw)
    for _ in range(12):
        async with browser.context(None, country="us"):
            pass
    async with browser.context(None, country="US", seed="run:5"):
        pass
    zones = set()
    for context in pw.contexts[:-1]:
        options, zone = context.options, context.options["timezone_id"]
        assert options["locale"] == "en-US,en" and zone in geo.country("US").zones
        assert options["geolocation"] == geo.geolocation(zone)
        assert options["extra_http_headers"] == {"Accept-Language": "en-US,en;q=0.9"}
        zones.add(zone)
    assert len(zones) > 1 and browser._tries == 12
    place = geo.place(geo.seeded("run:5"), "US")
    assert pw.contexts[-1].options == place.camoufox_options()
    with pytest.raises(GeoRefused):
        async with browser.context(None, country="zz"):
            pass
    assert len(pw.contexts) == 13 and pw.closed_contexts == 13


@pytest.mark.parametrize(
    ("agents", "override"),
    [([UA.format(158)], False), ([UA.format(135), UA.format(158)], True)],
)
async def test_user_agent_check_passes_or_overrides(agents, override):
    pw = PWBrowser("158.0.1", agents)
    browser = Browser(PWDriver(), pw)
    await browser.check_user_agent("linux")
    assert browser.identity == {
        "release": release.RELEASE,
        "engine_version": "158.0.1",
        "user_agent": UA.format(158),
    }
    assert ("user_agent" in pw.contexts[-1].options) is override


@pytest.mark.parametrize(
    ("version", "agents"), [("158.0.1", [UA.format(135), UA.format(135)]), ("", [])]
)
async def test_user_agent_check_refuses(version, agents):
    browser = Browser(PWDriver(), PWBrowser(version, agents))
    with pytest.raises(UserAgentRefused):
        await browser.check_user_agent("macos")


def fake_modules(monkeypatch, browser: PWBrowser | Exception) -> PWDriver:
    playwright = PWDriver()
    calls = {}

    class Starter:
        async def start(self):
            return playwright

    async def new_browser(pw, **options):
        calls.update(options)
        if isinstance(browser, Exception):
            raise browser
        return browser

    camoufox = types.ModuleType("camoufox.async_api")
    camoufox.AsyncNewBrowser = new_browser
    api = types.ModuleType("playwright.async_api")
    api.async_playwright = Starter
    monkeypatch.setitem(sys.modules, "camoufox", types.ModuleType("camoufox"))
    monkeypatch.setitem(sys.modules, "camoufox.async_api", camoufox)
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", api)
    playwright.calls = calls
    return playwright


async def test_launch_uses_camoufox_options_and_checks_the_user_agent(monkeypatch):
    pw = PWBrowser("158.0.1", [UA.format(158)])
    playwright = fake_modules(monkeypatch, pw)
    browser = await driver.launch("/opt/camoufox-bin", locale="en-US", headless=True)
    assert playwright.calls == {
        "executable_path": "/opt/camoufox-bin",
        "os": driver.host_os(),
        "locale": "en-US",
        "headless": True,
        "geoip": False,
        "proxy": {"server": "http://localhost:9"},
        "firefox_user_prefs": background.CAMOUFOX_PREFS,
    }
    assert playwright.calls["firefox_user_prefs"] is not background.CAMOUFOX_PREFS
    assert browser.identity["user_agent"] == UA.format(158)
    await browser.close()
    assert pw.closed and playwright.stopped


@pytest.mark.parametrize("failure", ["launch", "user_agent"])
async def test_a_failed_launch_leaves_nothing_running(monkeypatch, failure):
    pw = PWBrowser("158.0.1", [UA.format(1), UA.format(1)])
    playwright = fake_modules(monkeypatch, OSError("no") if failure == "launch" else pw)
    with pytest.raises(OSError if failure == "launch" else UserAgentRefused):
        await driver.launch("/opt/camoufox-bin", locale="en-US", headless=True, os_name="linux")
    assert playwright.stopped and (failure == "launch" or pw.closed)


def test_camoufox_starts_with_its_background_services_off():
    prefs = background.CAMOUFOX_PREFS
    off = [
        "media.gmp-manager.updateEnabled",  # Widevine and OpenH264 downloads
        "media.gmp-gmpopenh264.autoupdate",
        "media.gmp-widevinecdm.autoupdate",
        "app.normandy.enabled",
        "network.captive-portal-service.enabled",
        "network.connectivity-service.enabled",
        "app.update.auto",
        "extensions.update.enabled",
        "browser.safebrowsing.malware.enabled",
        "browser.safebrowsing.phishing.enabled",
        "browser.safebrowsing.downloads.enabled",
        "network.proxy.failover_direct",
    ]
    assert all(prefs[name] is False for name in off)
    assert prefs["app.update.disabledForTesting"] is True
    remote = ["media.gmp-manager.url", "services.settings.server", "app.normandy.api_url"]
    remote += ["captivedetect.canonicalURL", "geo.provider.network.url"]
    assert all(prefs[name] == "" or prefs[name].startswith("data:,") for name in remote)
    assert background.NO_NETWORK.host == "localhost" and background.NO_NETWORK.username is None


async def test_lean_rules_route_the_context_and_set_the_navigation_wait():
    from atlas.browser.lean import Lean

    pw = PWBrowser()
    browser = Browser(PWDriver(), pw)
    async with browser.context(None, Lean(wait_until="commit")) as page:
        await page.goto("https://example.com/", 1000)
    assert pw.contexts[0].routes == [] and page._page.calls == [("goto", "commit")]
    async with browser.context(None, Lean(block_urls=("*/log*",))) as page:
        await page.goto("https://example.com/", 1000)
    [(pattern, handler)] = pw.contexts[1].routes
    assert pattern == "**/*" and page._page.calls == [("goto", "load")]
