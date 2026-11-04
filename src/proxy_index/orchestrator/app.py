"""The orchestrator process: node API, scheduler and the admin JSON API in one app.

The admin routes (admin/routes.py, /admin/api) bring their own Basic-auth middleware for their
path prefix, as node_auth only guards /v1/. ADMIN_USER / ADMIN_PASSWORD come from the
environment; without a password the admin answers only over loopback in --dev, and `serve`
refuses to start outside --dev. With a certificate and key, `serve` answers HTTPS only (the
manager's one node API port, MANAGER-ORCH-1).
"""

from __future__ import annotations

import logging
import ssl
from pathlib import Path

from aiohttp import web

from .admin import routes as admin
from .api import CREDENTIALS_KEY, DB_KEY, add_routes, node_auth
from .db import Database
from .jobs import register_jobs
from .scheduler import Scheduler

log = logging.getLogger(__name__)

SCHEDULER_KEY: web.AppKey[Scheduler] = web.AppKey("scheduler", Scheduler)
LOOPBACK = ("127.0.0.1", "localhost", "::1")


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
        if db is None:
            await database.close()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


def server_tls(cert: str | None, key: str | None) -> ssl.SSLContext | None:
    """The HTTPS context for a certificate and its key (TLS 1.2 or later); None for neither."""
    if not cert and not key:
        return None
    if not (cert and key):
        raise ValueError("--tls-cert and --tls-key go together")
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


def serve(
    credentials_file: str,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    dev: bool = False,
    methodology: str | Path | None = None,
    tls: tuple[str | None, str | None] = (None, None),
) -> None:
    """Run until interrupted. --dev logs verbosely and refuses to bind beyond loopback; `tls`
    (certificate, key) serves HTTPS."""
    context = server_tls(*tls)
    if dev and host not in LOOPBACK:
        raise ValueError(f"--dev refuses to bind {host}")
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
    web.run_app(
        build_app(credentials_file, dev=dev, methodology=methodology),
        host=host,
        port=port,
        ssl_context=context,
        print=None,
    )
