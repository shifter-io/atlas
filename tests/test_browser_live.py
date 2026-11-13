"""The real pinned Camoufox against tests/browser_site.py through its authenticated proxy.

Remote only (scripts/ci/browser_live.sh on Linux and on the pool's Mac class): it needs
`--extra browser` and the fetched release in ATLAS_BROWSER_DIR, and skips anywhere else.
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from browser_site import (
    OUTCOME_PAGES,
    PASSWORD,
    TLS_HOST,
    USERNAME,
    SiteTarget,
    site_and_proxy,
)

from atlas.browser import driver, release
from atlas.browser.pool import BrowserPool
from atlas.browser.settings import BrowserOptions, TargetSettings
from atlas.browser.transport import BrowserTransport
from atlas.models import ProxyConfig, RequestContext, RunConfig
from atlas.runner import RunOptions, run
from atlas.sinks import JsonSink
from atlas.targets import TargetRegistry

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
    dest = os.environ.get("ATLAS_BROWSER_DIR")
    if not dest:
        pytest.skip("set ATLAS_BROWSER_DIR (scripts/ci/browser_live.sh)")
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
        # The proxy has read the last bytes each try's relay wrote.
        await asyncio.sleep(0.5)
        seen = (proxy.up, proxy.down)
        print(f"proxy saw up {seen[0]} down {seen[1]}")
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
    # BYTES-TRUTH-1: every try's wire bytes, exact, failures included, add up to the proxy's.
    for name, outcome, record in zip(OUTCOME_PAGES, outcomes, records, strict=True):
        estimate = record["bytes_down_est"]
        print(f"{name}: up {outcome.bytes_up} down {outcome.bytes_down} est {estimate}")
    assert all(o.bytes_exact and o.bytes_up > 0 for o in outcomes)
    assert outcomes[0].bytes_down > ok["bytes_down_est"]
    assert (sum(o.bytes_up for o in outcomes), sum(o.bytes_down for o in outcomes)) == seen


async def test_images_load_when_not_blocked_and_refused_credentials_are_proxy_errors(executable):
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
                RunOptions(targets=registry, transport_factory=factory),
            )
        stats = pool.stats()
    print(f"30 tries: {summary.successes} ok, latency {summary.latency_ms}, pool {stats}")
    for record in records.tries:
        if record["error"]:
            print(f"try {record['i']}: {record['error']} {record['reason']} {record['timings_ms']}")
    assert summary.completed == 30 and summary.successes == 30
    # Whatever the lease spread, every launch ends in exactly one close once the pool closed
    # (30 tries on 3 slots reach recycle_after=10 on one slot at least).
    assert stats["leases"] == 30 and stats["crashes"] == 0 and stats["recycles"] >= 1
    assert stats["launches"] == stats["recycles"] + stats["crashes"] + stats["closes"]
    for _ in range(50):
        if not leftover(executable):
            break
        await asyncio.sleep(0.1)
    assert leftover(executable) == ""


CAPTURE = Path(__file__).resolve().parents[1] / "scripts" / "browser_capture.py"


def load_capture():
    spec = importlib.util.spec_from_file_location("browser_capture", CAPTURE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up
    spec.loader.exec_module(module)
    return module


def check_capture(directory: Path, records: list[dict]) -> None:
    documents = [json.loads((directory / f"{i:04d}.json").read_text()) for i in range(3)]
    print(f"capture: {[d['judged'] for d in documents]}")
    assert [r["error"] for r in records] == [None, "no_content", "proxy_connect"]
    assert documents[0]["view"]["found"] == {"#content": 1}
    assert documents[2]["view"] is None
    assert all((directory / f"{i:04d}.jpg").stat().st_size > 0 for i in range(3))
    written = b"".join(p.read_bytes() for p in directory.iterdir())
    assert USERNAME.encode() not in written and PASSWORD.encode() not in written


async def test_a_capture_writes_each_try_and_no_credential(executable, tmp_path):
    capture = load_capture()
    inputs = ("ok", "empty", "refused")
    async with site_and_proxy() as (_, _, _, port):
        plan = capture.Plan(
            target=SiteTarget(),
            entry=replace(SETTINGS, inputs=inputs, timeout_s=20),
            browser=OPTIONS,
            provider="p",
            product="q",
            country="US",
            untargeted=False,
            tries=len(inputs),
            proxy=ProxyConfig("http", "127.0.0.1", port, USERNAME, PASSWORD),
        )
        launch = functools.partial(driver.launch, executable, locale="en-US", headless=True)
        records = await capture.capture(plan, tmp_path, launch, "browser-live")
    check_capture(tmp_path, records)
