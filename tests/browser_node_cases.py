"""Shared helpers of the browser host tests: a state directory of two networks, a fake fleet
of browsers and an in-process fake orchestrator. test_browser_node.py holds the serving host,
test_browser_node_cli.py the browser-node command (--check and serve)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web
from browser_transport_cases import FakeBrowser, Script

from proxy_index.browser import node as host_module
from proxy_index.browser.node import BrowserHost
from proxy_index.browser.settings import load_settings

CONFIGS = Path(__file__).resolve().parents[1] / "configs"
SHIFTER, CHEAP = "shifter/residential", "proxy-cheap/residential"
TOKENS = {SHIFTER: "token-shifter", CHEAP: "token-cheap"}
SETTINGS = """schema = 2
enabled = true

[schedule]
countries = ["US"]
lead_seconds = 0

[browser]
browsers_per_host = 3
recycle_after = 25
page_timeout_s = 20
headless = true
block_resources = ["image"]
screenshots = "none"
locale = "en-US"

[outbox]
dir = "outbox"
max_pending_gb = 1

[[target]]
id = "amazon_pdp"
tries = 3
every_minutes = 60
inputs = ["0306406152"]

[[target]]
id = "google_search"
tries = 2
every_minutes = 30
timeout_s = 15
inputs = ["example query"]

[[target]]
id = "ai_overview"
tries = 1
every_minutes = 60
answer_wait_s = 40
inputs = ["example question"]

[[host]]
name = "browser-1"
networks = ["shifter/residential", "proxy-cheap/residential"]

[[host]]
name = "browser-2"
networks = ["example/residential"]

[networks]
untargeted = ["proxy-cheap/residential"]
"""
CREDENTIALS = """[shifter]
account = "acct"
password = "secret"

[proxy-cheap]
account = "cheapacct"
password = "cheapsecret"
"""


def private(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def make_state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    for network, config in ((SHIFTER, "shifter.toml"), (CHEAP, "proxy-cheap.toml")):
        directory = state / "networks" / network.replace("/", ".")
        directory.mkdir(parents=True)
        private(directory / "provider.toml", (CONFIGS / config).read_text())
        private(directory / "credentials.toml", CREDENTIALS)
        private(directory / "node.token", TOKENS[network] + "\n")
    private(state / "settings.toml", SETTINGS)
    return state


def host_view(state: Path):
    return load_settings(state / "settings.toml").host("browser-1")


class Fleet:
    """Fake browsers for the shared pool; tracks how many tries run at once across them."""

    def __init__(self, delay: float = 0.05) -> None:
        self.script = Script(delay=delay)
        self.browsers: list[FakeBrowser] = []
        self.open = self.peak = 0
        self.closed = 0

    async def launch(self):
        fleet = self

        class Tracked(FakeBrowser):
            @contextlib.asynccontextmanager
            async def context(self, proxy, block=(), timeout_ms=30_000, *, country=None):
                fleet.open += 1
                fleet.peak = max(fleet.peak, fleet.open)
                try:
                    async with FakeBrowser.context(
                        self, proxy, block, timeout_ms, country=country
                    ) as page:
                        yield page
                finally:
                    fleet.open -= 1

            async def close(self):
                fleet.closed += 1
                await FakeBrowser.close(self)

        self.browsers.append(Tracked(self.script))
        return self.browsers[-1]

    def proxies(self):
        return [proxy for browser in self.browsers for proxy, _ in browser.contexts]


class Orchestrator:
    """An in-process fake orchestrator speaking the node API, one queue per token."""

    def __init__(self, work: dict[str, list[dict]]) -> None:
        self.work = work
        self.registered: list[tuple[str, dict]] = []
        self.results: dict[int, tuple[str, dict]] = {}
        self.failed: dict[int, tuple[str, str]] = {}
        self.changed = asyncio.Event()
        self.app = web.Application(client_max_size=64 << 20)
        for path, handler in (
            ("/v1/register", self.register),
            ("/v1/work", self.assign),
            ("/v1/progress", self.ok),
            ("/v1/heartbeat", self.ok),
            ("/v1/results", self.result),
            ("/v1/failed", self.fail),
        ):
            self.app.router.add_route("*", path, handler)

    @staticmethod
    def token(request) -> str:
        return request.headers["Authorization"].removeprefix("Bearer ")

    async def register(self, request):
        self.registered.append((self.token(request), await request.json()))
        return web.json_response({"node_id": len(self.registered), "region": "browser"})

    async def assign(self, request):
        return web.json_response({"assignments": self.work.pop(self.token(request), [])})

    async def ok(self, request):
        return web.json_response({})

    async def result(self, request):
        body = await request.read()
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
            summary = json.load(archive.extractfile("summary.json"))
        self.results[int(request.query["batch_id"])] = (self.token(request), summary)
        self.changed.set()
        return web.json_response({"ok": True, "status": "done"})

    async def fail(self, request):
        body = await request.json()
        self.failed[body["batch_id"]] = (self.token(request), body["error"])
        self.changed.set()
        return web.json_response({"ok": True, "status": "failed"})


def browser_work(batch_id, target, start=None, **overrides):
    start = start or datetime.now(UTC)
    return {
        "batch_id": batch_id,
        "kind": "browser",
        "product_id": batch_id,
        "country_code": "US",
        "region": "browser",
        "target_id": target,
        "target_url": "https://example.invalid/",
        # The assignment's pulse values: a browser host ignores them.
        "timeout_ms": 1234,
        "concurrency": 77,
        "stop_rule": "count",
        "requests": 999,
        "min_sample": None,
        "start_at": start.isoformat(),
    } | overrides


async def serve_until(host: BrowserHost, fake: Orchestrator, done, limit: float = 10) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(host.run(stop))
    try:
        async with asyncio.timeout(limit):
            while not done():
                fake.changed.clear()
                await fake.changed.wait()
    finally:
        stop.set()
        await task


def log_lines(state: Path) -> list[dict]:
    path = state / "logs" / host_module.LOG_NAME
    return [json.loads(line) for line in path.read_text().splitlines()]
