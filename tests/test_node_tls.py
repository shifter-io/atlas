"""NODE-TLS-1: nodes trust the manager's own self-signed certificate, and only it.

A real HTTPS orchestrator stand-in on 127.0.0.1 serves a self-signed certificate made as the
manager makes its own; a node registers only when it pins exactly that certificate."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp import web
from helpers import self_signed

from atlas import enrichment
from atlas import node as node_module
from atlas.cli import main
from atlas.errors import UserError
from atlas.node import NodeAgent, NodeSettings
from atlas.node_tls import client_tls, pinned_context
from atlas.orchestrator.app import server_tls


@asynccontextmanager
async def manager(cert: Path, key: Path) -> AsyncIterator[tuple[str, list[str]]]:
    """An HTTPS orchestrator stand-in with the manager's TLS context; yields its URL and the
    paths it answered."""
    answered: list[str] = []

    async def answer(request: web.Request) -> web.Response:
        answered.append(request.path)
        if request.path == "/v1/register":
            return web.json_response({"node_id": 1, "region": "fra1"})
        return web.json_response({})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", answer)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_tls(str(cert), str(key)))
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"https://127.0.0.1:{port}", answered
    finally:
        await runner.cleanup()


async def serve_briefly(tmp_path: Path, url: str, tls: ssl.SSLContext | bool) -> NodeAgent:
    """Run a node against `url` for half a second: long enough to register or be refused."""
    agent = NodeAgent(
        NodeSettings(
            url,
            "token-for-tests-only",
            provider="shifter",
            product="residential",
            region="fra1",
            proxy_factory=lambda country: None,
            work_dir=tmp_path / "runs",
            poll_interval=0.05,
            heartbeat_interval=60,
        )
    )
    agent.tls = tls
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.5, stop.set)
    await asyncio.wait_for(agent.run(stop), 10)
    return agent


async def test_a_node_that_pins_the_managers_certificate_registers(tmp_path):
    cert, key = self_signed(tmp_path, "manager")
    async with manager(cert, key) as (url, answered):
        agent = await serve_briefly(tmp_path, url, client_tls(url, cert))
    assert agent.node_id == 1 and answered[0] == "/v1/register"


@pytest.mark.parametrize("pinned", ["none", "another", "wrong-address"])
async def test_a_node_refuses_a_certificate_it_does_not_pin(tmp_path, caplog, pinned):
    """No --ca-file (public CAs only), another self-signed certificate with the same name, or
    the right one made for another address: the node never sends a request."""
    address = "203.0.113.9" if pinned == "wrong-address" else "127.0.0.1"
    cert, key = self_signed(tmp_path, "manager", address)
    other, _ = self_signed(tmp_path, "other")
    ca = {"none": None, "another": other, "wrong-address": cert}[pinned]
    caplog.set_level(logging.ERROR, logger="atlas.node")
    async with manager(cert, key) as (url, answered):
        agent = await serve_briefly(tmp_path, url, client_tls(url, ca))
    assert agent.node_id is None and answered == []
    assert "TLS certificate is not the trusted one" in caplog.text


def test_the_pinned_context_trusts_only_the_file(tmp_path):
    cert, _ = self_signed(tmp_path, "manager")
    context = pinned_context(cert)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert len(context.get_ca_certs()) == 1  # no public CA is loaded beside it


@pytest.mark.parametrize(
    "url, ca, message",
    [
        ("http://203.0.113.9:8080", None, "plain http:// is refused"),
        ("http://203.0.113.9:8080", "manager", "needs an https://"),
        ("http://127.0.0.1:8080", "manager", "needs an https://"),
        ("ftp://203.0.113.9", None, "must start with https://"),
        ("https://", None, "must start with https://"),
        ("https://203.0.113.9:8443", "missing", "cannot be read"),
        ("https://203.0.113.9:8443", "two", "exactly one PEM certificate"),
        ("https://203.0.113.9:8443", "garbage", "not a valid PEM certificate"),
    ],
)
def test_urls_and_files_a_node_must_not_use_are_refused(tmp_path, url, ca, message):
    cert, _ = self_signed(tmp_path, "manager")
    (tmp_path / "two").write_bytes(cert.read_bytes() * 2)
    (tmp_path / "garbage").write_bytes(b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END")
    path = {"manager": cert, None: None}.get(ca, tmp_path / str(ca))
    with pytest.raises(UserError, match=message) as refused:
        client_tls(url, path)
    assert "203.0.113.9" not in str(refused.value)  # a UserError names no URL


@pytest.mark.parametrize(
    "url", ["https://203.0.113.9:8443", "http://127.0.0.1:8080", "http://localhost:8080"]
)
def test_public_https_and_local_http_need_no_ca_file(url):
    assert client_tls(url) is True


def node_argv(url: str, *extra: str) -> list[str]:
    return [
        "node", "--orchestrator", url, "--token-file", "token", "--provider", "shifter",
        "--product", "residential", "--region", "fra1", "--provider-config", "p.toml", *extra,
    ]  # fmt: skip


def test_the_node_cli_refuses_plain_http_to_a_remote_orchestrator(capsys):
    assert main(node_argv("http://203.0.113.9:8080")) == 2
    assert "plain http:// is refused" in capsys.readouterr().err


def test_the_node_cli_passes_the_pinned_certificate(tmp_path, monkeypatch):
    cert, _ = self_signed(tmp_path, "manager")
    built: dict = {}

    class Enricher(contextlib.AbstractContextManager):
        source = None

        def __exit__(self, *exc):
            return None

    async def served(agent):
        built["agent"] = agent

    monkeypatch.setattr(node_module, "provider_factory", lambda *a: lambda country: None)
    monkeypatch.setattr(node_module, "read_token", lambda path: "token-for-tests-only")
    monkeypatch.setattr(node_module, "serve", served)
    monkeypatch.setattr(enrichment, "LocalEnricher", lambda mmdb: Enricher())
    assert main(node_argv("https://203.0.113.9:8443", "--ca-file", str(cert))) == 0
    tls = built["agent"].tls
    assert isinstance(tls, ssl.SSLContext) and len(tls.get_ca_certs()) == 1
