"""The orchestrator process (orchestrator/app.py): what `serve` hands to aiohttp, the scheduler an
app starts and stops with itself, the pool it closes when it made it, and the orchestrator
command without a database file."""

import asyncio
import logging
import ssl

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from helpers import client_ca, self_signed

from atlas.cli import main
from atlas.orchestrator import app as app_module
from atlas.orchestrator.api import BROWSER_TLS_KEY, CREDENTIALS_KEY, DB_KEY
from atlas.orchestrator.app import SCHEDULER_KEY, build_app, serve

pytest_plugins = ["test_storage"]


@pytest.fixture
def credentials(tmp_path):
    """A database file that is read, never connected to: serving (until_signal) is stubbed."""
    path = tmp_path / "database.toml"
    path.write_text(
        '[postgres]\nhost = "127.0.0.1"\nport = "5432"\ndbname = "d"\nuser = "u"\npassword = "p"\n'
    )
    path.chmod(0o600)
    return str(path)


@pytest.fixture
def run_app(monkeypatch):
    calls = []

    async def served(app, sites):
        calls.append((app, sites))

    monkeypatch.setattr(app_module, "until_signal", served)
    psycopg_log = logging.getLogger("psycopg")
    level = psycopg_log.level
    psycopg_log.setLevel(logging.DEBUG)
    yield calls
    psycopg_log.setLevel(level)


def test_serve_hands_the_app_address_and_tls_to_aiohttp(
    tmp_path, credentials, run_app, monkeypatch
):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    cert, key = self_signed(tmp_path, "orchestrator")
    serve(credentials, host="::1", port=8443, dev=True, tls=(str(cert), str(key)))
    ((app, sites),) = run_app
    assert isinstance(app, web.Application) and BROWSER_TLS_KEY not in app
    assert app[CREDENTIALS_KEY] == credentials and "census_plan" in app[SCHEDULER_KEY].tasks
    ((host, port, context),) = sites
    assert (host, port) == ("::1", 8443)
    assert isinstance(context, ssl.SSLContext) and context.verify_mode == ssl.CERT_NONE
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    # Statement logs stay out even in dev.
    assert logging.getLogger("psycopg").level == logging.INFO


def test_serve_outside_dev_with_a_password_answers_plain_http(credentials, run_app, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    serve(credentials, host="0.0.0.0", port=8080)
    ((_, sites),) = run_app
    assert sites == [("0.0.0.0", 8080, None)]


def test_serve_refuses_tls_with_half_a_pair_before_aiohttp(credentials, run_app):
    with pytest.raises(ValueError, match="go together"):
        serve(credentials, dev=True, tls=("cert.pem", None))
    with pytest.raises(ValueError, match="--browser-port needs --tls-cert and --tls-key"):
        serve(credentials, dev=True, browser=(8444, "ca.pem"))
    assert run_app == []


def test_serve_adds_the_browser_port_with_client_certificates(
    tmp_path, credentials, run_app, monkeypatch
):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    cert, key = self_signed(tmp_path, "orchestrator")
    ca, _ = client_ca(tmp_path, "browser")
    tls = (str(cert), str(key))
    serve(credentials, port=8443, dev=True, tls=tls, browser=(8444, str(ca)))
    ((app, sites),) = run_app
    (_, node_port, node_tls), (host, port, browser) = sites
    assert (node_port, node_tls.verify_mode) == (8443, ssl.CERT_NONE)
    assert (host, port, browser.verify_mode) == ("127.0.0.1", 8444, ssl.CERT_REQUIRED)
    assert app[BROWSER_TLS_KEY] is browser and browser.minimum_version == ssl.TLSVersion.TLSv1_2
    assert [c["subject"] for c in browser.get_ca_certs()] == [((("commonName", "browser-ca"),),)]


async def test_the_app_starts_its_scheduler_and_closes_the_pool_it_made(database):
    app = build_app(database, admin_credential=None)
    client = TestClient(TestServer(app))
    await client.start_server()
    scheduler = app[SCHEDULER_KEY]
    try:
        for _ in range(50):
            if all(task.next_at for task in scheduler.tasks.values()):
                break
            await asyncio.sleep(0.01)
        assert scheduler._runner is not None and not scheduler._runner.done()
        assert all(task.next_at for task in scheduler.tasks.values())
        async with app[DB_KEY].connection() as conn:
            pass
        assert not conn.closed
    finally:
        await client.close()
    # Cleanup stopped the clock and closed the pool's idle connection.
    assert scheduler._runner is None
    assert conn.closed


def test_the_orchestrator_command_needs_a_database_file(monkeypatch, capsys):
    monkeypatch.delenv("ATLAS_DATABASE_FILE", raising=False)
    served = []
    monkeypatch.setattr(app_module, "serve", lambda *args, **kw: served.append(args))
    assert main(["orchestrator", "--dev"]) == 2
    # Refused before serving; the CLI names only the error's type.
    assert served == []
    assert "Command failed (ValueError)" in capsys.readouterr().err
