"""A local fake site for the browser runner, and a local authenticated HTTP proxy in front of it.

The site has one page per K-BP2 outcome a page target can reach (OUTCOME_PAGES). Every page
lives on the host SITE_HOST, which only the proxy resolves (to the loopback site), so a page
that loads proves the browser went through the proxy. The proxy asks for Basic credentials
(407 with Proxy-Authenticate), forwards absolute-URI requests to the site and tunnels CONNECT:
to a loopback port that answers plain text instead of TLS (the `tls` outcome), or refuses
REFUSED_HOST with a 502 (`proxy_error`). No public host is ever contacted.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from aiohttp import web
from helpers import server

from atlas.browser.judge import OK, PageAssessment, count, failed, path_has
from atlas.browser.targets import PageTarget
from atlas.targets import PageView

SITE_HOST = "site.invalid"
TLS_HOST = "tls.invalid"
REFUSED_HOST = "refused.invalid"
USERNAME, PASSWORD = "browser-user", "browser-secret"
PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
CONTENT = '<div id="content">Real content</div><img src="/pixel.png" alt="">'


def page(title: str, body: str) -> str:
    return f"<!doctype html><html><head><title>{title}</title></head><body>{body}</body></html>"


class Site:
    """The fake site; `images` counts image requests that reached it."""

    def __init__(self, slow_seconds: float = 30) -> None:
        self.images = 0
        self.slow_seconds = slow_seconds
        self.app = web.Application()
        self.app.router.add_get("/{name}", self.serve)
        self.app.router.add_get("/login/wall", self.wall)

    async def serve(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        html = {
            "ok": ("Product", CONTENT),
            "challenge": ("Just a moment...", "Checking your browser"),
            "empty": ("Product", "<p>Nothing here</p>"),
        }
        if name in html:
            return web.Response(text=page(*html[name]), content_type="text/html")
        if name == "blocked":
            return web.Response(status=403, text=page("Denied", "No"), content_type="text/html")
        if name == "login":
            raise web.HTTPFound("/login/wall")
        if name == "pixel.png":
            self.images += 1
            return web.Response(body=PIXEL, content_type="image/png")
        if name == "slow":
            await asyncio.sleep(self.slow_seconds)
        return web.Response(status=404, text="missing")

    async def wall(self, request: web.Request) -> web.Response:
        return web.Response(text=page("Sign in", "<form>Sign in</form>"), content_type="text/html")


class Proxy:
    """The authenticated proxy: `connects` lists (host, authorized) per CONNECT, `forwarded`
    counts absolute-URI requests it forwarded with valid credentials; `up` and `down` count the
    bytes it read from and wrote to its clients (BYTES-TRUTH-1)."""

    def __init__(self, site_port: int, tls_port: int) -> None:
        self.site_port, self.tls_port = site_port, tls_port
        self.connects: list[tuple[str, bool]] = []
        self.forwarded = 0
        self.challenged = 0
        self.up = self.down = 0

    def answer(self, writer: Any, data: bytes) -> None:
        writer.write(data)
        self.down += len(data)

    async def pipe(self, reader: Any, writer: Any, *, up: bool) -> None:
        """helpers.pipe, counting what came from the client (`up`) or went to it. The end of one
        direction half-closes the other, so a tunnel still reads what the client sends after
        it (a TLS alert), as a gateway does."""
        try:
            while data := await reader.read(65536):
                if up:
                    self.up += len(data)
                    writer.write(data)
                else:
                    self.answer(writer, data)
                await writer.drain()
            if writer.can_write_eof() and not writer.is_closing():
                writer.write_eof()
        except BaseException:
            writer.close()
            raise

    def authorized(self, head: bytes) -> bool:
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"proxy-authorization: basic "):
                user, _, password = base64.b64decode(line.split()[-1]).decode().partition(":")
                return (user, password) == (USERNAME, PASSWORD)
        return False

    async def __call__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Like a real gateway, a 407 keeps the connection open for the retry with credentials.
        while True:
            head = await reader.readuntil(b"\r\n\r\n")
            self.up += len(head)
            method, target, _ = head.split(b"\r\n", 1)[0].decode().split(" ")
            ok = self.authorized(head)
            if method == "CONNECT":
                self.connects.append((target.rsplit(":", 1)[0], ok))
            if ok:
                break
            self.challenged += 1
            self.answer(
                writer,
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b'Proxy-Authenticate: Basic realm="test"\r\nContent-Length: 0\r\n\r\n',
            )
            await writer.drain()
        if method == "CONNECT":
            await self.tunnel(target, reader, writer)
        else:
            await self.forward(method, target, head, reader, writer)

    async def tunnel(self, target: str, reader: Any, writer: Any) -> None:
        if target.rsplit(":", 1)[0] != TLS_HOST:
            self.answer(writer, b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return
        remote_reader, remote_writer = await asyncio.open_connection("127.0.0.1", self.tls_port)
        self.answer(writer, b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(
            self.pipe(reader, remote_writer, up=True), self.pipe(remote_reader, writer, up=False)
        )
        remote_writer.close()

    async def forward(self, method: str, target: str, head: bytes, reader: Any, writer: Any):
        path = "/" + target.split("://", 1)[-1].split("/", 1)[-1]
        lines = [
            line
            for line in head.split(b"\r\n")[1:]
            if line and not line.lower().startswith((b"proxy-", b"connection"))
        ]
        request = b"\r\n".join([f"{method} {path} HTTP/1.1".encode(), *lines, b"Connection: close"])
        self.forwarded += 1
        remote_reader, remote_writer = await asyncio.open_connection("127.0.0.1", self.site_port)
        remote_writer.write(request + b"\r\n\r\n")
        await remote_writer.drain()
        await self.pipe(remote_reader, writer, up=False)
        remote_writer.close()


async def not_tls(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Answers a TLS ClientHello with plain HTTP: the browser fails the handshake (tls)."""
    await reader.read(1024)
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nno")
    await writer.drain()


@dataclass(frozen=True, slots=True)
class SiteTarget(PageTarget):
    """A page target over the fake site: an input is a page name (OUTCOME_PAGES), the page
    passes with #content; a /login redirect is a login wall."""

    id: str = "browser_site"
    url: str = f"http://{SITE_HOST}/"

    def address(self, value: Any) -> str:
        if value in ("tls", "refused"):
            return f"https://{TLS_HOST if value == 'tls' else REFUSED_HOST}/"
        return f"http://{SITE_HOST}/{value}"

    def wanted(self, value: Any) -> tuple[str, ...]:
        return ("#content",)

    def rules(self, view: PageView) -> PageAssessment:
        if path_has(view, ("/login",)):
            return failed("login_wall", "login_redirect")
        return OK if count(view, "#content") else failed("no_content", "no_content_marker")


# Input page -> the (verdict, error) a try of it must record.
OUTCOME_PAGES = {
    "ok": ("ok", None),
    "challenge": ("challenge", "challenge"),
    "blocked": ("block", "blocked"),
    "login": ("error", "login_wall"),
    "empty": ("error", "no_content"),
    "slow": ("timeout", "timeout"),
    "refused": ("proxy_error", "proxy_connect"),
    "tls": ("error", "tls"),
}


@asynccontextmanager
async def site_and_proxy(slow_seconds: float = 30):
    """(site, proxy, site_port, proxy_port) for one test."""
    site = Site(slow_seconds)
    runner = web.AppRunner(site.app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    site_port = runner.addresses[0][1]
    try:
        async with server(not_tls) as tls_port:
            proxy = Proxy(site_port, tls_port)
            async with server(proxy) as proxy_port:
                yield site, proxy, site_port, proxy_port
    finally:
        await runner.cleanup()
