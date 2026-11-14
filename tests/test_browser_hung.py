"""A hung browser never holds a host (IDLE-TIMEOUT-1): a try past its own longest time plus
[browser] idle_timeout_s is a timeout and its browser is replaced, a context or browser that
never closes is given up after pool.CLOSE_S, and a run whose counters stand still for
[browser] stall_minutes is stopped and reported 'stalled'."""

import asyncio
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestServer
from browser_node_cases import (
    SHIFTER,
    TOKENS,
    Fleet,
    Orchestrator,
    browser_work,
    host_view,
    make_state,
    serve_until,
)
from browser_transport_cases import OPTIONS, SETTINGS, Harness, Script
from test_browser_driver import PWBrowser, PWContext, PWDriver

from atlas.browser import driver
from atlas.browser import pool as pool_module
from atlas.browser.driver import Browser, BrowserHung, ChromeBrowser
from atlas.browser.node import BrowserHost
from atlas.browser.pool import BrowserPool
from atlas.browser.run import StallWatch


class Forever:
    """A step that never returns, as a hung Chrome's."""

    async def wait(self) -> None:
        await asyncio.Event().wait()


async def test_a_hung_page_is_a_timeout_and_its_browser_is_replaced():
    options = replace(OPTIONS, idle_timeout_s=0.3)
    settings = replace(SETTINGS, timeout_s=1)
    harness = Harness(Script(hold=asyncio.Event()), settings=settings, options=options)
    outcome = await asyncio.wait_for(harness.execute(), 5)
    assert (outcome.verdict, outcome.error) == ("timeout", "timeout")
    assert harness.recorder.tries[0][0]["reason"] == "idle"
    # The run goes on: the next try gets a new browser, the hung one is closed.
    harness.script.hold = None
    assert (await harness.execute(index=1)).verdict == "ok"
    assert len(harness.browsers) == 2 and harness.browsers[0].closed
    assert harness.pool.stats()["crashes"] == 1


class HungContext(PWContext):
    async def close(self):
        await Forever().wait()


class HungBrowser(PWBrowser):
    async def new_context(self, **options):
        self.contexts.append(HungContext(self, options))
        return self.contexts[-1]


async def test_a_context_that_never_closes_abandons_its_browser(monkeypatch):
    monkeypatch.setattr(driver, "CLOSE_S", 0.1)
    browser = Browser(PWDriver(), HungBrowser())
    with pytest.raises(BrowserHung):
        async with asyncio.timeout(5), browser.context(None):
            pass
    assert not browser.alive()
    chrome = ChromeBrowser(PWDriver(), setup=None)
    chrome.abandon()
    assert not chrome.alive()


async def test_a_browser_that_never_closes_is_given_up(monkeypatch):
    monkeypatch.setattr(pool_module, "CLOSE_S", 0.1)

    class Stuck:
        def alive(self):
            return False

        async def close(self):
            await Forever().wait()

    async def launch():
        return Stuck()

    pool = BrowserPool(1, launch, recycle_after=50, memory=lambda: None)
    async with asyncio.timeout(5):
        async with pool.browser():
            pass
    assert pool.stats()["crashes"] == 1 and pool._idle


def test_the_stall_watch_fires_once_when_the_counters_stand_still():
    now = [0.0]
    watch = StallWatch(60, clock=lambda: now[0])
    assert not watch.seen(1, {"requests": 3})
    now[0] = 59
    assert not watch.seen(1, {"requests": 3}) and not watch.stalled(1)
    now[0] = 61
    assert watch.seen(1, {"requests": 3}) and watch.stalled(1)
    assert not watch.seen(1, {"requests": 3})  # once
    now[0] = 200
    assert not watch.seen(2, {"requests": 3})  # each run its own
    watch.forget(1)
    assert not watch.stalled(1) and not watch.seen(1, {"requests": 3})


async def test_a_run_without_progress_is_stopped_and_reported_stalled(tmp_path):
    state = make_state(tmp_path)
    fake = Orchestrator({TOKENS[SHIFTER]: [browser_work(1, "amazon_pdp")]})
    server = TestServer(fake.app)
    await server.start_server()
    fleet = Fleet()
    fleet.script.hold = asyncio.Event()  # every try hangs: the counters never move
    host = BrowserHost(
        host_view(state),
        state,
        str(server.make_url("")),
        launch=fleet.launch,
        poll_interval=0.02,
        heartbeat_interval=0.05,
        progress_interval=0.05,
    )
    for agent in host.agents:
        agent.watch.limit_s = 0.3
    try:
        await serve_until(host, fake, lambda: 1 in fake.failed)
    finally:
        await server.close()
    assert fake.failed == {1: (TOKENS[SHIFTER], "stalled")} and fake.results == {}
