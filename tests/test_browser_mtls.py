"""BROWSER-MTLS-1: the orchestrator's browser port admits a browser host only with a client
certificate of the browser CA whose CN is its token's host, and serves only the node API there.

The real orchestrator app (with a database) listens on two loopback ports, the node API port and
the browser port, as `serve` starts them; the clients use node_tls.client_tls as the hosts do."""

from __future__ import annotations

import asyncio
import os
import signal
import ssl
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from helpers import client_ca, client_certificate, self_signed
from orchestrator_cases import bearer, prepare

from atlas.errors import UserError
from atlas.node_tls import client_tls
from atlas.orchestrator.api import BROWSER_TLS_KEY
from atlas.orchestrator.app import (
    browser_tls,
    build_app,
    server_tls,
    start_sites,
    until_signal,
)
from atlas.orchestrator.db import Database
from atlas.orchestrator.nodes import issue_token
from atlas.storage import connect

pytest_plugins = ["test_storage"]

REGISTER = {"provider": "alpha", "product": "res", "host": "browser-1"}
ADMIN = {"Authorization": aiohttp.encode_basic_auth("admin", "pw")}


@pytest.fixture
async def manager(database, tmp_path):
    """The orchestrator on its node API port and its browser port, a browser host's token
    (browser-1) and a pulse node's token, and the browser CA."""
    prepare(database)
    cert, key = self_signed(tmp_path, "manager")
    ca_path, ca = client_ca(tmp_path, "browser")
    tls = (str(cert), str(key))
    browser = browser_tls(tls, (0, str(ca_path)))
    assert browser is not None and browser.verify_mode == ssl.CERT_REQUIRED
    db = Database(database)
    app = build_app(database, with_scheduler=False, db=db, admin_credential="admin:pw")
    app[BROWSER_TLS_KEY] = browser
    sites = [("127.0.0.1", 0, server_tls(*tls)), ("127.0.0.1", 0, browser)]
    runner = await start_sites(app, sites)
    node_port, browser_port = (address[1] for address in runner.addresses)
    try:
        yield SimpleNamespace(
            database=database,
            cert=cert,
            ca=ca,
            tmp=tmp_path,
            node_url=f"https://127.0.0.1:{node_port}",
            browser_url=f"https://127.0.0.1:{browser_port}",
            host=issue_token(
                database, provider="alpha", product="res", region="browser", host="browser-1"
            )["token"],
            pulse=issue_token(database, provider="alpha", product="res", region="fra1")["token"],
        )
    finally:
        await runner.cleanup()
        await db.close()


def presenting(manager, common_name: str | None, ca=None) -> ssl.SSLContext | bool:
    """The host's TLS value: the manager's certificate pinned, and a client certificate for
    `common_name` signed by `ca` (the browser CA by default); none for None."""
    if common_name is None:
        return client_tls(manager.browser_url, manager.cert)
    cert, key = client_certificate(manager.tmp, ca or manager.ca, common_name)
    return client_tls(manager.browser_url, manager.cert, (cert, key))


async def call(url, tls, path="/v1/register", token=None, body=REGISTER, auth=None):
    headers = bearer(token) if token else auth or {}
    async with aiohttp.ClientSession() as session:
        method = "POST" if path.startswith("/v1/") else "GET"
        async with session.request(
            method, url + path, json=body, headers=headers, ssl=tls
        ) as response:
            return response.status, await response.json(content_type=None)


def heartbeats(database):
    with connect(database) as conn:
        return conn.execute("SELECT last_heartbeat FROM nodes WHERE host='browser-1'").fetchone()


async def test_a_host_with_its_certificate_and_token_registers(manager):
    tls = presenting(manager, "browser-1")
    status, body = await call(manager.browser_url, tls, token=manager.host)
    assert (status, body["region"]) == (200, "browser")
    assert heartbeats(manager.database)[0] is not None


async def test_a_handshake_without_a_certificate_of_the_browser_ca_fails(manager):
    _, other = client_ca(manager.tmp, "other")
    for tls in (presenting(manager, None), presenting(manager, "browser-1", ca=other)):
        with pytest.raises(aiohttp.ClientError):
            await call(manager.browser_url, tls, token=manager.host)
    assert heartbeats(manager.database) == (None,)  # no route ran


async def test_the_certificate_must_name_the_tokens_host(manager):
    tls = presenting(manager, "browser-2")
    status, body = await call(manager.browser_url, tls, token=manager.host)
    assert (status, body) == (403, {"error": "the client certificate is not the token's host"})
    assert heartbeats(manager.database) == (None,)


async def test_the_browser_port_takes_only_browser_host_tokens(manager):
    tls = presenting(manager, "browser-1")
    body = REGISTER | {"region": "fra1"}
    status, answer = await call(manager.browser_url, tls, token=manager.pulse, body=body)
    assert (status, answer) == (403, {"error": "the browser port takes only browser host tokens"})
    status, _ = await call(manager.browser_url, tls, token="not-a-token")
    assert status == 401


async def test_the_browser_port_serves_no_admin_and_no_health(manager):
    tls = presenting(manager, "browser-1")
    for path in ("/admin/api/health", "/health"):
        status, body = await call(manager.browser_url, tls, path=path, auth=ADMIN)
        assert (status, body) == (404, {"error": "the browser port serves only the node API"})
    pinned = client_tls(manager.node_url, manager.cert)  # the node API port is unchanged
    assert (await call(manager.node_url, pinned, path="/admin/api/health", auth=ADMIN))[0] == 200
    assert (await call(manager.node_url, pinned, path="/health"))[0] == 200
    status, _ = await call(manager.node_url, pinned, token=manager.host)
    assert status == 200  # no client certificate asked there


async def test_until_signal_serves_until_sigterm_and_cleans_up():
    app, cleaned = web.Application(), []

    async def stop_soon(_app):
        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)

    async def cleanup(_app):
        cleaned.append(True)

    app.on_startup.append(stop_soon)
    app.on_cleanup.append(cleanup)
    await asyncio.wait_for(until_signal(app, [("127.0.0.1", 0, None)]), 5)
    assert cleaned == [True]


async def test_a_site_that_cannot_start_cleans_the_runner_up(tmp_path):
    app, cleaned = web.Application(), []

    async def cleanup(_app):
        cleaned.append(True)

    app.on_cleanup.append(cleanup)
    with pytest.raises(OSError):
        await start_sites(app, [("192.0.2.1", 0, None)])  # not an address of this machine
    assert cleaned == [True]


# --- the host's side: node_tls.client_tls with a client certificate --------------------------


def test_the_client_files_must_be_owner_only_pem(tmp_path):
    cert, key = self_signed(tmp_path, "manager")
    _, ca = client_ca(tmp_path, "browser")
    mine, mine_key = client_certificate(tmp_path, ca, "browser-1")
    url = "https://203.0.113.9:8444"
    context = client_tls(url, cert, (mine, mine_key))
    assert isinstance(context, ssl.SSLContext) and context.check_hostname
    public = client_tls(url, None, (mine, mine_key))  # public CAs, the certificate presented
    assert isinstance(public, ssl.SSLContext) and public.verify_mode == ssl.CERT_REQUIRED
    mine.chmod(0o644)
    with pytest.raises(UserError, match="client certificate file must be owned"):
        client_tls(url, cert, (mine, mine_key))
    mine.chmod(0o600)
    with pytest.raises(UserError, match="client key file cannot be read"):
        client_tls(url, cert, (mine, tmp_path / "missing.key"))
    with pytest.raises(UserError, match="not a valid PEM pair"):
        client_tls(url, cert, (mine, key))  # the manager's key, not the certificate's
    with pytest.raises(UserError, match="needs an https:// URL"):
        client_tls("http://127.0.0.1:8080", None, (mine, mine_key))
