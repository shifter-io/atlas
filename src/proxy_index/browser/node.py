"""The browser host process (`proxy-index browser-node`; docs/browser.html, Host).

One process per browser host runs every network the settings assign to that host (K-BP3
[[host]]): one ordinary NodeAgent per network, each with its own token, all sharing one
BrowserPool. A `browser` assignment runs with its target's settings, never the assignment's
pulse values: `tries` requests, `browsers_per_host` concurrency (the pool, not the run, limits
the browsers), the page timeout plus the answer or challenge wait as the try timeout, and the
deadline slot end minus 2 minutes (D8): no try starts after it. Any other kind, or a target the
settings do not list, is refused and reported failed. A network in `[networks] untargeted`
(D11) runs with no country parameter and records no requested country.

Every finished run writes one JSON timing line, and the host one line an hour, to
<state>/logs/browser-node.jsonl (size-rotated). Uploads are the node's plain upload; BP-DATA
replaces them with the outbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import stat
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from functools import partial
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from ..errors import UserError
from ..models import ProxySource, RunConfig
from ..node import NodeAgent, ProxyFactory, provider_factory, read_token, resident_mb
from ..targets import Target, TargetRegistry, registry
from .contracts import BROWSER_KIND, BROWSER_REGION
from .pool import BrowserPool, Pooled
from .settings import BrowserSettings, TargetSettings, load_settings
from .transport import BrowserTransport, NullRecorder, Recorder

log = logging.getLogger(__name__)

DEADLINE_MARGIN_S = 120
LOG_NAME = "browser-node.jsonl"
LOG_BYTES = 20 << 20
LOG_BACKUPS = 5
HOUR_S = 3600.0
NETWORK_FILES = ("provider.toml", "credentials.toml", "node.token")

RecorderFactory = Callable[["Network", dict[str, Any]], Recorder]


class UnexpectedKind(Exception):
    """A browser host runs only `browser` assignments."""


class UnknownTarget(Exception):
    """The assignment's target is not a [[target]] of the settings file."""


@dataclass(frozen=True, slots=True)
class Network:
    """One network of this host: its state directory, token and proxies (never logged)."""

    name: str
    provider: str
    product: str
    directory: Path
    untargeted: bool
    token: str = field(repr=False)
    proxy_factory: ProxyFactory = field(repr=False)


def _check_private(path: Path) -> None:
    """An owner-only regular file, opened without following links (as node.read_token)."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("must be a regular file")
    if os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError("must be owned by the current user with mode 600")


def untargeted_factory(provider_config: Path, credentials_file: Path) -> ProxyFactory:
    """Proxies with no country parameter and rotating sessions, whatever the batch country."""
    from ..providers import load_provider

    def build(country_code: str) -> ProxySource:
        return load_provider(
            provider_config,
            parameters={},
            session_policy="rotate",
            credentials_file=credentials_file,
        )

    return build


def load_network(state: Path, name: str, settings: BrowserSettings) -> Network:
    """networks/<provider>.<product>/ checked (all 0600) and its provider built once."""
    provider, product = name.split("/")
    directory = state / "networks" / f"{provider}.{product}"
    for file in NETWORK_FILES:
        try:
            _check_private(directory / file)
        except FileNotFoundError:
            raise UserError(f"{name}: {file} is missing") from None
        except (OSError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            raise UserError(f"{name}: {file} {reason}") from None
    untargeted = settings.is_untargeted(name)
    files = (directory / "provider.toml", directory / "credentials.toml")
    factory = untargeted_factory(*files) if untargeted else provider_factory(*files)
    try:
        token = read_token(directory / "node.token")
        factory(settings.schedule.countries[0])  # a broken provider fails before registering
    except (OSError, ValueError) as exc:
        raise UserError(f"{name}: unusable network files ({type(exc).__name__})") from None
    return Network(name, provider, product, directory, untargeted, token, factory)


def load_networks(state: str | Path, settings: BrowserSettings) -> list[Network]:
    """Every network of the host view `settings` (BrowserSettings.host)."""
    return [load_network(Path(state), name, settings) for name in settings.hosts[0].networks]


def bound_registry(settings: BrowserSettings) -> TargetRegistry:
    """The settings' browser targets, each a copy with its settings waits bound."""
    bound = TargetRegistry()
    for entry in settings.targets:
        target = registry.get(entry.id)
        values = {
            "challenge_wait_s": entry.challenge_wait_s,
            "answer_wait_s": entry.answer_wait_s,
            "page_timeout_s": entry.timeout_s or settings.browser.page_timeout_s,
        }
        names = {f.name for f in fields(target)}
        changes = {k: v for k, v in values.items() if k in names and v is not None}
        bound.register(replace(target, **changes))
    return bound


def try_seconds(target: Target, page_timeout_s: int) -> float:
    """The try timeout: the page timeout plus the target's answer or challenge wait."""
    budget = getattr(target, "budget_s", None)
    wait = budget() if budget is not None else getattr(target, "challenge_wait_s", 0)
    return float(page_timeout_s + wait)


def slot_deadline(start_at: str, every_minutes: int) -> float:
    """The run deadline: the end of the target slot holding start_at, minus 2 minutes."""
    every = every_minutes * 60
    start = datetime.fromisoformat(start_at).timestamp()
    return math.floor(start / every) * every + every - DEADLINE_MARGIN_S


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).isoformat()


def _percentile(values: list[int], share: float) -> int | None:
    """Nearest-rank percentile; None for no values."""
    ordered = sorted(values)
    return ordered[max(math.ceil(share * len(ordered)) - 1, 0)] if ordered else None


class RunTiming:
    """One run's recorder: keeps the timing of every try and passes it on to `inner`."""

    def __init__(self, inner: Recorder) -> None:
        self.inner = inner
        self.tries = self.ok = self.busy_ms = 0
        self.latency: list[int] = []
        self.queue: list[int] = []

    def try_done(self, record: dict[str, Any], screenshot: bytes | None) -> None:
        timings = record["timings_ms"]
        self.tries += 1
        self.ok += record["error"] is None
        if timings["navigate"] is not None:
            self.latency.append(timings["navigate"] + (timings["settle"] or 0))
        if timings["queue"] is not None:  # it held a browser from the lease to its end
            self.queue.append(timings["queue"])
            self.busy_ms += max((timings["total"] or 0) - timings["queue"], 0)
        self.inner.try_done(record, screenshot)


class RunTransport(BrowserTransport):
    """BrowserTransport that reports when its run ends (the runner leaves the transport)."""

    def __init__(self, config: RunConfig, *, finished: Callable[[RunConfig], None], **options):
        super().__init__(config, **options)
        self._finished = finished

    async def __aexit__(self, *args: object) -> None:
        self._finished(self.config)


class JsonLog:
    """JSON lines, size-rotated: LOG_BYTES per file, LOG_BACKUPS old files kept."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handler = RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.handler = handler

    def write(self, line: dict[str, Any]) -> None:
        message = json.dumps(line, sort_keys=True)
        self.handler.handle(logging.makeLogRecord({"msg": message, "levelno": logging.INFO}))

    def close(self) -> None:
        self.handler.close()


class BrowserAgent(NodeAgent):
    """One network's node agent on a browser host: its targets, run options and transport
    come from the host (NodeAgent's registry, config_options and run_options hooks)."""

    def __init__(self, host: BrowserHost, network: Network, orchestrator: str, **options):
        super().__init__(
            orchestrator,
            network.token,
            provider=network.provider,
            product=network.product,
            proxy_factory=network.proxy_factory,
            work_dir=host.state / "runs" / network.directory.name,
            region=BROWSER_REGION,
            host=host.name,
            **options,
        )
        self.browser_host, self.network = host, network
        self.registry = host.registry

    def config_options(self, assignment: dict[str, Any]) -> dict[str, Any]:
        return self.browser_host.config_options(self.network, assignment)

    def run_options(self, assignment: dict[str, Any]) -> dict[str, Any]:
        return self.browser_host.run_options(self.network, assignment)


class BrowserHost:
    """Every network of one host (`settings` is its BrowserSettings.host view) on one pool."""

    def __init__(
        self,
        settings: BrowserSettings,
        state: str | Path,
        orchestrator: str,
        *,
        launch: Callable[[], Awaitable[Pooled]],
        recorder: RecorderFactory | None = None,
        **agent_options: Any,
    ) -> None:
        self.settings, self.state = settings, Path(state)
        self.name = settings.hosts[0].name
        self.targets: dict[str, TargetSettings] = {t.id: t for t in settings.targets}
        self.registry = bound_registry(settings)
        options = settings.browser
        self.pool = BrowserPool(
            options.browsers_per_host, launch, recycle_after=options.recycle_after
        )
        self.recorder = recorder or (lambda network, assignment: NullRecorder())
        self.hour_interval = HOUR_S
        self.busy_ms = 0
        self.networks = load_networks(self.state, settings)
        self.log = JsonLog(self.state / "logs" / LOG_NAME)
        self.agents = [BrowserAgent(self, n, orchestrator, **agent_options) for n in self.networks]

    def config_options(self, network: Network, assignment: dict[str, Any]) -> dict[str, Any]:
        """The RunConfig fields from the settings; refuses other kinds and unlisted targets."""
        if assignment.get("kind") != BROWSER_KIND:
            raise UnexpectedKind
        entry = self.targets.get(assignment.get("target_id"))
        if entry is None:
            raise UnknownTarget
        page_timeout_s = entry.timeout_s or self.settings.browser.page_timeout_s
        options: dict[str, Any] = {
            "requests": entry.tries,
            "concurrency": self.settings.browser.browsers_per_host,
            "timeout_seconds": try_seconds(self.registry.get(entry.id), page_timeout_s),
        }
        if network.untargeted:
            options["requested_country"] = None
        return options

    def run_options(self, network: Network, assignment: dict[str, Any]) -> dict[str, Any]:
        """runner.run's deadline and this run's transport on the shared pool."""
        entry = self.targets[assignment["target_id"]]
        deadline = slot_deadline(assignment["start_at"], entry.every_minutes)
        timing = RunTiming(self.recorder(network, assignment))
        finished = partial(self._run_line, network, assignment, timing, deadline)
        transport = partial(
            RunTransport,
            pool=self.pool,
            browser=self.settings.browser,
            settings=entry,
            recorder=timing,
            deadline=deadline,
            finished=finished,
        )
        return {"deadline": deadline, "transport_factory": transport}

    def _run_line(
        self,
        network: Network,
        assignment: dict[str, Any],
        timing: RunTiming,
        deadline: float,
        config: RunConfig,
    ) -> None:
        now = time.time()
        self.busy_ms += timing.busy_ms
        self.log.write(
            {
                "kind": "run",
                "at": _iso(now),
                "batch": assignment["batch_id"],
                "network": network.name,
                "target": assignment["target_id"],
                "country": assignment["country_code"],
                "geo_targeting": not network.untargeted,
                "tries_requested": config.requests,
                "tries_done": timing.tries,
                "ok": timing.ok,
                "latency_ms": {
                    "p50": _percentile(timing.latency, 0.5),
                    "p90": _percentile(timing.latency, 0.9),
                },
                "browser_seconds": timing.busy_ms / 1000,
                "queue_wait_ms_p50": _percentile(timing.queue, 0.5),
                "deadline": _iso(deadline),
                "finished_before_deadline": now < deadline,
            }
        )

    async def hour_line(self) -> dict[str, Any]:
        """The host line: browser-seconds used over browsers x the interval, pool, memory."""
        stats = await asyncio.to_thread(self.pool.stats)
        busy, self.busy_ms = self.busy_ms, 0
        seconds = busy / 1000
        line = {"kind": "hour", "at": _iso(time.time()), "browsers": self.pool.size}
        line |= {"browser_seconds": seconds, "pool": stats, "rss_mb": resident_mb()}
        line["utilization"] = round(seconds / (self.pool.size * self.hour_interval), 4)
        self.log.write(line)
        return line

    async def _hourly(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.hour_interval)
            if not stop.is_set():
                await self.hour_line()

    async def run(self, stop: asyncio.Event) -> None:
        """Serve every network until `stop`; then their runs end as NodeAgent ends them and
        the pool closes every browser. A network that cannot serve stops the host."""
        tasks = [asyncio.create_task(agent.run(stop)) for agent in self.agents]
        hourly = asyncio.create_task(self._hourly(stop))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        finally:
            stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            hourly.cancel()
            await asyncio.gather(hourly, return_exceptions=True)
            await self.pool.close()
            self.log.close()
        for task in done:
            if task.exception() is not None:
                raise task.exception()


async def check(
    settings: BrowserSettings,
    state: str | Path,
    launch: Callable[[], Awaitable[Any]],
    browsers: int = 1,
    memory: Callable[[], int | None] | None = None,
) -> dict[str, Any]:
    """--check: the state files, then `browsers` browsers open together (each checks the UA
    rule at launch), their identity and resident memory; no proxy traffic."""
    from . import release
    from .pool import resident_bytes

    memory = memory or resident_bytes
    networks = load_networks(state, settings)
    handles: list[Any] = []
    try:
        before, sizes = memory(), []
        for _ in range(browsers):
            handles.append(await launch())
            after = memory()
            sizes.append(after - before if None not in (before, after) else None)
            before = after
        identity = handles[0].identity
        return {
            "host": settings.hosts[0].name,
            "networks": [network.name for network in networks],
            "release": release.RELEASE,
            "engine": release.ENGINE,
            "engine_version": identity["engine_version"],
            "user_agent": identity["user_agent"],
            "browsers": [{"resident_bytes": size} for size in sizes],
            "resident_bytes": None if None in sizes else sum(sizes),
        }
    finally:
        for handle in handles:
            await handle.close()


async def serve(host: BrowserHost | None) -> None:
    """Run the host until SIGINT or SIGTERM; with no host (settings disabled), only wait."""
    import signal

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    if host is None:
        await stop.wait()
    else:
        await host.run(stop)


def register(commands) -> None:
    parser = commands.add_parser(
        "browser-node", help="Run every network the browser settings assign to this host"
    )
    parser.add_argument("--settings", type=Path, required=True, help="Settings file (schema 2)")
    parser.add_argument("--state", type=Path, required=True, help="Host state directory")
    parser.add_argument("--host", required=True, help="Logical host name (browser-<n>)")
    parser.add_argument("--orchestrator", help="Orchestrator base URL (not needed with --check)")
    parser.add_argument(
        "--check", action="store_true", help="Check settings, state and browsers, then exit"
    )
    parser.add_argument("--browsers", type=int, default=1, help="Browsers --check opens")
    parser.add_argument("--verbose", action="store_true")


def dispatch(args) -> int:
    from . import driver, release

    settings = load_settings(args.settings).host(args.host)
    state = args.state.expanduser()
    launch = partial(
        driver.launch,
        release.executable(state / "camoufox"),
        locale=settings.browser.locale,
        headless=settings.browser.headless,
    )
    if args.check:
        if not 1 <= args.browsers <= 16:
            raise UserError("--browsers must be 1 to 16")
        print(json.dumps(asyncio.run(check(settings, state, launch, args.browsers)), indent=2))
        return 0
    if not args.orchestrator:
        raise UserError("--orchestrator is required unless --check")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Request URLs and proxy handshakes stay out of the log.
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    if not settings.enabled:
        log.warning("the browser settings say enabled = false: this host runs nothing")
        asyncio.run(serve(None))
        return 0
    asyncio.run(serve(BrowserHost(settings, state, args.orchestrator, launch=launch)))
    return 0
