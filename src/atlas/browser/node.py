"""The browser host process (`atlas browser-node`; docs/browser.html, Host).

One process per browser host runs every network the settings assign to that host (K-BP3
[[host]]): one ordinary NodeAgent per network, each with its own token, all sharing one
BrowserPool, of which each engine the targets use has its own pool (pool.EnginePool): the host
never has more than `browsers_per_host` browsers open, whatever their engines, and leases them
first in, first out. A run uses its target's engine (settings.engine) and records it in its
summary's config (`browser_engine`, part of its scope), its tries and its manifest. A `browser`
assignment runs with its target's settings, never the assignment's
pulse values: `tries` requests, `browsers_per_host` concurrency (the pool, not the run, limits
the browsers), the page timeout plus the answer or challenge wait as the try timeout, and the
deadline slot end minus 2 minutes (D8): no try starts after it. Any other kind, or a target the
settings do not list, is refused and reported failed. A network in `[networks] untargeted`
(D11) runs with no country parameter and records no requested country.

Every finished run writes one JSON timing line, and the host one line an hour, to
<state>/logs/browser-node.jsonl (size-rotated). A run's tries and screenshots go into its run
directory (rawfiles.FileRecorder), with the host's process samples of the run
(procstats.TreeSampler, browser/samples.jsonl); when it ends it is sealed into the outbox
and sent from there (rawfiles.Outbox): after every run, at start and every 5 minutes, at most
2 at a time.
An unconfirmed upload never fails the run: the bundle stays and is sent again (K-BP7).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from ..errors import UserError
from ..models import RunConfig
from ..node import (
    NodeAgent,
    NodeSettings,
    ProxyFactory,
    provider_factory,
    read_token,
    resident_mb,
    rotating_loader,
)
from ..node_tls import check_private as _check_private
from ..node_tls import client_tls
from ..sinks import JsonSink
from ..targets import TargetRegistry, registry
from . import chrome, engines, procstats, rawfiles, release
from .contracts import BROWSER_KIND, BROWSER_REGION, user_agent
from .contracts import CAMOUFOX_FIELDS as CAMOUFOX
from .driver import host_os
from .pool import BrowserPool, Launch, Pooled
from .run import BundleSink as BundleSink
from .run import JsonLog as JsonLog
from .run import RunTiming as RunTiming
from .run import RunTransport as RunTransport
from .run import _iso as _iso
from .run import _percentile as _percentile
from .run import run_deadline, run_slot
from .run import slot_deadline as slot_deadline
from .run import slot_start as slot_start
from .run import try_seconds as try_seconds
from .settings import BrowserSettings, TargetSettings, load_settings, queue_load
from .transport import Recorder

log = logging.getLogger(__name__)

LOG_NAME = "browser-node.jsonl"
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


def untargeted_factory(provider_config: Path, credentials_file: Path) -> ProxyFactory:
    """Proxies with no country parameter, a new exit per request, whatever the batch country
    (node.rotating_loader: Geonode on its rotating port)."""
    load = rotating_loader(provider_config, credentials_file)
    return lambda country_code: load({})


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
    try:
        # Making the factory reads provider.toml (node.rotating_loader): inside the try too.
        factory = untargeted_factory(*files) if untargeted else provider_factory(*files)
        token = read_token(directory / "node.token")
        factory(settings.schedule.countries[0])  # a broken provider fails before registering
    except (OSError, ValueError) as exc:
        raise UserError(f"{name}: unusable network files ({type(exc).__name__})") from None
    return Network(name, provider, product, directory, untargeted, token, factory)


def load_networks(state: str | Path, settings: BrowserSettings) -> list[Network]:
    """Every network of the host view `settings` (BrowserSettings.host)."""
    return [load_network(Path(state), name, settings) for name in settings.hosts[0].networks]


def detected_chrome(settings: BrowserSettings) -> dict[str, str]:
    """The Chrome engine block before any Chrome launched: the [chrome] executable's version
    (chrome.detect: a missing or too old Chrome refuses the host's start) and its user agent."""
    version = chrome.detect(settings.chrome.executable)
    return engines.from_chrome(
        {"version": version, "user_agent": chrome.user_agent(version, host_os())}
    )


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


class BrowserAgent(NodeAgent):
    """One network's node agent on a browser host: its targets, run options, transport, sink
    and upload come from the host (NodeAgent's hooks); its upload is the outbox."""

    def __init__(self, host: BrowserHost, network: Network, orchestrator: str, **options):
        super().__init__(
            NodeSettings(
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
    """Every network of one host (`settings` is its BrowserSettings.host view) on one pool.
    `launch` is the launch of each engine (one launch alone serves every engine)."""

    def __init__(
        self,
        settings: BrowserSettings,
        state: str | Path,
        orchestrator: str,
        *,
        launch: Launch | Mapping[str, Launch],
        **agent_options: Any,
    ) -> None:
        self.settings, self.state = settings, Path(state)
        self.name = settings.hosts[0].name
        self.targets: dict[str, TargetSettings] = {t.id: t for t in settings.targets}
        self.registry = bound_registry(settings)
        options = settings.browser
        self._launch = (
            launch if isinstance(launch, Mapping) else dict.fromkeys(engines.ENGINES, launch)
        )
        used = settings.engines()
        launches = {engine: partial(self._launch_one, engine) for engine in used}
        self.pool = BrowserPool(
            options.browsers_per_host, launches, recycle_after=options.recycle_after
        )
        self.pools = {engine: self.pool.engine(engine) for engine in used}
        self.read_ps = procstats.read_ps  # each run's TreeSampler reads `ps` through it
        # The manifest's camoufox block: the last launched browser's, else the pinned one's.
        self.identity = {
            "release": release.RELEASE,
            "engine_version": release.ENGINE_VERSION,
            "user_agent": user_agent(release.ENGINE_VERSION, host_os()),
        }
        # A Chrome run's engine block: the last launched Chrome's, else the detected one's.
        self.chrome = detected_chrome(settings) if engines.CHROME in used else None
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
        # Half runs recover() deleted at this start (and those it could not delete); finished
        # run directories kept unsealed (by recover() at start, and seals that failed since).
        self.discarded = self.unsealed = self.discard_failed = 0
        # Run ids being sealed now: a pass must not send a bundle before its seal has ended.
        self.sealing: set[str] = set()
        self._wake: asyncio.Event | None = None
        self.agents = [BrowserAgent(self, n, orchestrator, **agent_options) for n in self.networks]
        self.by_network = {agent.network.name: agent for agent in self.agents}

    async def _launch_one(self, engine: str) -> Pooled:
        handle = await self._launch[engine]()
        identity = getattr(handle, "identity", None)
        if not isinstance(identity, dict):
            return handle
        if engine == engines.CAMOUFOX and set(CAMOUFOX) <= set(identity):
            self.identity = {key: identity[key] for key in CAMOUFOX}
            log.info(
                "browser launched: Camoufox %s, engine %s, user agent %s",
                *(self.identity[key] for key in CAMOUFOX),
            )
        elif engine == engines.CHROME and {"version", "user_agent"} <= set(identity):
            self.chrome = engines.from_chrome(identity)
            version, agent = self.chrome["version"], self.chrome["user_agent"]
            log.info("browser launched: Chrome %s, user agent %s", version, agent)
        return handle

    def _full_line(self, line: dict[str, Any]) -> None:
        self.log.write(line | {"at": _iso(time.time())})

    def recorder(self, assignment: dict[str, Any]) -> rawfiles.FileRecorder:
        """The run's FileRecorder with its batch facts (the slot of its target's interval)."""
        every = self.targets[assignment["target_id"]].every_minutes
        start = run_slot(assignment["start_at"], every, self.settings.schedule)
        slot = datetime.fromtimestamp(start, UTC)
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
        """Seal with the identity of the run's engine (its summary's config); a Chrome run
        while this host runs no Chrome target has none: kept unsealed (ValueError)."""
        if summary["config"].get("browser_engine") != engines.CHROME:
            camoufox = self.identity
            return rawfiles.seal(
                run_dir, batch, summary, settings_sha256, camoufox, outbox=self.outbox_dir
            )
        if self.chrome is None:
            raise ValueError("a Chrome run, and this host runs no Chrome")
        return rawfiles.seal(
            run_dir,
            batch,
            summary,
            settings_sha256,
            None,
            outbox=self.outbox_dir,
            engine=self.chrome,
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
            except Exception as exc:  # boundary: the next pass retries; type only
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
            "browser_engine": self.settings.engine(entry.id),
        }
        if network.untargeted:
            options["requested_country"] = None
        return options

    def run_options(
        self, network: Network, assignment: dict[str, Any], recorder: Recorder
    ) -> dict[str, Any]:
        """runner.run's deadline and this run's transport on its engine's pool, sampled."""
        entry = self.targets[assignment["target_id"]]
        deadline = run_deadline(assignment["start_at"], entry.every_minutes, self.settings.schedule)
        timing = RunTiming(recorder, procstats.TreeSampler(os.getpid(), read=self.read_ps))
        finished = partial(self._run_line, network, assignment, timing, deadline)
        transport = partial(
            RunTransport,
            pool=self.pools[self.settings.engine(entry.id)],
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
        if isinstance(timing.inner, rawfiles.FileRecorder) and timing.sampler is not None:
            timing.inner.samples(timing.sampler.lines)
        self.log.write(
            {
                "kind": "run",
                "at": _iso(now),
                "batch": assignment["batch_id"],
                "network": network.name,
                "target": assignment["target_id"],
                "engine": config.browser_engine,
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
        line |= {"discarded": self.discarded, "discard_failed": self.discard_failed}
        line["unsealed"] = self.unsealed
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
        the pool closes every browser. A network that cannot serve stops the host. The state
        directory's lock (rawfiles.host_lock) is held throughout, so a second process on it
        fails before its recover() could delete this one's runs."""
        with rawfiles.host_lock(self.state):
            await self._serve(stop)

    async def _serve(self, stop: asyncio.Event) -> None:
        self._wake = asyncio.Event()
        recovered = await asyncio.to_thread(
            rawfiles.recover, self.runs, self.outbox_dir, self.seal_run
        )
        self.discarded, self.unsealed, self.discard_failed = recovered
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
    """--check (_check below), holding the host's lock (rawfiles.host_lock): it never runs
    beside a live host on the same state."""
    with rawfiles.host_lock(Path(state)):
        return await _check(settings, state, launch, browsers, memory)


async def _check(
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
            "queue_load": queue_load(settings),
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
    parser.add_argument("--client-cert", help="Owner-only PEM client certificate (browser port)")
    parser.add_argument("--client-key", help="Owner-only PEM key of --client-cert")
    parser.add_argument(
        "--check", action="store_true", help="Check settings, state and browsers, then exit"
    )
    parser.add_argument("--browsers", type=int, default=1, help="Browsers --check opens")
    parser.add_argument("--verbose", action="store_true")


def dispatch(args) -> int:
    from . import driver

    settings = load_settings(args.settings).host(args.host)
    state = args.state.expanduser()
    headless = settings.browser.headless
    launch = {
        engines.CAMOUFOX: partial(
            driver.launch,
            release.executable(state / "camoufox"),
            locale=settings.browser.locale,
            headless=headless,
        ),
        # The launch check's fingerprint country; each try draws its own run's (driver).
        engines.CHROME: partial(
            driver.launch_chrome,
            settings.chrome.executable,
            country=settings.schedule.countries[0],
            headless=headless,
        ),
    }
    if args.check:
        if not 1 <= args.browsers <= 16:
            raise UserError("--browsers must be 1 to 16")
        camoufox = launch[engines.CAMOUFOX]
        print(json.dumps(asyncio.run(check(settings, state, camoufox, args.browsers)), indent=2))
        return 0
    if not args.orchestrator:
        raise UserError("--orchestrator is required unless --check")
    if bool(args.client_cert) != bool(args.client_key):
        raise UserError("--client-cert and --client-key go together")
    client = (args.client_cert, args.client_key) if args.client_cert else None
    tls = client_tls(args.orchestrator, args.ca_file, client)  # refuses what the host must not use
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
