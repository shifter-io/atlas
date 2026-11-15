"""The orchestrator process: node API, scheduler and the admin JSON API in one app.

The admin routes (admin/routes.py, /admin/api) bring their own Basic-auth middleware for their
path prefix, as node_auth only guards /v1/. ADMIN_USER / ADMIN_PASSWORD come from the
environment; without a password the admin answers only over loopback in --dev, and `serve`
refuses to start outside --dev. With a certificate and key, `serve` answers HTTPS only (the
manager's one node API port, MANAGER-ORCH-1). With a browser port and a client CA as well,
the same app also listens there for the browser hosts (BROWSER-MTLS-1): HTTPS that requires a
client certificate of that CA, the node API only (api.node_auth). The admin API has a listener
of its own (SEC-MANAGER-1): `Listeners.admin` (host, port), loopback and the node port + 3 by
default, with the node port's TLS; the node and browser ports answer 404 for it.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import ssl
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from . import pulse_enrich
from .admin import routes as admin
from .api import ADMIN_PORT_KEY, BROWSER_TLS_KEY, CREDENTIALS_KEY, DB_KEY, add_routes, node_auth
from .db import Database
from .jobs import register_jobs
from .scheduler import Scheduler

log = logging.getLogger(__name__)

SCHEDULER_KEY: web.AppKey[Scheduler] = web.AppKey("scheduler", Scheduler)
LOOPBACK = ("127.0.0.1", "localhost", "::1")
ADMIN_PORT_OFFSET = 3  # the admin port when none is given: 8443 gives 8446, 9443 gives 9446


def build_app(
    credentials_file: str,
    *,
    dev: bool = False,
    with_scheduler: bool = True,
    methodology: str | Path | None = None,
    db: Database | None = None,
    admin_credential: str | None | bool = True,
) -> web.Application:
    """Wire the process. With `with_scheduler`, the jobs start when the app starts.

    `admin_credential` is the admin's `user:password`; True (the default) reads ADMIN_USER and
    ADMIN_PASSWORD from the environment, None means no password.
    """
    app = web.Application(middlewares=[node_auth])
    database = db or Database(credentials_file)
    app[DB_KEY] = database
    app[CREDENTIALS_KEY] = str(credentials_file)
    enricher = pulse_enrich.configured()  # $DBIP_MMDB, for the pulse uploads
    if enricher is not None:
        app[pulse_enrich.ENRICHER_KEY] = enricher
    scheduler = Scheduler(database)
    register_jobs(
        scheduler, database, str(credentials_file), Path(methodology) if methodology else None
    )
    app[SCHEDULER_KEY] = scheduler
    add_routes(app)
    admin.setup(
        app,
        dev=dev,
        credential=admin.credentials_from_env()
        if admin_credential is True
        else admin_credential or None,
        scheduler=scheduler,
    )

    async def startup(_: web.Application) -> None:
        if with_scheduler:
            scheduler.start()
        log.info("orchestrator ready%s", " (dev)" if dev else "")

    async def cleanup(_: web.Application) -> None:
        await scheduler.stop()
        if enricher is not None:
            enricher.close()
        if db is None:
            await database.close()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


def server_tls(
    cert: str | None, key: str | None, client_ca: str | None = None
) -> ssl.SSLContext | None:
    """The HTTPS context for a certificate and its key (TLS 1.2 or later); None for neither.
    With `client_ca`, the handshake requires a client certificate signed by that CA alone."""
    if not cert and not key:
        return None
    if not (cert and key):
        raise ValueError("--tls-cert and --tls-key go together")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)  # no public CA can vouch for a client
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    if client_ca:
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=client_ca)
    return context


Site = tuple[str, int, ssl.SSLContext | None]


async def start_sites(app: web.Application, sites: list[Site]) -> web.AppRunner:
    """Start serving `app` on every (host, port, TLS context); the caller cleans the runner up."""
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        for host, port, context in sites:
            await web.TCPSite(runner, host, port, ssl_context=context).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner


async def until_signal(app: web.Application, sites: list[Site]) -> None:
    """Serve until SIGINT or SIGTERM, then stop the sites and run the app's cleanup."""
    stop, loop = asyncio.Event(), asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    runner = await start_sites(app, sites)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


Browser = tuple[int | None, str | None]
Admin = tuple[str, int | None]


@dataclass(frozen=True)
class Listeners:
    """Where `serve` listens: the node API's host and port, the browser port (port, client CA)
    if any, and the admin API's (host, port), port None being the node port + 3."""

    host: str = "127.0.0.1"
    port: int = 8080
    browser: Browser | None = None
    admin: Admin = ("127.0.0.1", None)


def listen(app: web.Application, node: Site, browser: Site | None, admin: Site) -> list[Site]:
    """The sites to serve: the node port, the browser port if any, and the admin port, each
    marked on the app so node_auth knows which listener a request came in on."""
    ports = [node[1], admin[1]] + ([browser[1]] if browser else [])
    if len(set(ports)) != len(ports):
        raise ValueError("the node, browser and admin ports must differ")
    app[ADMIN_PORT_KEY] = admin[1]
    if browser is None:
        return [node, admin]
    if browser[2] is not None:
        app[BROWSER_TLS_KEY] = browser[2]
    return [node, browser, admin]


def browser_tls(
    tls: tuple[str | None, str | None], browser: Browser | None
) -> ssl.SSLContext | None:
    """The browser port's context (the server's certificate, client certificates of the CA);
    None without a browser port (port, client CA), refused with half of one."""
    port, client_ca = browser or (None, None)
    if port is None and not client_ca:
        return None
    if port is None or not client_ca:
        raise ValueError("--browser-port and --client-ca go together")
    if not (tls[0] and tls[1]):
        raise ValueError("--browser-port needs --tls-cert and --tls-key")
    return server_tls(*tls, client_ca=client_ca)


def serve(
    credentials_file: str,
    at: Listeners | None = None,
    *,
    dev: bool = False,
    methodology: str | Path | None = None,
    tls: tuple[str | None, str | None] = (None, None),
) -> None:
    """Run until SIGINT or SIGTERM on the listeners `at`. --dev logs verbosely and refuses to
    bind beyond loopback; `tls` (certificate, key) serves HTTPS on every port."""
    at = at or Listeners()
    host, port, browser = at.host, at.port, at.browser
    context, browser_context = server_tls(*tls), browser_tls(tls, browser)
    admin_host, admin_port = at.admin
    admin_site = (admin_host, admin_port or port + ADMIN_PORT_OFFSET, context)
    for bound in (host, admin_site[0]):
        if dev and bound not in LOOPBACK:
            raise ValueError(f"--dev refuses to bind {bound}")
    if not dev and admin.credentials_from_env() is None:
        raise ValueError(
            "ADMIN_PASSWORD is required outside --dev: an unprotected admin is the easiest "
            "hole to open by accident"
        )
    logging.basicConfig(
        level=logging.DEBUG if dev else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # psycopg logs every statement at DEBUG; keep it quiet even in dev.
    logging.getLogger("psycopg").setLevel(logging.INFO)
    app = build_app(credentials_file, dev=dev, methodology=methodology)
    browser_site = None
    if browser and browser[0] is not None and browser_context:
        browser_site = (host, browser[0], browser_context)
    sites = listen(app, (host, port, context), browser_site, admin_site)
    asyncio.run(until_signal(app, sites))
