"""BrowserTransport over a fake driver and the real pool: outcomes, timings, recorder, budget."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, replace
from typing import Any

import pytest
from browser_site import SiteTarget

from proxy_index.browser import transport as transport_module
from proxy_index.browser.contracts import FIREFOX_ERROR_PREFIXES, FIREFOX_ERRORS, TIMINGS
from proxy_index.browser.driver import NavigationError, UserAgentRefused
from proxy_index.browser.pool import BrowserPool
from proxy_index.browser.settings import BrowserOptions, TargetSettings
from proxy_index.browser.targets.pages import GoogleSearch
from proxy_index.browser.transport import CONSENT_ACCEPT, BrowserTransport, NullRecorder
from proxy_index.models import ProxyConfig, RequestContext, RunConfig
from proxy_index.runner import run
from proxy_index.sinks import JsonSink
from proxy_index.targets import TargetRegistry

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
        self.contexts: list[tuple[Any, tuple[str, ...]]] = []
        self.open_contexts = 0

    def alive(self) -> bool:
        return self.connected

    @contextlib.asynccontextmanager
    async def context(self, proxy, block=()):
        self.contexts.append((proxy, tuple(block)))
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


async def test_a_successful_try_is_ok_with_timings_bytes_and_one_record():
    harness = Harness()
    outcome = await harness.execute()
    assert outcome.verdict == "ok" and outcome.error is None and outcome.status == 200
    assert outcome.bytes_down == 4321 and outcome.bytes_exact is False
    [(record, shot)] = harness.recorder.tries
    assert shot is None and record["screenshot"] is None
    assert record["verdict"] == "ok" and record["input"] == "ok" and record["source"] is None
    assert set(record["timings_ms"]) == set(TIMINGS)
    assert all(v is not None for v in record["timings_ms"].values())
    assert record["final_url"] == "http://site.invalid/ok" and record["bytes_down_est"] == 4321
    browser = harness.browsers[0]
    assert browser.contexts == [(HTTP_PROXY, ("image", "media"))]
    assert browser.gotos == [("http://site.invalid/ok", 20_000)] and browser.open_contexts == 0


NAMES = [*FIREFOX_ERRORS, *(prefix + "X" for prefix, _ in FIREFOX_ERROR_PREFIXES), "OTHER", None]


@pytest.mark.parametrize("name", NAMES)
async def test_every_navigation_error_name_maps_by_k_bp2(name):
    harness = Harness(Script(error=NavigationError(name), url="about:blank"))
    outcome = await harness.execute()
    expected = dict(FIREFOX_ERRORS).get(name) or next(
        (code for prefix, code in FIREFOX_ERROR_PREFIXES if (name or "").startswith(prefix)),
        "browser_error",
    )
    assert outcome.error == expected
    [(record, shot)] = harness.recorder.tries
    assert record["error"] == expected and record["final_url"] is None and shot == b"jpeg"
    assert record["reason"] == ((name or "").lower() or "navigation_error")
    assert record["screenshot"] == "browser/screenshots/0000.jpg"


@pytest.mark.parametrize(
    ("policy", "fails", "taken"),
    [
        ("failures", True, True),
        ("failures", False, False),
        ("all", False, True),
        ("all", True, True),
        ("none", True, False),
    ],
)
async def test_screenshot_policy(policy, fails, taken):
    script = Script(found=0 if fails else 1)
    harness = Harness(script, options=replace(OPTIONS, screenshots=policy))
    outcome = await harness.execute(index=7)
    assert (outcome.error == "no_content") is fails
    [(record, shot)] = harness.recorder.tries
    assert (shot == b"jpeg") is taken
    assert record["screenshot"] == ("browser/screenshots/0007.jpg" if taken else None)


async def test_a_failed_screenshot_is_skipped():
    harness = Harness(Script(found=0, shot_error=True))
    await harness.execute()
    [(record, shot)] = harness.recorder.tries
    assert shot is None and record["screenshot"] is None


async def test_queue_wait_is_in_timings_not_in_latency():
    hold = asyncio.Event()
    harness = Harness(Script(hold=hold))
    first = asyncio.create_task(harness.execute(index=0))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(harness.execute(index=1))
    await asyncio.sleep(0.25)
    hold.set()
    await first
    outcome = await second
    record = harness.recorder.tries[1][0]
    assert record["timings_ms"]["queue"] >= 200
    assert outcome.latency_ms < 100
    assert outcome.latency_ms == record["timings_ms"]["navigate"] + record["timings_ms"]["settle"]


async def test_socks5_with_credentials_is_refused_before_any_traffic(caplog):
    socks = ProxyConfig("socks5", "192.0.2.10", 1080, "user", "pass")
    transport_module._SOCKS_LOGGED.clear()
    harness = Harness(proxy=socks)
    with caplog.at_level(logging.WARNING):
        for index in range(2):
            outcome = await harness.execute(index=index)
            assert (outcome.verdict, outcome.error) == ("proxy_error", "proxy_connect")
    assert harness.browsers == [] and harness.pool.stats()["leases"] == 0
    assert [t[0]["reason"] for t in harness.recorder.tries] == ["socks5_auth_unsupported"] * 2
    assert len([r for r in caplog.records if "SOCKS5" in r.getMessage()]) == 1
    assert "user" not in caplog.text and "pass" not in caplog.text


async def test_socks5_without_credentials_is_used():
    socks = ProxyConfig("socks5", "192.0.2.10", 1080)
    harness = Harness(proxy=socks)
    assert (await harness.execute()).verdict == "ok"


async def test_the_deadline_cancels_an_in_flight_try():
    settings = replace(SETTINGS, timeout_s=1)
    harness = Harness(Script(delay=30), settings=settings, deadline=time.time() - 0.9)
    began = time.perf_counter()
    outcome = await harness.execute()
    assert time.perf_counter() - began < 5
    assert (outcome.verdict, outcome.error) == ("timeout", "timeout")
    assert harness.recorder.tries[0][0]["reason"] == "deadline"
    assert harness.browsers[0].open_contexts == 0 and harness.pool._idle


async def test_a_crash_is_browser_error_and_the_pool_relaunches():
    harness = Harness(Script(error=RuntimeError("Target closed"), crash=True))
    outcome = await harness.execute()
    assert (outcome.verdict, outcome.error) == ("error", "browser_error")
    assert harness.recorder.tries[0][0]["reason"] == "exception"
    harness.script.crash, harness.script.error = False, None
    assert (await harness.execute(index=1)).verdict == "ok"
    assert len(harness.browsers) == 2 and harness.browsers[0].closed
    assert harness.pool.stats()["crashes"] == 1


class Raising(SiteTarget):
    async def interact(self, driver, spec) -> None:
        raise TimeoutError("an interact wait")


async def test_the_context_is_closed_when_the_target_raises():
    harness = Harness()
    outcome = await harness.execute(Raising())
    assert outcome.error == "browser_error"
    assert harness.browsers[0].open_contexts == 0 and harness.browsers[0].alive()


async def test_a_refused_user_agent_stops_the_run():
    harness = Harness()

    async def refuse():
        raise UserAgentRefused("no")

    harness.pool._launch = refuse
    with pytest.raises(UserAgentRefused):
        await harness.execute()


GOOGLE = replace(SETTINGS, id="google_search", inputs=("pizza",))


async def test_a_google_consent_wall_is_accepted_once():
    script = Script(url="https://consent.google.com/ml", found={GoogleSearch.RESULTS: 3})
    harness = Harness(script, settings=GOOGLE)
    outcome = await harness.execute(GoogleSearch())
    assert outcome.verdict == "ok" and harness.browsers[0].clicks == [CONSENT_ACCEPT]
    assert harness.recorder.tries[0][0]["final_url"] == "https://www.google.com/search"


@pytest.mark.parametrize(("results", "clicks"), [(3, 0), (0, 1)])
async def test_a_consent_form_is_accepted_only_when_it_hides_the_results(results, clicks):
    found = {GoogleSearch.RESULTS: results, GoogleSearch.CONSENT_FORM: 1}
    harness = Harness(Script(url="https://www.google.com/search", found=found), settings=GOOGLE)
    await harness.execute(GoogleSearch())
    assert len(harness.browsers[0].clicks) == clicks


async def test_target_overrides_win_over_browser_defaults():
    settings = replace(SETTINGS, timeout_s=3, block_resources=())
    harness = Harness(settings=settings)
    await harness.execute()
    assert harness.browsers[0].contexts == [(HTTP_PROXY, ())]
    assert harness.browsers[0].gotos[0][1] == 3000


async def test_runs_through_the_runner_like_an_http_run(tmp_path):
    harness = Harness(Script(found=0), size=2)
    registry = TargetRegistry()
    registry.register(SiteTarget())
    factory = BrowserTransport.factory(
        harness.pool, harness.recorder, browser=OPTIONS, settings=SETTINGS
    )
    config = replace(harness.config, requests=6, concurrency=3)
    summary = await run(config, JsonSink(tmp_path), targets=registry, transport_factory=factory)
    assert summary.completed == 6 and summary.successes == 0
    assert summary.errors == {"no_content": 6}
    assert sorted(t[0]["i"] for t in harness.recorder.tries) == list(range(6))


async def test_without_a_context_the_transport_numbers_its_tries():
    harness = Harness()
    async with harness.transport as transport:
        for _ in range(2):
            await transport.execute(SiteTarget())
    assert [t[0]["i"] for t in harness.recorder.tries] == [0, 1]
    NullRecorder().try_done({}, None)
