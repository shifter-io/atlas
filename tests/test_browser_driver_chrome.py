"""browser.driver's Chrome engine over a fake Playwright and a fake Chrome: CI has no Chrome."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import types
from pathlib import Path

import pytest

from atlas.browser import background, chrome, driver, geo
from atlas.browser.chrome import ChromeRefused
from atlas.browser.driver import (
    ChromeBrowser,
    ChromeSetup,
    NavigationError,
    Page,
    UserAgentRefused,
)
from atlas.models import ProxyConfig

VERSION = f"{chrome.CHROME_MIN_MAJOR}.0.7922.34"
MAJOR = str(chrome.CHROME_MIN_MAJOR)
REAL = [chrome.user_agent(VERSION, "macos"), [{"brand": "Google Chrome", "version": MAJOR}]]
HEADLESS = [REAL[0].replace("Chrome/", "HeadlessChrome/"), [{"brand": "HeadlessChrome"}]]


class CDPSession:
    def __init__(self, chromium: Chromium) -> None:
        self.chromium = chromium

    async def send(self, method, params):
        self.chromium.overrides.append((method, params))
        if self.chromium.override_works:
            self.chromium.claims = list(REAL)


class Route:
    def __init__(self) -> None:
        self.fulfilled: dict | None = None

    async def fulfill(self, **response):
        self.fulfilled = response


class PWPage:
    """Like a real Chrome: about:blank is no secure context, so it has no userAgentData."""

    url = "about:blank"

    def __init__(self, chromium: Chromium) -> None:
        self.chromium = chromium
        self.routes: dict = {}
        self.loads: list[str] = []

    async def evaluate(self, expression):
        claims = self.chromium.claims
        if expression != driver.CLAIMS:
            return claims[0]
        if isinstance(claims, list) and not self.url.startswith("https://"):
            return [claims[0], []]
        return claims

    async def route(self, url, handler):
        self.routes[url] = handler

    async def unroute(self, url):
        del self.routes[url]

    async def goto(self, url, timeout=None):  # noqa: ASYNC109 - Playwright's signature
        if url not in self.routes:  # the network: the proxy here
            raise RuntimeError(f"page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at {url}")
        route = Route()
        await self.routes[url](route)
        assert route.fulfilled is not None
        self.url = url
        self.loads.append(url)


class PersistentContext:
    def __init__(self, chromium: Chromium, profile: str, options: dict) -> None:
        self.chromium, self.profile, self.options = chromium, profile, options
        self.pages = [PWPage(chromium)] if chromium.default_page else []
        self.opened = list(self.pages)
        self.scripts: list[str] = []
        self.routes: list = []
        self.closed = False

    def on(self, event, handler):
        pass

    def set_default_timeout(self, timeout):
        self.timeout = timeout

    async def route(self, pattern, handler):
        self.routes.append(pattern)

    async def add_init_script(self, script):
        self.scripts.append(script)

    async def new_page(self):
        self.opened.append(PWPage(self.chromium))
        return self.opened[-1]

    async def new_cdp_session(self, page):
        return CDPSession(self.chromium)

    async def close(self):
        self.closed = True


class Chromium:
    def __init__(self, claims=REAL, *, override_works=True, fail=False) -> None:
        self.claims, self.override_works, self.fail = list(claims), override_works, fail
        self.contexts: list[PersistentContext] = []
        self.overrides: list = []
        self.started: list[Path] = []
        self.default_page = True

    async def launch_persistent_context(self, user_data_dir, **options):
        mode = stat.S_IMODE(os.stat(user_data_dir).st_mode)
        assert mode == 0o700 and Path(user_data_dir).parent == Path(tempfile.gettempdir())
        self.started.append(Path(user_data_dir))
        if self.fail:
            raise RuntimeError("Chrome crashed at start")
        self.contexts.append(PersistentContext(self, user_data_dir, options))
        return self.contexts[-1]


class Playwright:
    def __init__(self, chromium: Chromium) -> None:
        self.chromium, self.stopped = chromium, False

    async def stop(self):
        self.stopped = True


def handle(chromium: Chromium, **setup) -> ChromeBrowser:
    options = {"executable": "/x/chrome", "version": VERSION, "country": "US"}
    options |= {"headless": True, "seed": "run", "os_name": "macos"} | setup
    return ChromeBrowser(Playwright(chromium), ChromeSetup(**options))


def profiles(chromium: Chromium) -> list[Path]:
    """The profile directories a try left behind."""
    return [path for path in chromium.started if path.exists()]


async def test_the_runs_country_overrides_the_launchs():
    chromium = Chromium()
    async with handle(chromium, country="DE").context(None, country="us", seed="s") as page:
        assert page.fingerprint == chrome.fingerprint("s", "US").record()
    options = chromium.contexts[0].options
    assert options["locale"] == "en-US" and options["timezone_id"] in geo.country("US").zones
    assert options["geolocation"] == geo.geolocation(options["timezone_id"])


async def test_three_tries_three_profiles_none_left():
    chromium = Chromium()
    browser = handle(chromium)
    proxy = ProxyConfig("http", "192.0.2.1", 8080, "u", "p")
    async with browser.context(proxy, ("image",), 5000) as page:
        assert isinstance(page, Page) and profiles(chromium) == chromium.started
        assert page.fingerprint == chrome.fingerprint("run:0", "US").record()
    with pytest.raises(NavigationError) as caught:
        async with browser.context(None) as page:
            await page.goto("https://x/", 1000)
    assert caught.value.name == "ERR_TUNNEL_CONNECTION_FAILED"
    chromium.default_page = False
    async with browser.context(None, seed="run-b:4") as page:
        assert page.fingerprint == chrome.fingerprint("run-b:4", "US").record()
    assert len(set(chromium.started)) == 3 and profiles(chromium) == []
    assert all(c.closed for c in chromium.contexts) and chromium.overrides == []
    first = chromium.contexts[0]
    fingerprint = chrome.fingerprint("run:0", "US")
    assert first.options == fingerprint.context_options() | {
        "executable_path": "/x/chrome",
        "headless": True,
        "proxy": driver.proxy_option(proxy),
    }
    assert first.options["geolocation"] == geo.geolocation(fingerprint.timezone_id)
    assert first.scripts == [fingerprint.init_script()] and first.routes == ["**/*"]
    assert first.timeout == 5000 and chromium.contexts[1].routes == []
    assert browser.alive()
    await browser.close()
    assert not browser.alive() and browser._playwright.stopped


async def test_a_crashed_start_leaves_no_profile():
    chromium = Chromium(fail=True)
    with pytest.raises(RuntimeError):
        async with handle(chromium).context(None):
            pass
    assert len(chromium.started) == 1 and profiles(chromium) == []


async def test_the_check_reads_a_local_secure_page_never_the_network():
    chromium = Chromium()
    proxy = ProxyConfig("http", "192.0.2.1", 8080, "u", "p")
    async with handle(chromium).context(proxy, ("document", "image")) as page:
        [checked] = chromium.contexts[0].opened
        assert checked.loads == [driver.CHECK_URL] and checked.routes == {}
        with pytest.raises(NavigationError):  # the check page's route is gone
            await page.goto(driver.CHECK_URL, 1000)
    assert driver.CHECK_URL.startswith("https://") and ".invalid/" in driver.CHECK_URL
    assert chromium.overrides == []


async def test_headless_is_made_to_agree_through_cdp():
    chromium = Chromium(HEADLESS)
    async with handle(chromium).context(None) as page:
        assert await page.evaluate("navigator.userAgent") == REAL[0]
        assert chromium.contexts[0].opened[0].loads == [driver.CHECK_URL] * 2
    [(method, params)] = chromium.overrides
    assert method == "Emulation.setUserAgentOverride"
    assert params["userAgent"] == REAL[0] and "platform" not in params
    assert params["acceptLanguage"] == "en-US,en"  # Chrome adds the weights itself
    assert params["userAgentMetadata"] == chrome.user_agent_metadata(VERSION, "macos")


@pytest.mark.parametrize("claims", [HEADLESS, [REAL[0], []], "not a list"])
async def test_a_user_agent_that_cannot_agree_is_refused(claims):
    chromium = Chromium(claims, override_works=False)
    with pytest.raises(UserAgentRefused, match=f"Chrome {MAJOR}"):
        async with handle(chromium).context(None):
            pass
    assert chromium.contexts[0].closed and profiles(chromium) == []


def fake_playwright(monkeypatch, chromium: Chromium) -> Playwright:
    playwright = Playwright(chromium)

    class Starter:
        async def start(self):
            return playwright

    api = types.ModuleType("playwright.async_api")
    api.async_playwright = Starter
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", api)
    return playwright


def fake_chrome(tmp_path, version: str) -> Path:
    path = tmp_path / "Google Chrome"
    path.write_text(f"#!/bin/sh\necho 'Google Chrome {version}'\n")
    path.chmod(0o755)
    return path


async def test_launch_chrome_detects_and_checks_on_a_first_profile(monkeypatch, tmp_path):
    chromium = Chromium(HEADLESS)
    playwright = fake_playwright(monkeypatch, chromium)
    executable = fake_chrome(tmp_path, VERSION)
    browser = await driver.launch_chrome(executable, country="US", headless=True, seed="s")
    assert browser.identity == {"engine": "chrome", "version": VERSION, "user_agent": REAL[0]}
    assert browser.setup.executable == str(executable) and browser.setup.os_name == driver.host_os()
    assert len(chromium.contexts) == 1 and profiles(chromium) == []
    # The check's Chrome has a proxy that refuses everything; its page is fulfilled locally.
    check = chromium.contexts[0]
    assert check.options["proxy"] == driver.proxy_option(background.NO_NETWORK)
    assert set(check.opened[0].loads) == {driver.CHECK_URL}
    async with browser.context(None) as page:  # the launch's check counted no try
        assert page.fingerprint == chrome.fingerprint("s:0", "US").record()
    await browser.close()
    assert playwright.stopped


async def test_launch_chrome_refusals_leave_nothing_running(monkeypatch, tmp_path):
    playwright = fake_playwright(monkeypatch, Chromium(HEADLESS, override_works=False))
    with pytest.raises(ChromeRefused):
        await driver.launch_chrome(tmp_path / "absent", country="US", headless=True)
    with pytest.raises(ChromeRefused, match="'ZZ'"):
        await driver.launch_chrome(fake_chrome(tmp_path, VERSION), country="ZZ", headless=False)
    assert not playwright.stopped  # nothing was started
    with pytest.raises(UserAgentRefused):
        await driver.launch_chrome(fake_chrome(tmp_path, VERSION), country="US", headless=True)
    assert playwright.stopped
