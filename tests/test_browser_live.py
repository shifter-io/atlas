"""The real pinned Camoufox against tests/browser_site.py through its authenticated proxy.

Remote only (scripts/ci/browser_live.sh on Linux and on the pool's Mac class): it needs
`--extra browser` and the fetched release in PROXY_INDEX_BROWSER_DIR, and skips anywhere else.
"""

from __future__ import annotations

import asyncio
import functools
import os
import subprocess
from dataclasses import replace

import pytest
from browser_site import (
    OUTCOME_PAGES,
    PASSWORD,
    TLS_HOST,
    USERNAME,
    SiteTarget,
    site_and_proxy,
)

from proxy_index.browser import driver, release
from proxy_index.browser.pool import BrowserPool
from proxy_index.browser.settings import BrowserOptions, TargetSettings
from proxy_index.browser.transport import BrowserTransport
from proxy_index.models import ProxyConfig, RequestContext, RunConfig
from proxy_index.runner import run
from proxy_index.sinks import JsonSink
from proxy_index.targets import TargetRegistry

pytestmark = [pytest.mark.browser_live, pytest.mark.timeout(600)]
pytest.importorskip("camoufox")

OPTIONS = BrowserOptions(
    browsers_per_host=3,
    recycle_after=10,
    page_timeout_s=5,
    headless=True,
    block_resources=("image",),
    screenshots="failures",
    locale="en-US",
)
SETTINGS = TargetSettings(id="browser_site", tries=30, every_minutes=60, inputs=("ok",))


class Records:
    def __init__(self) -> None:
        self.tries: list[dict] = []

    def try_done(self, record: dict, screenshot: bytes | None) -> None:
        self.tries.append(record)


@pytest.fixture
def executable():
    dest = os.environ.get("PROXY_INDEX_BROWSER_DIR")
    if not dest:
        pytest.skip("set PROXY_INDEX_BROWSER_DIR (scripts/ci/browser_live.sh)")
    return release.executable(dest)


def make_pool(executable, size: int) -> BrowserPool:
    launch = functools.partial(driver.launch, executable, locale="en-US", headless=True)
    return BrowserPool(size, launch, recycle_after=OPTIONS.recycle_after)


def config(port: int, password: str = PASSWORD, **fields) -> RunConfig:
    proxy = ProxyConfig("http", "127.0.0.1", port, USERNAME, password)
    return RunConfig(target="browser_site", proxy=proxy, provider="p", product="q", **fields)


async def tries(pool, port, inputs, *, block=("image",), password=PASSWORD):
    records = Records()
    settings = replace(SETTINGS, inputs=tuple(inputs), block_resources=block)
    factory = BrowserTransport.factory(pool, records, browser=OPTIONS, settings=settings)
    async with factory(config(port, password)) as transport:
        outcomes = [
            await transport.execute(SiteTarget(), context=RequestContext("r", i, 0))
            for i in range(len(inputs))
        ]
    return outcomes, records.tries


async def test_each_outcome_page_gives_its_verdict(executable):
    async with site_and_proxy() as (site, proxy, _, port), make_pool(executable, 2) as pool:
        outcomes, records = await tries(pool, port, list(OUTCOME_PAGES))
        async with pool.browser() as browser:
            identity = browser.identity
    print(f"camoufox {identity['release']} engine {identity['engine_version']}")
    print(f"user agent {identity['user_agent']}")
    for name, outcome, record in zip(OUTCOME_PAGES, outcomes, records, strict=True):
        verdict = f"{outcome.verdict}/{outcome.error}"
        print(f"{name}: {verdict} {record['reason']} {record['timings_ms']}")
    assert [(o.verdict, o.error) for o in outcomes] == list(OUTCOME_PAGES.values())
    major = int(identity["engine_version"].split(".")[0])
    assert f"Firefox/{major}." in identity["user_agent"]
    assert proxy.forwarded and (TLS_HOST, True) in proxy.connects
    assert site.images == 0
    ok = records[0]
    assert all(ok["timings_ms"][key] is not None for key in ok["timings_ms"])
    assert ok["bytes_down_est"] > 0


async def test_images_load_when_not_blocked_and_bad_credentials_are_proxy_auth(executable):
    async with site_and_proxy() as (site, proxy, _, port), make_pool(executable, 1) as pool:
        outcomes, _ = await tries(pool, port, ["ok"], block=())
        assert outcomes[0].verdict == "ok" and site.images == 1
        outcomes, records = await tries(pool, port, ["ok"], password="wrong")
    print(f"wrong credentials: {records[0]['status']} {records[0]['reason']}")
    # Firefox gives up on refused credentials with NS_ERROR_PROXY_CONNECTION_REFUSED, which
    # K-BP2 maps to proxy_connect; only a 407 shown as the page is proxy_auth.
    assert (outcomes[0].verdict, outcomes[0].error) == ("proxy_error", "proxy_connect")
    assert records[0]["reason"] == "ns_error_proxy_connection_refused"


def leftover(executable) -> str:
    """Processes running from the release's directory (the browser and its children)."""
    install = str(executable).split(release.RELEASE)[0] + release.RELEASE
    found = subprocess.run(["pgrep", "-f", install], capture_output=True, text=True)
    return found.stdout.strip()


async def test_a_pool_of_three_runs_thirty_tries_and_leaks_no_process(executable, tmp_path):
    registry = TargetRegistry()
    registry.register(SiteTarget())
    async with site_and_proxy() as (_, proxy, _, port):
        async with make_pool(executable, 3) as pool:
            records = Records()
            # A fresh browser's first loads can take seconds: a generous page timeout here.
            settings = replace(SETTINGS, timeout_s=20)
            factory = BrowserTransport.factory(pool, records, browser=OPTIONS, settings=settings)
            summary = await run(
                config(port, requests=30, concurrency=6),
                JsonSink(tmp_path),
                targets=registry,
                transport_factory=factory,
            )
            stats = pool.stats()
    print(f"30 tries: {summary.successes} ok, latency {summary.latency_ms}, pool {stats}")
    for record in records.tries:
        if record["error"]:
            print(f"try {record['i']}: {record['error']} {record['reason']} {record['timings_ms']}")
    assert summary.completed == 30 and summary.successes == 30
    # Leases spread unevenly over the slots, so the recycle count varies (30 tries on 3 slots
    # reach 10 on one at least). Every launch past the first three replaces a recycled browser;
    # a browser recycled by the last lease of its slot is never replaced.
    assert stats["leases"] == 30 and stats["crashes"] == 0 and stats["recycles"] >= 1
    assert 3 <= stats["launches"] <= 3 + stats["recycles"]
    for _ in range(50):
        if not leftover(executable):
            break
        await asyncio.sleep(0.1)
    assert leftover(executable) == ""
