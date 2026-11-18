"""BYTES-TRUTH-1, browser: the try's relay forwards byte for byte and counts both ways, and the
BrowserTransport's Outcome takes its counts however the try ends (fake page, real sockets)."""

from __future__ import annotations

import array
import asyncio
import base64
import contextlib
import fcntl
import json
import logging
import socket
import termios
import time
from dataclasses import replace

import browser_transport_cases
import pytest
from browser_transport_cases import SETTINGS, Harness, Script
from helpers import server

from atlas.browser.driver import NavigationError
from atlas.browser.relay import BAD_GATEWAY, Relay, _unread, relayed
from atlas.bytes_io import ByteCount
from atlas.models import ProxyConfig

PASSWORD = "relay-secret-7f3a"
PAGE = b"<html><body><div id=content>Real content</div></body></html>"
ANSWER = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(PAGE), PAGE)


class Upstream:
    """The fake gateway: reads one request head and answers ANSWER, counting what it read and
    wrote. With `stall` it sends only the head and half the page, then holds the connection;
    with `reset` it then resets it."""

    def __init__(self, *, stall: bool = False, reset: bool = False) -> None:
        self.stall, self.reset = stall, reset
        self.read = self.written = 0
        self.heads: list[bytes] = []

    async def __call__(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        self.read += len(head)
        self.heads.append(head)
        answer = ANSWER[: len(ANSWER) - len(PAGE) // 2] if self.stall or self.reset else ANSWER
        writer.write(answer)
        await writer.drain()
        self.written += len(answer)
        if self.reset:
            await asyncio.sleep(0.05)
            writer.transport.abort()
        elif self.stall:
            await asyncio.sleep(60)


def gateway(port: int, scheme: str = "http") -> ProxyConfig:
    return ProxyConfig(scheme, "127.0.0.1", port, "user", PASSWORD)


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def load(proxy: ProxyConfig, url: str = "http://site.invalid/ok") -> bytes:
    """What a browser does through an HTTP proxy: an absolute-URI GET with its credentials."""
    reader, writer = await asyncio.open_connection(proxy.host, proxy.port)
    auth = base64.b64encode(f"{proxy.username}:{proxy.password}".encode())
    writer.write(
        f"GET {url} HTTP/1.1\r\nHost: site.invalid\r\n".encode()
        + b"Proxy-Authorization: Basic "
        + auth
        + b"\r\n\r\n"
    )
    await writer.drain()
    try:
        return await reader.read(-1)
    finally:
        writer.close()


async def test_the_relay_forwards_byte_for_byte_and_counts_both_ways():
    upstream = Upstream()
    async with server(upstream) as port:
        async with relayed(gateway(port)) as (through, relay):
            assert (through.scheme, through.host) == ("http", "127.0.0.1")
            assert (through.username, through.password) == ("user", PASSWORD)
            assert through.port != port
            answer = await load(through)
        assert relay.count == ByteCount(upstream.read, upstream.written, exact=True)
    assert answer == ANSWER and relay.down == len(ANSWER)
    # Credentials pass through unchanged, headers counted with them.
    auth = base64.b64encode(f"user:{PASSWORD}".encode())
    assert upstream.heads == [upstream.heads[0]] and auth in upstream.heads[0]
    assert relay.up == len(upstream.heads[0])


async def test_a_reset_gateway_keeps_what_crossed():
    upstream = Upstream(reset=True)
    async with server(upstream) as port, relayed(gateway(port)) as (through, relay):
        answer = await load(through)
    assert answer == ANSWER[: len(answer)] and 0 < len(answer) < len(ANSWER)
    assert relay.count == ByteCount(upstream.read, upstream.written, exact=True)


@pytest.mark.parametrize("scheme", ["http", "socks5"])
async def test_an_unreachable_gateway_counts_nothing(scheme):
    async with relayed(gateway(closed_port(), scheme)) as (through, relay):
        answer = await load(through)
    # An HTTP proxy's 502, as the gateway would have been a proxy error; SOCKS5 sees the close.
    assert answer == (BAD_GATEWAY if scheme == "http" else b"")
    assert relay.count == ByteCount(0, 0, exact=True)


async def test_closing_the_relay_ends_a_stalled_connection():
    upstream = Upstream(stall=True)
    partial = len(ANSWER) - len(PAGE) // 2
    async with server(upstream) as port:
        async with relayed(gateway(port)) as (through, relay):
            reader, writer = await asyncio.open_connection(through.host, through.port)
            writer.write(b"GET http://site.invalid/ HTTP/1.1\r\nHost: site.invalid\r\n\r\n")
            assert await reader.readexactly(partial) == ANSWER[:partial]
        # The relay closed the browser's side of the held connection.
        assert await asyncio.wait_for(reader.read(), 5) == b""
        writer.close()
    assert relay.count == ByteCount(upstream.read, upstream.written, exact=True)
    assert upstream.written == partial


async def sent(writer: asyncio.StreamWriter) -> None:
    """Until `writer` holds nothing unsent, in asyncio or (where the system says) the kernel."""
    queued = array.array("i", [0])
    for _ in range(100):
        with contextlib.suppress(OSError, AttributeError):
            fcntl.ioctl(writer.get_extra_info("socket").fileno(), termios.TIOCOUTQ, queued)
        if writer.transport.get_write_buffer_size() == 0 and queued[0] == 0:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.2)  # the relay's side reads what arrived


async def test_a_cut_counts_what_the_relay_received_but_had_not_forwarded(monkeypatch):
    """RELAY-TAIL-1: the browser is slow, so its pipe has not read the response's tail when the
    context closes. 150,000 bytes wait in the relay's buffer, which then stops reading, and the
    last 40,000 in its socket; all crossed the link, so all count."""
    pipe = Relay._pipe

    async def stalled(self, reader, writer, pair, lease=None, *, up):
        if up:
            return await pipe(self, reader, writer, pair, lease, up=up)
        await asyncio.Event().wait()

    monkeypatch.setattr(Relay, "_pipe", stalled)
    upstream, done = Upstream(), asyncio.Event()

    async def answer(reader, writer):
        upstream.read += len(await reader.readuntil(b"\r\n\r\n"))
        for size in (150_000, 40_000):
            writer.write(b"x" * size)
            await sent(writer)
            upstream.written += size
        done.set()
        await reader.read()  # held until the relay cuts it

    async with server(answer) as port:
        async with relayed(gateway(port)) as (through, relay):
            reader, writer = await asyncio.open_connection(through.host, through.port)
            writer.write(b"GET http://site.invalid/ HTTP/1.1\r\nHost: site.invalid\r\n\r\n")
            await asyncio.wait_for(done.wait(), 10)
        writer.close()
    assert relay.count == ByteCount(upstream.read, 190_000, exact=True)


def test_a_closed_socket_has_nothing_unread():
    probe = socket.socket()
    probe.close()
    assert _unread(probe) == 0


@pytest.fixture
def through_proxy(monkeypatch):
    """The fake page loads its URL through the proxy its context was given, like a browser."""

    async def goto(self, url, timeout_ms):
        answer = await load(self.browser.contexts[-1][0], url)
        if not answer.startswith(b"HTTP/1.1 200"):
            raise NavigationError("NS_ERROR_PROXY_BAD_GATEWAY")
        return 200

    monkeypatch.setattr(browser_transport_cases.FakePage, "goto", goto)


@pytest.mark.usefixtures("through_proxy")
async def test_a_try_records_the_relay_bytes_headers_included():
    upstream = Upstream()
    async with server(upstream) as port:
        harness = Harness(proxy=gateway(port))
        outcome = await harness.execute()
    assert outcome.verdict == "ok" and outcome.bytes_exact is True
    assert (outcome.bytes_up, outcome.bytes_down) == (upstream.read, upstream.written)
    # The context used the relay, not the gateway; the record keeps the browser's estimate.
    [(proxy, _)] = harness.browsers[0].contexts
    assert proxy.host == "127.0.0.1" and proxy.port != port and proxy.password == PASSWORD
    [(record, _)] = harness.recorder.tries
    assert record["bytes_down_est"] == 4321


@pytest.mark.usefixtures("through_proxy")
async def test_a_try_cut_at_its_deadline_keeps_the_bytes_that_crossed():
    upstream = Upstream(stall=True)
    settings = replace(SETTINGS, timeout_s=2)
    async with server(upstream) as port:
        harness = Harness(proxy=gateway(port), settings=settings, deadline=time.time() - 1)
        outcome = await harness.execute()
    assert (outcome.verdict, outcome.error) == ("timeout", "timeout")
    assert harness.recorder.tries[0][0]["reason"] == "deadline"
    assert outcome.bytes_down > 0 and outcome.bytes_exact is True
    assert (outcome.bytes_up, outcome.bytes_down) == (upstream.read, upstream.written)


@pytest.mark.usefixtures("through_proxy")
async def test_a_gateway_refusal_before_any_byte_records_zero():
    harness = Harness(proxy=gateway(closed_port()))
    outcome = await harness.execute()
    assert (outcome.verdict, outcome.error) == ("proxy_error", "proxy_connect")
    assert (outcome.bytes_up, outcome.bytes_down, outcome.bytes_exact) == (0, 0, True)


async def test_a_try_without_its_relay_falls_back_to_nothing_exact():
    """SOCKS5 with credentials is refused before a context opens: no relay, no bytes."""
    harness = Harness(Script(), proxy=gateway(1080, "socks5"))
    outcome = await harness.execute()
    assert outcome.error == "proxy_connect" and harness.browsers == []
    assert (outcome.bytes_up, outcome.bytes_down, outcome.bytes_exact) == (0, 0, False)


@pytest.mark.usefixtures("through_proxy")
async def test_no_credential_reaches_logs_records_or_errors(caplog):
    caplog.set_level(logging.DEBUG)
    outcomes = []
    for upstream in (Upstream(), Upstream(reset=True)):
        async with server(upstream) as port:
            outcomes.append(await Harness(proxy=gateway(port)).execute())
    harness = Harness(proxy=gateway(closed_port()))
    outcomes.append(await harness.execute())
    written = caplog.text + json.dumps([record for record, _ in harness.recorder.tries])
    written += "".join(f"{o.error} {o.verdict} {o!r}" for o in outcomes)
    assert PASSWORD not in written and "user:" not in written
