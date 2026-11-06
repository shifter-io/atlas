"""The always-on node agent (`proxy-index node`): one per provider product per region.

It registers with the orchestrator, polls /v1/work every few seconds and runs every assignment
(census and pulses at once) with the ordinary runner: it waits for the shared start_at on the
wall clock (keep the host NTP-synced), builds the proxy from the local provider configuration
with the batch's country and a rotating session, posts live counters to /v1/progress (a
{"stop": true} reply stops the run), enriches the finished directory with the local MMDB,
uploads it as tar.gz to /v1/results with retries, and deletes it once accepted. Heartbeats go
out every ten seconds.

Credentials never leave the node and are never logged: errors are reported by type only.
Every request verifies the orchestrator's TLS as `tls` says (node_tls.client_tls: the pinned
manager certificate, or the public CAs).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import shutil
import ssl
import stat
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import KW_ONLY, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from . import __version__
from .models import ProxySource, RunConfig
from .orchestrator.archive import pack
from .runner import Enricher, RunOptions, run
from .sinks import JsonSink
from .targets import EchoIpTarget, TargetRegistry, registry
from .transport import HttpTransport

log = logging.getLogger(__name__)

ProxyFactory = Callable[[str], ProxySource]
UPLOAD_ATTEMPTS = 8


def read_token(path: str | Path) -> str:
    """The node's bearer token: an owner-only regular file, read without following links."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Token path must be a regular file")
        if os.name == "posix" and (
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValueError("Token file must be owned by the current user with mode 600")
        token = stream.read().strip()
    if not token or any(char.isspace() for char in token):
        raise ValueError("Token file must contain one token")
    return token


def provider_factory(
    provider_config: str | Path, credentials_file: str | Path | None = None
) -> ProxyFactory:
    """Proxies from the node's provider TOML, targeted at one country, a new exit per request."""
    load = rotating_loader(provider_config, credentials_file)
    return lambda country_code: load({"country": country_code.lower()})


def rotating_loader(
    provider_config: str | Path, credentials_file: str | Path | None = None
) -> Callable[[dict[str, Any]], ProxySource]:
    """Load the provider TOML with the given parameters and a new exit per request.

    The policy is rotating_policy's. A grammar that rotates only on rotating gateway ports
    (Geonode) refuses "rotate" on the file's sticky port: the proxy then goes to the scheme's
    first rotating port (Geonode HTTP: 9000), logged once here. The file is not changed; its
    sticky port stays for the runs that ask for sessions (census, campaigns).
    """
    from .providers import load_provider

    provider, settings = _provider_file(provider_config)
    policy = rotating_policy(provider_config)
    port = _rotating_port(provider, settings) if policy == "rotate" else None
    if port is not None:
        log.info("%s: a new exit per request uses rotating gateway port %d", provider, port)

    def load(parameters: dict[str, Any]) -> ProxySource:
        if port is None:
            return load_provider(
                provider_config,
                parameters=parameters,
                session_policy=policy,
                credentials_file=credentials_file,
            )
        # Checked as the file says first ("gateway": no session, on its own port), then moved.
        sticky = load_provider(
            provider_config,
            parameters=parameters,
            session_policy="gateway",
            credentials_file=credentials_file,
        )
        return replace(sticky, port=port, session_policy=policy)

    return load


def rotating_policy(provider_config: str | Path) -> str | None:
    """The session policy for a new exit per request: "rotate", or None (the grammar default).

    A grammar whose default is not "rotate" and that has no rotating ports sets rotation
    itself and refuses "rotate" (Proxy-Seller: the next port per request). Geonode, default
    "gateway" with rotating ports, keeps "rotate" (on a rotating port, rotating_loader).
    """
    from .providers.grammar import load_grammar

    provider, _ = _provider_file(provider_config)
    if not isinstance(provider, str):
        return "rotate"  # load_provider refuses the file with its own error
    grammar = load_grammar(provider)
    return "rotate" if grammar.no_session_policy == "rotate" or grammar.rotating_ports else None


def _provider_file(provider_config: str | Path) -> tuple[Any, dict[str, Any]]:
    """The provider TOML's `provider` value and its [settings] table ({} when not a table)."""
    with Path(provider_config).open("rb") as stream:
        data = tomllib.load(stream)
    settings = data.get("settings")
    return data.get("provider"), settings if isinstance(settings, dict) else {}


def _rotating_port(provider: Any, settings: dict[str, Any]) -> int | None:
    """The scheme's first rotating gateway port when the grammar has rotating ports for the
    file's scheme and the file's port (or the grammar's default) is not one of them; else None.
    Malformed values give None: load_provider refuses them with its own error."""
    from .providers.grammar import load_grammar

    scheme = settings.get("scheme", "http")
    if not isinstance(provider, str) or not isinstance(scheme, str):
        return None
    grammar = load_grammar(provider)
    rotating = grammar.rotating_ports.get(scheme)
    port = settings.get("port", grammar.ports.get(scheme))
    if rotating is None or type(port) is not int or rotating[0] <= port <= rotating[1]:
        return None
    return rotating[0]


def resident_mb() -> int | None:
    """Current resident memory on Linux; peak resident memory elsewhere."""
    try:
        with open("/proc/self/statm") as stream:
            return int(stream.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") // (1 << 20)
    except (OSError, ValueError, IndexError):
        try:
            import resource

            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return int(peak / (1 << 20) if sys.platform == "darwin" else peak / 1024)
        except (ImportError, OSError):
            return None


class Rejected(Exception):
    """The orchestrator refused a request for good (4xx): do not retry."""


@dataclass
class Active:
    assignment: dict[str, Any]
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    # Set when the orchestrator, not this process, asked the run to stop.
    killed: bool = False
    concurrency: int = 0
    requests: int = 0


@dataclass(frozen=True)
class NodeSettings:
    """What a NodeAgent is built from: the orchestrator URL and bearer token, then the node's
    provider product, region or host, and its intervals (seconds)."""

    orchestrator: str
    token: str
    _: KW_ONLY
    provider: str
    product: str
    proxy_factory: ProxyFactory
    work_dir: str | Path
    region: str | None = None
    host: str | None = None
    enricher: Enricher | None = None
    mmdb: str | Path | None = None
    poll_interval: float = 5.0
    heartbeat_interval: float = 10.0
    progress_interval: float = 30.0
    transport_factory: Callable[[RunConfig], Any] = HttpTransport
    upload_backoff: float = 2.0


class NodeAgent:
    # The targets a node runs; a subclass (the browser host, browser/node.py) replaces them.
    registry: TargetRegistry = registry
    # The session's TLS check (node_tls.client_tls): True for the public CAs, or the context
    # pinning the manager's certificate; set before run().
    tls: ssl.SSLContext | bool = True

    def __init__(self, settings: NodeSettings) -> None:
        if not settings.region and not settings.host:
            raise ValueError("A node needs --region or --host-name")
        self.base = settings.orchestrator.rstrip("/")
        self._headers = {"Authorization": f"Bearer {settings.token}"}
        self.provider, self.product = settings.provider, settings.product
        self.region, self.host = settings.region, settings.host
        self.proxy_factory = settings.proxy_factory
        self.work_dir = Path(settings.work_dir)
        self.enricher, self.mmdb = settings.enricher, settings.mmdb
        self.poll_interval = settings.poll_interval
        self.heartbeat_interval = settings.heartbeat_interval
        self.progress_interval = settings.progress_interval
        self.transport_factory = settings.transport_factory
        self.upload_backoff = settings.upload_backoff
        self.active: dict[int, Active] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.node_id: int | None = None
        self.session: aiohttp.ClientSession | None = None
        self._rate_mark = (time.monotonic(), 0)

    # --- HTTP -----------------------------------------------------------------------------

    async def _call(self, method: str, path: str, **options: Any) -> dict[str, Any]:
        assert self.session is not None
        headers = self._headers | options.pop("headers", {})
        async with self.session.request(
            method, self.base + path, headers=headers, **options
        ) as response:
            text = await response.text()
            if 400 <= response.status < 500 and response.status not in (408, 429):
                raise Rejected(f"{method} {path} answered {response.status}: {text[:200]}")
            response.raise_for_status()
            return json.loads(text) if text else {}

    async def register(self) -> int:
        body = {"provider": self.provider, "product": self.product, "version": __version__}
        if self.region:
            body["region"] = self.region
        if self.host:
            body["host"] = self.host
        result = await self._call("POST", "/v1/register", json=body)
        self.node_id = result["node_id"]
        self.region = result["region"]
        log.info("registered as node %s in %s", self.node_id, self.region)
        return self.node_id

    # --- loops ----------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Serve until `stop` is set; in-flight runs are then stopped and reported failed."""
        self.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Results are ingested before the reply, which can take minutes for a large census.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=1800)
        connector = aiohttp.TCPConnector(ssl=self.tls)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as self.session:
            while not stop.is_set():
                try:
                    await self.register()
                    break
                except Rejected:
                    raise
                except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                    if isinstance(exc, aiohttp.ClientConnectorCertificateError):
                        log.error("register refused: the orchestrator's TLS certificate is not "
                                  "the trusted one (--ca-file); retrying")  # fmt: skip
                    else:
                        log.warning("register failed (%s); retrying", type(exc).__name__)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), self.poll_interval)
            beat = asyncio.create_task(self._heartbeats(stop))
            try:
                while not stop.is_set():
                    await self.reopen_database()
                    await self.poll()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), self.poll_interval)
            finally:
                for active in self.active.values():
                    active.stop.set()
                if self.tasks:
                    await asyncio.gather(*self.tasks, return_exceptions=True)
                beat.cancel()
                await asyncio.gather(beat, return_exceptions=True)

    async def reopen_database(self) -> None:
        """Follow `dbip update`: reopen the MMDB when its link moved, only while no run is
        active (poll() alone starts runs, and it waits for this), so a run keeps one release."""
        reload = getattr(self.enricher, "reload", None)
        if self.active or reload is None:
            return
        try:
            if await asyncio.to_thread(reload):
                log.info("DB-IP database reopened: %s", self.enricher.release["sha256"][:12])
        except Exception as exc:  # noqa: BLE001 - the open release stays in use
            log.warning("could not reopen the DB-IP database (%s)", type(exc).__name__)

    async def poll(self) -> None:
        try:
            work = await self._call("GET", "/v1/work")
        except (aiohttp.ClientError, OSError, TimeoutError, Rejected) as exc:
            log.warning("work poll failed (%s)", type(exc).__name__)
            return
        for batch_id in work.get("kill", []):
            if batch_id in self.active:
                self.active[batch_id].killed = True
                self.active[batch_id].stop.set()
        for assignment in work.get("assignments", []):
            if assignment["batch_id"] in self.active:
                continue
            active = Active(assignment)
            self.active[assignment["batch_id"]] = active
            task = asyncio.create_task(self.execute(active))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def _heartbeats(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.heartbeat()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.heartbeat_interval)

    async def heartbeat(self) -> None:
        now = time.monotonic()
        total = sum(active.requests for active in self.active.values())
        then, before = self._rate_mark
        self._rate_mark = (now, total)
        rate = max(total - before, 0) / (now - then) if now > then else 0.0
        body = {
            # In-flight connections, approximated by the concurrency of running assignments.
            "open_conns": sum(active.concurrency for active in self.active.values()),
            "req_per_s": round(rate, 3),
            "rss_mb": resident_mb(),
            "version": __version__,
        }
        try:
            await self._call("POST", "/v1/heartbeat", json=body)
        except (aiohttp.ClientError, OSError, TimeoutError, Rejected) as exc:
            log.warning("heartbeat failed (%s)", type(exc).__name__)

    # --- one assignment -------------------------------------------------------------------

    def targets(self, assignment: dict[str, Any]) -> TargetRegistry:
        """The code registry, or for a row added to the `targets` table that this build does not
        know, a one-target registry with an echo-IP target at the row's URL: pulse picks new
        rows up by itself, so the node must be able to run them. Pool eligibility follows the
        built-in echo target of the same scheme (generic_http / generic_https)."""
        target_id = assignment["target_id"]
        try:
            self.registry.get(target_id)
            return self.registry
        except ValueError:
            url = assignment.get("target_url")
            scheme = urlsplit(url).scheme if isinstance(url, str) else None
            twin = {"http": "generic_http", "https": "generic_https"}.get(scheme or "")
            if twin is None:
                raise
            adhoc = TargetRegistry()
            adhoc.register(
                EchoIpTarget(target_id, url, pool_eligible=registry.get(twin).pool_eligible)
            )
            return adhoc

    def config_options(self, assignment: dict[str, Any]) -> dict[str, Any]:
        """RunConfig fields replacing the assignment's (a subclass hook); raising refuses."""
        return {}

    def run_options(self, assignment: dict[str, Any]) -> dict[str, Any]:
        """Extra RunOptions fields for runner.run, such as `deadline` (a subclass hook)."""
        return {}

    def result_sink(self, assignment: dict[str, Any]) -> JsonSink:
        """The run's sink, whose directory upload() sends (a subclass hook)."""
        return JsonSink(self.work_dir)

    def config(self, assignment: dict[str, Any]) -> RunConfig:
        overrides = self.config_options(assignment)
        proxy = self.proxy_factory(assignment["country_code"])
        target = self.targets(assignment).get(assignment["target_id"])
        url = assignment.get("target_url")
        # An ad-hoc target already carries the row's URL, so it never takes an override.
        if not isinstance(target, EchoIpTarget) or url == target.url:
            url = None
        # Provider adapters label the requested country themselves (Shifter: lowercase).
        country = None if "requested_country" in proxy.public() else assignment["country_code"]
        options: dict[str, Any] = {}
        for name in ("min_sample", "stall_seconds"):
            if assignment.get(name):
                options[name] = assignment[name]
        config = RunConfig(
            target=assignment["target_id"],
            proxy=proxy,
            requests=assignment.get("requests"),
            concurrency=assignment["concurrency"],
            timeout_seconds=assignment["timeout_ms"] / 1000,
            provider=self.provider,
            product=self.product,
            requested_country=country.lower() if country else None,
            runner_location=self.region,
            target_url=url,
            stop_rule=assignment["stop_rule"],
            **options,
        )
        return replace(config, **overrides) if overrides else config

    async def _wait_until(self, moment: datetime, stop: asyncio.Event) -> None:
        while not stop.is_set():
            remaining = (moment - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                return
            # Re-check the wall clock: a long sleep on the monotonic clock can drift.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), min(remaining, 1.0))

    async def _progress(self, active: Active, counters: dict[str, int]) -> None:
        active.requests = counters.get("requests", 0)
        batch_id = active.assignment["batch_id"]
        try:
            reply = await self._call(
                "POST", "/v1/progress", json={"batch_id": batch_id, "counters": counters}
            )
        except Rejected:
            # The orchestrator no longer knows this run as ours: stop it.
            reply = {"stop": True}
        if reply.get("stop") and not active.stop.is_set():
            log.info("orchestrator stopped batch %s", batch_id)
            active.killed = True
            active.stop.set()

    async def execute(self, active: Active) -> None:
        assignment = active.assignment
        batch_id = assignment["batch_id"]
        directory: Path | None = None
        # Killed and interrupted runs are not uploaded; their directory goes.
        discard = False
        try:
            start_at = datetime.fromisoformat(assignment["start_at"])
            await self._wait_until(start_at, active.stop)
            if active.stop.is_set():
                # Not started yet: the orchestrator hands an unstarted run out again
                # after a restart, so a deploy just before a census slot loses nothing.
                return
            config = self.config(assignment)
            extra = self.run_options(assignment)
            active.concurrency = config.concurrency
            log.info(
                "batch %s: %s %s %s from %s",
                batch_id,
                assignment["kind"],
                assignment["target_id"],
                assignment["country_code"],
                start_at.isoformat(),
            )
            # The first progress post marks the run (and the batch) started.
            await self._progress(active, {"requests": 0})
            sink = self.result_sink(assignment)
            try:
                options = RunOptions(
                    stop=active.stop,
                    progress=lambda counters: self._progress(active, counters),
                    progress_interval=self.progress_interval,
                    enricher=self.enricher,
                    targets=self.targets(assignment),
                    **{"transport_factory": self.transport_factory} | extra,
                )
                summary = await run(config, sink, options)
            finally:
                directory = sink.directory
                active.concurrency = 0
            if active.killed:
                discard = True
                return
            if active.stop.is_set() and summary.status == "cancelled":
                discard = True
                await self._fail(batch_id, "node_shutdown")
                return
            if self.mmdb and summary.pool_eligible:
                from .enrichment import enrich_run

                await asyncio.to_thread(enrich_run, directory, self.mmdb)
            await self.upload(batch_id, directory)
        except Rejected as exc:
            log.warning("batch %s: %s", batch_id, exc)
        except Exception as exc:  # noqa: BLE001 - report the type only; it may carry secrets
            log.warning("batch %s failed (%s)", batch_id, type(exc).__name__)
            await self._fail(batch_id, f"node error ({type(exc).__name__})")
        finally:
            self.active.pop(batch_id, None)
            if discard and directory is not None:
                await asyncio.to_thread(shutil.rmtree, directory, True)

    async def _fail(self, batch_id: int, error: str) -> None:
        try:
            await self._call("POST", "/v1/failed", json={"batch_id": batch_id, "error": error})
        except (aiohttp.ClientError, OSError, TimeoutError, Rejected) as exc:
            log.warning("could not report batch %s failed (%s)", batch_id, type(exc).__name__)

    async def upload(self, batch_id: int, directory: Path) -> dict[str, Any]:
        """POST the run directory as tar.gz, retrying transient failures with backoff."""
        with tempfile.TemporaryDirectory(prefix="proxy-index-node-") as temporary:
            archive = await asyncio.to_thread(pack, directory, Path(temporary) / "run.tar.gz")
            for attempt in range(UPLOAD_ATTEMPTS):
                # Streamed from disk (aiohttp reads the file in its executor); reopened per try.
                stream = await asyncio.to_thread(archive.open, "rb")
                try:
                    reply = await self._call(
                        "POST",
                        "/v1/results",
                        params={"batch_id": str(batch_id)},
                        data=stream,
                        headers={"Content-Type": "application/gzip"},
                    )
                except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                    delay = self.upload_backoff * 2**attempt * (1 + random.random() / 4)
                    log.warning(
                        "upload of batch %s failed (%s); retry in %.0fs",
                        batch_id,
                        type(exc).__name__,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                finally:
                    await asyncio.to_thread(stream.close)
                await asyncio.to_thread(shutil.rmtree, directory, True)
                log.info("batch %s uploaded: %s", batch_id, reply.get("status"))
                return reply
        raise RuntimeError("upload attempts exhausted")


async def serve(agent: NodeAgent) -> None:
    import signal

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await agent.run(stop)
