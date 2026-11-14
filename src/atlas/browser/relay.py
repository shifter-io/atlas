"""A browser try's local relay to its proxy gateway (BYTES-TRUTH-1; docs/browser.html, Runner).

The try's context uses the relay, on 127.0.0.1 and an ephemeral port, as its proxy server. The
relay forwards every connection to the real gateway byte for byte and counts both ways: `up`
what it wrote to the gateway, `down` what the gateway's socket received (`_Gateway`). It parses
nothing, so the CONNECT exchange or SOCKS5 negotiation, TLS, headers, uploads, failed
sub-requests and aborted loads all count, and credentials pass through unchanged and are never
logged. The count is exact: it is what crossed the metered link, read after the context closed
(`Relay.count`). A connection cut before the browser had it all (the context closed, the try's
deadline, a reset) still counts every byte the gateway delivered: those asyncio had read but the
relay had not forwarded, and those still unread in the socket (FIONREAD) as it closes. When the
gateway refuses the connection or cannot be reached, nothing crossed: an HTTP gateway's browser
gets the 502 a proxy answers then, so the try stays a proxy error (proxy_connect) as without the
relay; a SOCKS5 one sees its connection close.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import fcntl
import socket
import termios
from collections.abc import AsyncIterator
from dataclasses import replace

from ..bytes_io import ByteCount
from ..models import ProxyConfig

CHUNK = 65_536
LOOPBACK = "127.0.0.1"
BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


def _unread(sock: socket.socket) -> int:
    """The bytes in `sock`'s receive buffer that nothing has read; 0 once it is closed."""
    pending = array.array("i", [0])
    try:
        fcntl.ioctl(sock.fileno(), termios.FIONREAD, pending)
    except (OSError, ValueError):
        return 0
    return pending[0]


class _Gateway(asyncio.StreamReaderProtocol):
    """A gateway connection's protocol: `down` counts a byte as the socket hands it to asyncio,
    before the pipe reads it, and when the connection is lost (closed, aborted or reset), what is
    still unread in the socket, which asyncio closes right after."""

    def __init__(self, relay: Relay) -> None:
        self.reader = asyncio.StreamReader()
        super().__init__(self.reader)
        self._relay = relay
        self._socket: socket.socket | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._socket = transport.get_extra_info("socket")
        super().connection_made(transport)

    def data_received(self, data: bytes) -> None:
        self._relay.down += len(data)
        super().data_received(data)

    def connection_lost(self, exc: Exception | None) -> None:
        if self._socket is not None:
            self._relay.down += _unread(self._socket)
        super().connection_lost(exc)


class Relay:
    """Forwards each connection the browser opens to `gateway` and counts its bytes."""

    def __init__(self, gateway: ProxyConfig) -> None:
        self.gateway = gateway
        self.up = self.down = 0
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def count(self) -> ByteCount:
        return ByteCount(self.up, self.down, exact=True)

    async def handle(self, client: asyncio.StreamReader, browser: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(browser)
        gateway = None
        try:
            try:
                upstream, gateway = await self._connect()
            except OSError:
                await self._refuse(client, browser)
                return
            self._writers.add(gateway)
            pair = (browser, gateway)
            await asyncio.gather(
                self._pipe(client, gateway, pair, up=True),
                self._pipe(upstream, browser, pair, up=False),
            )
        finally:
            for writer in (browser, gateway):
                if writer is not None:
                    writer.close()
            self._tasks.discard(task)  # type: ignore[arg-type]

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """`asyncio.open_connection` to the gateway, with the counting protocol."""
        loop = asyncio.get_running_loop()
        protocol = _Gateway(self)
        transport, _ = await loop.create_connection(
            lambda: protocol, self.gateway.host, self.gateway.port
        )
        return protocol.reader, asyncio.StreamWriter(transport, protocol, protocol.reader, loop)

    async def _refuse(self, client: asyncio.StreamReader, browser: asyncio.StreamWriter) -> None:
        """No connection to the gateway: an HTTP proxy's 502 once the request head is read (so
        closing does not reset it unread), uncounted; a SOCKS5 browser sees the close."""
        if self.gateway.scheme != "http":
            return
        with contextlib.suppress(OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            await client.readuntil(b"\r\n\r\n")
            browser.write(BAD_GATEWAY)
            await browser.drain()

    async def _pipe(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        pair: tuple[asyncio.StreamWriter, asyncio.StreamWriter],
        *,
        up: bool,
    ) -> None:
        """One direction until end of stream, then a half close; a reset closes both ways."""
        try:
            while data := await reader.read(CHUNK):
                if writer.is_closing():
                    break
                writer.write(data)
                if up:
                    self.up += len(data)
                await writer.drain()
            else:
                if writer.can_write_eof() and not writer.is_closing():
                    writer.write_eof()
                return
        except OSError:
            pass
        for each in pair:
            each.close()

    async def close(self) -> None:
        """Ends every connection still open: the context has closed, or the try was cut."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for writer in self._writers:
            # Pending writes to a peer that stopped reading are dropped, not waited for.
            writer.transport.abort()


@contextlib.asynccontextmanager
async def relayed(gateway: ProxyConfig) -> AsyncIterator[tuple[ProxyConfig, Relay]]:
    """The proxy a context should use (the relay, with `gateway`'s scheme and credentials) and
    the relay counting it; every connection is closed however the block ends."""
    relay = Relay(gateway)
    server = await asyncio.start_server(relay.handle, LOOPBACK, 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield replace(gateway, host=LOOPBACK, port=port), relay
    finally:
        server.close()
        await relay.close()
        await server.wait_closed()
