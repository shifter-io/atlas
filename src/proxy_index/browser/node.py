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
<state>/logs/browser-node.jsonl (size-rotated). A run's tries and screenshots go into its run
directory (rawfiles.FileRecorder); when it ends it is sealed into the outbox and sent from
there (rawfiles.Outbox): after every run, at start and every 5 minutes, at most 2 at a time.
An unconfirmed upload never fails the run: the bundle stays and is sent again (K-BP7).
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
from ..node import (
    NodeAgent,
    ProxyFactory,
    provider_factory,
    read_token,
    resident_mb,
    rotating_policy,
)
from ..node_tls import client_tls
from ..sinks import JsonSink
from ..targets import Target, TargetRegistry, registry
from . import rawfiles, release
from .contracts import BROWSER_KIND, BROWSER_REGION, user_agent
from .contracts import CAMOUFOX_FIELDS as CAMOUFOX
from .driver import host_os
from .pool import BrowserPool, Pooled
from .settings import BrowserSettings, TargetSettings, load_settings
from .transport import BrowserTransport, Recorder

log = logging.getLogger(__name__)

DEADLINE_MARGIN_S = 120
LOG_NAME = "browser-node.jsonl"
LOG_BYTES = 20 << 20
LOG_BACKUPS = 5
HOUR_S = 3600.0
OUTBOX_EVERY_S = 300.0
NETWORK_FILES = ("provider.toml", "credentials.toml", "node.token")


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
    """Proxies with no country parameter, a new exit per request, whatever the batch country."""
    from ..providers import load_provider

    def build(country_code: str) -> ProxySource:
        return load_provider(
            provider_config,
            parameters={},
            session_policy=rotating_policy(provider_config),
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


def slot_start(start_at: str, every_minutes: int) -> int:
    """The start (Unix time) of the target slot holding start_at."""
    every = every_minutes * 60
    return math.floor(datetime.fromisoformat(start_at).timestamp() / every) * every


def slot_deadline(start_at: str, every_minutes: int) -> float:
    """The run deadline: the end of the target slot holding start_at, minus 2 minutes."""
    return slot_start(start_at, every_minutes) + every_minutes * 60 - DEADLINE_MARGIN_S


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


class BundleSink(JsonSink):
    """The run's JsonSink; its run directory, once made, is the recorder's."""

    def __init__(self, output: Path, recorder: rawfiles.FileRecorder) -> None:
        super().__init__(output)
        self.recorder = recorder

    def start(self, run_id: str) -> None:
        super().start(run_id)
        assert self.directory is not None
        self.recorder.bind(self.directory)


class BrowserAgent(NodeAgent):
    """One network's node agent on a browser host: its targets, run options, transport, sink
    and upload come from the host (NodeAgent's hooks); its upload is the outbox."""

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
        # Each run's recorder, from run_options() until result_sink() binds it.
        self.recorders: dict[int, rawfiles.FileRecorder] = {}

    def config_options(self, assignment: dict[str, Any]) -> dict[str, Any]:
        return self.browser_host.config_options(self.network, assignment)

    def run_options(self, assignment: dict[str, Any]) -> dict[str, Any]:
        recorder = self.browser_host.recorder(assignment)
        self.recorders[assignment["batch_id"]] = recorder
        return self.browser_host.run_options(self.network, assignment, recorder)

    def result_sink(self, assignment: dict[str, Any]) -> JsonSink:
        return BundleSink(self.work_dir, self.recorders.pop(assignment["batch_id"]))

    async def execute(self, active: Any) -> None:
        try:
            await super().execute(active)
        finally:
            self.recorders.pop(active.assignment["batch_id"], None)

    async def register(self) -> int:
        node_id = await super().register()
        self.browser_host.wake_outbox()  # host start: send what earlier runs left
        return node_id

    async def upload(self, batch_id: int, directory: Path) -> dict[str, Any]:
        """Seal the run into the outbox and wake the outbox loop. Never raises: a run that
        cannot be sealed keeps its directory, and the next start seals it (rawfiles.recover)."""
        host = self.browser_host
        host.sealing.add(directory.name)  # no outbox pass sends it before the seal ends
        try:
            bundle = await asyncio.to_thread(host.seal, directory)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("batch %s: run not sealed (%s); kept", batch_id, type(exc).__name__)
            host.unsealed += 1
            return {}
        finally:
            host.sealing.discard(directory.name)
        host.wake_outbox()
        return {"bundle": bundle.run_id}


class BrowserHost:
    """Every network of one host (`settings` is its BrowserSettings.host view) on one pool."""

    def __init__(
        self,
        settings: BrowserSettings,
        state: str | Path,
        orchestrator: str,
        *,
        launch: Callable[[], Awaitable[Pooled]],
        **agent_options: Any,
    ) -> None:
        self.settings, self.state = settings, Path(state)
        self.name = settings.hosts[0].name
        self.targets: dict[str, TargetSettings] = {t.id: t for t in settings.targets}
        self.registry = bound_registry(settings)
        options = settings.browser
        self._launch = launch
        self.pool = BrowserPool(
            options.browsers_per_host, self._launch_one, recycle_after=options.recycle_after
        )
        # The manifest's camoufox block: the last launched browser's, else the pinned one's.
        self.identity = {
            "release": release.RELEASE,
            "engine_version": release.ENGINE_VERSION,
            "user_agent": user_agent(release.ENGINE_VERSION, host_os()),
        }
        self.hour_interval = HOUR_S
        self.outbox_interval = OUTBOX_EVERY_S
        self.busy_ms = 0
        self.networks = load_networks(self.state, settings)
        self.log = JsonLog(self.state / "logs" / LOG_NAME)
        self.runs, self.outbox_dir = self.state / "runs", self.state / settings.outbox.dir
        self.outbox = rawfiles.Outbox(self.outbox_dir)
        self.guard = rawfiles.DiskGuard(
            (self.runs, self.outbox_dir), settings.outbox.max_pending_gb, notify=self._full_line
        )
        # Half runs recover() deleted at this start; finished run directories kept unsealed
        # (by recover() at start, and seals that failed since).
        self.discarded = self.unsealed = 0
        # Run ids being sealed now: a pass must not send a bundle before its seal has ended.
        self.sealing: set[str] = set()
        self._wake: asyncio.Event | None = None
        self.agents = [BrowserAgent(self, n, orchestrator, **agent_options) for n in self.networks]
        self.by_network = {agent.network.name: agent for agent in self.agents}

    async def _launch_one(self) -> Pooled:
        handle = await self._launch()
        identity = getattr(handle, "identity", None)
        if isinstance(identity, dict) and set(CAMOUFOX) <= set(identity):
            self.identity = {key: identity[key] for key in CAMOUFOX}
        return handle

    def _full_line(self, line: dict[str, Any]) -> None:
        self.log.write(line | {"at": _iso(time.time())})

    def recorder(self, assignment: dict[str, Any]) -> rawfiles.FileRecorder:
        """The run's FileRecorder with its batch facts (the slot of its target's interval)."""
        every = self.targets[assignment["target_id"]].every_minutes
        slot = datetime.fromtimestamp(slot_start(assignment["start_at"], every), UTC)
        batch = rawfiles.Batch(assignment["batch_id"], assignment["country_code"], slot, self.name)
        return rawfiles.FileRecorder(batch, self.settings.sha256, self.guard)

    def seal(self, run_dir: Path) -> rawfiles.Bundle:
        """A finished run directory into the outbox, with the facts its recorder kept."""
        batch, settings_sha256 = rawfiles.run_facts(run_dir)
        summary = json.loads((run_dir / "summary.json").read_bytes())
        return self.seal_run(run_dir, batch, summary, settings_sha256)

    def seal_run(
        self, run_dir: Path, batch: rawfiles.Batch, summary: dict, settings_sha256: str
    ) -> rawfiles.Bundle:
        camoufox = self.identity
        return rawfiles.seal(
            run_dir, batch, summary, settings_sha256, camoufox, outbox=self.outbox_dir
        )

    def wake_outbox(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def flush(self) -> list[str]:
        """Send every due pending bundle through its network's registered agent, oldest
        first, at most INGEST_PARALLEL at once; their outcomes ("error" for a send that raised:
        logged by type, the bundle kept for the next pass)."""
        sends = []
        for bundle in await asyncio.to_thread(self.outbox.pending):
            agent = self.by_network.get(bundle.network)
            if agent is None or agent.node_id is None or agent.session is None:
                continue  # not this host's network, or not registered yet
            if bundle.run_id not in self.sealing and self.outbox.due(bundle):
                sends.append((bundle, self.outbox.send(bundle, agent)))
        results = await asyncio.gather(*(send for _, send in sends), return_exceptions=True)
        outcomes = []
        for (bundle, _), result in zip(sends, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result  # cancellation
                log.warning("bundle %s not sent (%s)", bundle.run_id, type(result).__name__)
                result = "error"
            outcomes.append(result)
        return outcomes

    async def _outbox_loop(self, stop: asyncio.Event) -> None:
        """A pass after every run end, at start and every outbox_interval seconds."""
        assert self._wake is not None
        while not stop.is_set():  # run() cancels it when it stops
            self._wake.clear()
            try:
                await self.flush()
            except Exception as exc:  # noqa: BLE001 - the next pass retries; type only
                log.warning("outbox pass failed (%s)", type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.outbox_interval)

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

    def run_options(
        self, network: Network, assignment: dict[str, Any], recorder: Recorder
    ) -> dict[str, Any]:
        """runner.run's deadline and this run's transport on the shared pool."""
        entry = self.targets[assignment["target_id"]]
        deadline = slot_deadline(assignment["start_at"], entry.every_minutes)
        timing = RunTiming(recorder)
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
        line |= self.outbox.hour_stats()
        line |= {"discarded": self.discarded, "unsealed": self.unsealed}
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
        self._wake = asyncio.Event()
        self.discarded, self.unsealed = await asyncio.to_thread(
            rawfiles.recover, self.runs, self.outbox_dir, self.seal_run
        )
        tasks = [asyncio.create_task(agent.run(stop)) for agent in self.agents]
        helpers = [asyncio.create_task(loop(stop)) for loop in (self._hourly, self._outbox_loop)]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        finally:
            stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            for helper in helpers:
                helper.cancel()
            await asyncio.gather(*helpers, return_exceptions=True)
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
        "--ca-file", help="PEM certificate to trust alone for an https orchestrator (the manager's)"
    )
    parser.add_argument(
        "--check", action="store_true", help="Check settings, state and browsers, then exit"
    )
    parser.add_argument("--browsers", type=int, default=1, help="Browsers --check opens")
    parser.add_argument("--verbose", action="store_true")


def dispatch(args) -> int:
    from . import driver

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
    tls = client_tls(args.orchestrator, args.ca_file)  # refuses a URL the host must not use
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
    host = BrowserHost(settings, state, args.orchestrator, launch=launch)
    for agent in host.agents:
        agent.tls = tls
    asyncio.run(serve(host))
    return 0
