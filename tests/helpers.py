from __future__ import annotations

import asyncio
import base64
import contextlib
import socket
import ssl
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


@asynccontextmanager
async def server(handler, *, tls: ssl.SSLContext | None = None) -> AsyncIterator[int]:
    tasks = set()

    async def wrapped(reader, writer):
        tasks.add(asyncio.current_task())
        try:
            await handler(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, ssl.SSLError):
                await writer.wait_closed()
            tasks.discard(asyncio.current_task())

    listener = await asyncio.start_server(wrapped, "127.0.0.1", 0, ssl=tls)
    try:
        yield listener.sockets[0].getsockname()[1]
    finally:
        listener.close()
        await listener.wait_closed()
        remaining = list(tasks)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


class Gateway:
    """Minimal loopback gateways: no public hosts or credentials are contacted."""

    def __init__(self, scheme="http", *, auth=False, body=b"8.8.8.8\n", status=200,
                 disconnect=False, delay=0, fragmented=False):
        self.scheme = scheme
        self.auth = auth
        self.body = body
        self.status = status
        self.disconnect = disconnect
        self.delay = delay
        self.fragmented = fragmented
        self.requests = []
        self.connections = 0
        self.active = 0
        self.peak = 0
        self.domains = []
        self.credentials = []
        self.accepted = asyncio.Event()

    async def __call__(self, reader, writer):
        self.connections += 1
        self.accepted.set()
        self.active += 1
        self.peak = max(self.active, self.peak)
        try:
            if self.disconnect:
                return
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.scheme == "socks5":
                await self.socks(reader, writer)
                return
            request = await reader.readuntil(b"\r\n\r\n")
            self.requests.append(request)
            auth_line = next((line for line in request.split(b"\r\n")
                              if line.startswith(b"Proxy-Authorization: Basic ")), None)
            credentials = (None, None)
            if auth_line:
                credentials = tuple(base64.b64decode(auth_line.split()[-1]).decode().split(":", 1))
                self.credentials.append(credentials)
            if self.auth and not self.valid_auth(*credentials):
                writer.write(b"HTTP/1.1 407 Nope\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            method, endpoint, _ = request.split(b"\r\n", 1)[0].split(b" ")
            if method == b"CONNECT":
                host, port = endpoint.decode().rsplit(":", 1)
                assert host in ("localhost", "127.0.0.1")
                remote_reader, remote_writer = await asyncio.open_connection("127.0.0.1", int(port))
                writer.write(b"HTTP/1.1 200 Established\r\n\r\n")
                await writer.drain()
                await asyncio.gather(pipe(reader, remote_writer), pipe(remote_reader, writer))
                return
            await self.respond(writer)
        finally:
            self.active -= 1

    async def respond(self, writer):
        headers = (
            f"HTTP/1.1 {self.status} Test\r\nContent-Length: {len(self.body)}\r\n"
            "Location: http://localhost/must-not-follow\r\nConnection: close\r\n\r\n"
        ).encode()
        writer.write(headers)
        if self.fragmented:
            writer.write(self.body[:7])
            await writer.drain()
            await asyncio.sleep(.01)
            writer.write(self.body[7:])
        else:
            writer.write(self.body)
        await writer.drain()

    def valid_auth(self, username, password):
        if callable(self.auth):
            return self.auth(username, password)
        return username == "test" and password == "secret"

    async def socks(self, reader, writer):
        version, nmethods = await reader.readexactly(2)
        assert version == 5
        await reader.readexactly(nmethods)
        writer.write(bytes([5, 2 if self.auth else 0]))
        await writer.drain()
        if self.auth:
            version, length = await reader.readexactly(2)
            username = await reader.readexactly(length)
            length = (await reader.readexactly(1))[0]
            password = await reader.readexactly(length)
            self.credentials.append((username.decode(), password.decode()))
            valid = self.valid_auth(username.decode(), password.decode())
            writer.write(bytes([1, 0 if valid else 1]))
            await writer.drain()
            if not valid:
                return
        version, command, _, atyp = await reader.readexactly(4)
        assert version == 5 and command == 1
        if atyp == 3:
            length = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(length)).decode()
            self.domains.append(host)
        else:
            size = 4 if atyp == 1 else 16
            host = socket.inet_ntop(socket.AF_INET if atyp == 1 else socket.AF_INET6,
                                   await reader.readexactly(size))
        port = struct.unpack("!H", await reader.readexactly(2))[0]
        assert host in ("localhost", "127.0.0.1")
        remote_reader, remote_writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
        await writer.drain()
        await asyncio.gather(pipe(reader, remote_writer), pipe(remote_reader, writer))


class FakeTransport:
    def __init__(self, config):
        self.count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, target, *, context=None):
        from proxy_index.models import Outcome
        self.count += 1
        return Outcome(10.1, ip="8.8.8.8", status=200, response_body_bytes=8)
