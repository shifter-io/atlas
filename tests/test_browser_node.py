"""The browser host process: networks on one pool, per-target settings, refusals, logs, --check."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import shutil
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from test_browser_driver import UA, PWBrowser, fake_modules
from test_browser_transport import FakeBrowser, Script

from proxy_index import cli
from proxy_index.browser import node as host_module
from proxy_index.browser import release
from proxy_index.browser.node import (
    BrowserHost,
    RunTiming,
    bound_registry,
    load_networks,
    slot_deadline,
    try_seconds,
)
from proxy_index.browser.settings import load_settings
from proxy_index.browser.transport import NullRecorder
from proxy_index.errors import UserError
from proxy_index.node import Rejected

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
            async def context(self, proxy, block=(), timeout_ms=30_000):
                fleet.open += 1
                fleet.peak = max(fleet.peak, fleet.open)
                try:
                    async with FakeBrowser.context(self, proxy, block, timeout_ms) as page:
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


async def test_two_networks_share_one_pool_with_per_target_settings(tmp_path, monkeypatch):
    # The deadline 10 minutes after the slot end, so that a run started now always has time,
    # even in a slot's last 2 minutes (the real margin: test_deadline_is_the_slot_end...).
    monkeypatch.setattr(host_module, "DEADLINE_MARGIN_S", -600)
    state = make_state(tmp_path)
    past = datetime.now(UTC) - timedelta(hours=2)
    google = browser_work(2, "google_search")
    google_start = google["start_at"]
    work = {
        TOKENS[SHIFTER]: [
            browser_work(1, "amazon_pdp"),
            google,
            browser_work(4, "amazon_pdp", kind="pulse"),
            browser_work(6, "amazon_pdp", start=past),
        ],
        TOKENS[CHEAP]: [browser_work(3, "amazon_pdp"), browser_work(5, "cloudflare_challenge")],
    }
    fake = Orchestrator(work)
    server = TestServer(fake.app)
    await server.start_server()
    fleet = Fleet()
    host = BrowserHost(
        host_view(state),
        state,
        str(server.make_url("")),
        launch=fleet.launch,
        poll_interval=0.02,
        heartbeat_interval=0.05,
    )
    try:
        await serve_until(host, fake, lambda: len(fake.results) == 4 and len(fake.failed) == 2)
    finally:
        await server.close()

    # Each network registers and uploads with its own token, as region browser and its host.
    assert sorted(token for token, _ in fake.registered) == sorted(TOKENS.values())
    assert {body["host"] for _, body in fake.registered} == {"browser-1"}
    assert {body["region"] for _, body in fake.registered} == {"browser"}
    owner = {batch: token for batch, (token, _) in fake.results.items()}
    assert owner == {1: TOKENS[SHIFTER], 2: TOKENS[SHIFTER], 3: TOKENS[CHEAP], 6: TOKENS[SHIFTER]}
    assert fake.failed == {
        4: (TOKENS[SHIFTER], "node error (UnexpectedKind)"),
        5: (TOKENS[CHEAP], "node error (UnknownTarget)"),
    }
    summaries = {batch: summary for batch, (_, summary) in fake.results.items()}
    # Tries, concurrency and timeout come from the settings per target, not the assignment.
    for batch, tries, timeout in ((1, 3, 20.0), (2, 2, 15.0), (3, 3, 20.0)):
        summary = summaries[batch]
        assert summary["requested"] == tries and summary["completed"] == tries
        assert summary["config"]["concurrency"] == 3
        assert summary["config"]["timeout_seconds"] == timeout
        assert summary["config"]["runner_location"] == "browser"
    # Untargeted (D11): no country requested or sent; the targeted network sends US.
    assert summaries[1]["config"]["requested_country"] == "us"
    assert summaries[3]["config"]["requested_country"] is None
    assert summaries[3]["config"]["provider_config"]["parameters"] == {}
    cheap = [p for p in fleet.proxies() if p.username.startswith("cheapacct")]
    shifter = [p for p in fleet.proxies() if not p.username.startswith("cheapacct")]
    assert len(cheap) == 3 and len(shifter) == 5
    assert all("country" not in p.username + p.password for p in cheap)
    assert all("country-us" in p.username for p in shifter)
    # A deadline in the past starts no try.
    assert summaries[6]["started"] == 0 and summaries[6]["stop_reason"] == "deadline"
    # One pool for both networks: runs overlapped, never more browsers than the pool's size.
    assert 2 <= fleet.peak <= 3 and len(fleet.browsers) <= 3
    assert fleet.closed == len(fleet.browsers)  # shutdown closed every browser

    lines = {line["batch"]: line for line in log_lines(state) if line["kind"] == "run"}
    assert set(lines) == {1, 2, 3, 6}
    first = lines[1]
    assert first["network"] == SHIFTER and first["target"] == "amazon_pdp"
    assert first["tries_requested"] == 3 and first["tries_done"] == 3
    assert first["ok"] >= 0 and first["latency_ms"]["p50"] is not None
    assert first["latency_ms"]["p90"] >= first["latency_ms"]["p50"]
    assert first["browser_seconds"] > 0 and first["queue_wait_ms_p50"] is not None
    assert first["finished_before_deadline"] is True and first["geo_targeting"] is True
    assert lines[3]["geo_targeting"] is False
    expected = slot_deadline(google_start, 30)
    assert datetime.fromisoformat(lines[2]["deadline"]).timestamp() == expected
    assert lines[6]["tries_done"] == 0 and lines[6]["finished_before_deadline"] is False


def test_deadline_is_the_slot_end_minus_two_minutes():
    assert (
        slot_deadline("2026-11-04T10:17:30+00:00", 60)
        == datetime(2026, 11, 4, 10, 58, tzinfo=UTC).timestamp()
    )
    assert (
        slot_deadline("2026-11-04T10:31:00+00:00", 30)
        == datetime(2026, 11, 4, 10, 58, tzinfo=UTC).timestamp()
    )
    assert (
        slot_deadline("2026-11-04T10:00:00+00:00", 5)
        == datetime(2026, 11, 4, 10, 3, tzinfo=UTC).timestamp()
    )


def test_targets_are_bound_to_their_settings_waits(tmp_path):
    settings = host_view(make_state(tmp_path))
    bound = bound_registry(settings)
    overview = bound.get("ai_overview")
    assert overview.answer_wait_s == 40 and overview.page_timeout_s == 20
    assert try_seconds(overview, 20) == 40.0  # the wait is cut to the page timeout
    assert try_seconds(bound.get("amazon_pdp"), 20) == 20.0
    example = load_settings(CONFIGS / "browser-pulse.example.toml").host("browser-1")
    challenge = bound_registry(example).get("cloudflare_challenge")
    assert challenge.challenge_wait_s == 20  # the class default is 30
    assert try_seconds(challenge, 45) == 65.0
    chatgpt = bound_registry(example).get("ai_chatgpt")
    assert chatgpt.answer_wait_s == 60 and chatgpt.page_timeout_s == 45
    with pytest.raises(ValueError):
        bound.get("linkedin_public")  # not in the settings, so not in the host registry


def replace_with_directory(path: Path) -> None:
    path.unlink()
    path.mkdir(mode=0o700)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: (d / "node.token").unlink(), "node.token is missing"),
        (lambda d: (d / "node.token").chmod(0o644), "node.token must be owned"),
        (lambda d: (d / "credentials.toml").chmod(0o640), "credentials.toml must be owned"),
        (lambda d: private(d / "node.token", "two words"), "unusable network files"),
        (lambda d: private(d / "provider.toml", "schema_version = 9"), "unusable network"),
        (lambda d: replace_with_directory(d / "provider.toml"), "must be a regular file"),
    ],
)
def test_bad_network_files_are_refused_at_start(tmp_path, change, message):
    state = make_state(tmp_path)
    change(state / "networks" / "proxy-cheap.residential")
    with pytest.raises(UserError, match=message):
        load_networks(state, host_view(state))


def test_a_linked_token_is_refused(tmp_path):
    state = make_state(tmp_path)
    directory = state / "networks" / "shifter.residential"
    shutil.move(directory / "node.token", tmp_path / "token")
    (directory / "node.token").symlink_to(tmp_path / "token")
    with pytest.raises(UserError, match="node.token"):
        load_networks(state, host_view(state))


def test_run_timing_counts_tries_latency_queue_and_browser_time():
    class Inner:
        seen = 0

        def try_done(self, record, screenshot):
            Inner.seen += 1

    timing = RunTiming(Inner())
    ok = {"navigate": 100, "settle": 20, "queue": 5, "context": 3, "total": 140}
    refused = dict.fromkeys(ok) | {"total": 1}
    timing.try_done({"timings_ms": ok, "error": None}, None)
    timing.try_done({"timings_ms": ok | {"navigate": 300}, "error": "timeout"}, None)
    timing.try_done({"timings_ms": refused, "error": "proxy_connect"}, None)
    assert (timing.tries, timing.ok, Inner.seen) == (3, 1, 3)
    assert timing.latency == [120, 320] and timing.queue == [5, 5]
    assert timing.busy_ms == 270
    assert host_module._percentile([], 0.5) is None
    assert host_module._percentile([5, 1, 9, 3], 0.5) == 3
    assert host_module._percentile([5, 1, 9, 3], 0.9) == 9
    NullRecorder().try_done({}, None)


async def test_the_hourly_line_reports_utilization_pool_and_memory(tmp_path):
    state = make_state(tmp_path)
    fleet = Fleet()
    host = BrowserHost(host_view(state), state, "http://orchestrator.invalid", launch=fleet.launch)
    host.hour_interval = 10.0
    host.busy_ms = 6000
    line = await host.hour_line()
    assert line["kind"] == "hour" and line["browsers"] == 3
    assert line["browser_seconds"] == 6 and line["utilization"] == 0.2
    assert set(line["pool"]) >= {"leases", "launches", "crashes", "resident_bytes"}
    assert "rss_mb" in line and host.busy_ms == 0
    host.log.close()
    [written] = log_lines(state)
    assert written == json.loads(json.dumps(line))


async def test_the_hourly_loop_writes_lines_until_stopped(tmp_path):
    state = make_state(tmp_path)
    host = BrowserHost(
        host_view(state), state, "http://orchestrator.invalid", launch=Fleet().launch
    )
    host.hour_interval = 0.02
    stop = asyncio.Event()
    task = asyncio.create_task(host._hourly(stop))
    await asyncio.sleep(0.1)
    stop.set()
    await task
    host.log.close()
    assert len(log_lines(state)) >= 2


async def test_a_network_that_cannot_register_stops_the_host(tmp_path):
    state = make_state(tmp_path)

    async def refuse(request):
        return web.json_response({"error": "bad token"}, status=401)

    app = web.Application()
    app.router.add_route("*", "/v1/register", refuse)
    server = TestServer(app)
    await server.start_server()
    host = BrowserHost(host_view(state), state, str(server.make_url("")), launch=Fleet().launch)
    try:
        with pytest.raises(Rejected):
            await host.run(asyncio.Event())
    finally:
        await server.close()


def install_fake_release(state: Path) -> None:
    name = release.current_platform()
    build = release.BUILDS.get(name)
    if build is None:
        pytest.skip("no pinned build for this platform")
    target = state / "camoufox" / release.RELEASE
    executable = target / build.executable
    executable.parent.mkdir(parents=True)
    executable.write_text("")
    marker = {"release": release.RELEASE, "platform": name, "sha256": build.sha256}
    (target / release.MARKER).write_text(json.dumps(marker))


def run_cli(state: Path, *extra: str) -> int:
    settings = str(state / "settings.toml")
    argv = ["browser-node", "--settings", settings, "--state", str(state), "--host", "browser-1"]
    return cli.main([*argv, *extra])


def test_check_launches_browsers_and_prints_their_identity(tmp_path, monkeypatch, capsys):
    state = make_state(tmp_path)
    install_fake_release(state)
    fake_modules(monkeypatch, PWBrowser("158.0.1", [UA.format(158)] * 2))
    sizes = iter([100, 350, 600])
    monkeypatch.setattr("proxy_index.browser.pool.resident_bytes", lambda: next(sizes))
    assert run_cli(state, "--check", "--browsers", "2") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["release"] == release.RELEASE and report["engine"] == "Firefox"
    assert report["engine_version"] == "158.0.1" and report["user_agent"] == UA.format(158)
    assert report["networks"] == [SHIFTER, CHEAP] and report["host"] == "browser-1"
    assert report["browsers"] == [{"resident_bytes": 250}, {"resident_bytes": 250}]
    assert report["resident_bytes"] == 500


def test_check_fails_on_a_user_agent_mismatch(tmp_path, monkeypatch, capsys):
    state = make_state(tmp_path)
    install_fake_release(state)
    playwright = fake_modules(monkeypatch, PWBrowser("158.0.1", [UA.format(135)] * 2))
    assert run_cli(state, "--check") == 2
    assert "user agent" in capsys.readouterr().err and playwright.stopped


def test_check_refuses_bad_options_and_a_missing_browser(tmp_path, capsys):
    state = make_state(tmp_path)
    assert run_cli(state, "--check") == 2  # no verified Camoufox install
    assert "not installed" in capsys.readouterr().err
    install_fake_release(state)
    assert run_cli(state, "--check", "--browsers", "0") == 2
    assert run_cli(state) == 2  # serving needs --orchestrator
    assert "--orchestrator" in capsys.readouterr().err


async def test_check_reports_unknown_memory_and_closes_browsers(tmp_path):
    state = make_state(tmp_path)
    fleet = Fleet()

    async def launch():
        browser = await fleet.launch()
        browser.identity = {"engine_version": "158.0.1", "user_agent": "UA"}
        return browser

    report = await host_module.check(host_view(state), state, launch, 2, memory=lambda: None)
    assert report["browsers"] == [{"resident_bytes": None}] * 2
    assert report["resident_bytes"] is None and fleet.closed == 2


def test_serving_disabled_settings_runs_nothing(tmp_path, monkeypatch):
    state = make_state(tmp_path)
    install_fake_release(state)
    private(state / "settings.toml", SETTINGS.replace("enabled = true", "enabled = false"))
    served = []

    async def fake_serve(host):
        served.append(host)

    monkeypatch.setattr(host_module, "serve", fake_serve)
    assert run_cli(state, "--orchestrator", "http://orchestrator.invalid") == 0
    assert served == [None]
    private(state / "settings.toml", SETTINGS)
    assert run_cli(state, "--orchestrator", "http://orchestrator.invalid") == 0
    assert isinstance(served[1], BrowserHost) and served[1].name == "browser-1"
    served[1].log.close()


async def test_serve_stops_on_sigterm(tmp_path):
    import os
    import signal

    async def stop_soon():
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    task = asyncio.create_task(stop_soon())
    await asyncio.wait_for(host_module.serve(None), 5)
    await task
