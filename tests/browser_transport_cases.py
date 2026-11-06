"""The fakes behind the BrowserTransport tests: a scripted page and browser, a recorder of tries
and a harness that runs the transport over the real pool. test_browser_transport.py holds the
tests; test_browser_capture.py and browser_node_cases.py reuse the fakes."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any

from browser_site import SiteTarget

from proxy_index.browser.pool import BrowserPool
from proxy_index.browser.settings import BrowserOptions, TargetSettings
from proxy_index.browser.transport import BrowserTransport
from proxy_index.models import ProxyConfig, RequestContext, RunConfig

HTTP_PROXY = ProxyConfig("http", "192.0.2.10", 8080, "user", "pass")


@dataclass
class Script:
    """What the fake page does: goto raises or returns status; `hold` delays goto."""

    status: int | None = 200
    error: BaseException | None = None
    hold: asyncio.Event | None = None
    delay: float = 0
    title: str = "Product"
    text: str = "Real content"
    found: int | dict[str, int] = 1
    url: str = "http://site.invalid/ok"
    crash: bool = False
    shot_error: bool = False


class FakePage:
    def __init__(self, browser: FakeBrowser, script: Script) -> None:
        self.browser, self.script = browser, script

    async def goto(self, url: str, timeout_ms: int) -> int | None:
        self.browser.gotos.append((url, timeout_ms))
        if self.script.hold is not None:
            await self.script.hold.wait()
        await asyncio.sleep(self.script.delay)
        if self.script.crash:
            self.browser.connected = False
        if self.script.error is not None:
            raise self.script.error
        return self.script.status

    async def click(self, selector: str) -> None:
        self.browser.clicks.append(selector)
        self.script.url = "https://www.google.com/search"

    async def wait_for(self, selector: str, timeout_ms: int) -> bool:
        return True

    async def count(self, selector: str) -> int:
        found = self.script.found
        return found.get(selector, 0) if isinstance(found, dict) else found

    async def text(self, selector: str) -> str:
        return self.script.text

    async def url(self) -> str:
        return self.script.url

    async def title(self) -> str:
        return self.script.title

    async def screenshot(self) -> bytes:
        if self.script.shot_error:
            raise RuntimeError("no shot")
        return b"jpeg"

    async def bytes_down_est(self) -> int:
        return 4321


class FakeBrowser:
    def __init__(self, script: Script) -> None:
        self.script = script
        self.connected = True
        self.closed = False
        self.gotos: list[tuple[str, int]] = []
        self.clicks: list[str] = []
        self.timeouts: list[int] = []
        self.contexts: list[tuple[Any, tuple[str, ...]]] = []
        self.open_contexts = 0

    def alive(self) -> bool:
        return self.connected

    @contextlib.asynccontextmanager
    async def context(self, proxy, block=(), timeout_ms=30_000):
        self.contexts.append((proxy, tuple(block)))
        self.timeouts.append(timeout_ms)
        self.open_contexts += 1
        try:
            yield FakePage(self, self.script)
        finally:
            self.open_contexts -= 1

    async def close(self) -> None:
        self.closed = True


class Recorder:
    def __init__(self) -> None:
        self.tries: list[tuple[dict, bytes | None]] = []

    def try_done(self, record: dict, screenshot: bytes | None) -> None:
        self.tries.append((record, screenshot))


OPTIONS = BrowserOptions(
    browsers_per_host=2,
    recycle_after=50,
    page_timeout_s=20,
    headless=True,
    block_resources=("image", "media"),
    screenshots="failures",
    locale="en-US",
)
SETTINGS = TargetSettings(id="browser_site", tries=5, every_minutes=60, inputs=("ok",))


class Harness:
    def __init__(self, script=None, *, proxy=HTTP_PROXY, size=1, options=OPTIONS, **kw):
        self.script = script or Script()
        self.browsers: list[FakeBrowser] = []
        self.pool = BrowserPool(size, self.launch, recycle_after=50, memory=lambda: None)
        self.recorder = Recorder()
        self.config = RunConfig(target="browser_site", proxy=proxy, provider="p", product="q")
        settings = kw.pop("settings", SETTINGS)
        factory = BrowserTransport.factory(
            self.pool, self.recorder, browser=options, settings=settings, **kw
        )
        self.transport = factory(self.config)

    async def launch(self) -> FakeBrowser:
        self.browsers.append(FakeBrowser(self.script))
        return self.browsers[-1]

    async def execute(self, target=None, index=0):
        context = RequestContext("r", index, 0)
        return await self.transport.execute(target or SiteTarget(), context=context)
