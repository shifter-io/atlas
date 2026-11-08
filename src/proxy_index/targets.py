"""Targets describe and assess requests; they do not schedule or persist them."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from urllib.parse import urlsplit

from .models import ErrorCode, RequestContext, VerdictName
from .outcome import BODY_CODES, Verdict, failed_response, sniff_body


@dataclass(frozen=True, slots=True)
class RequestSpec:
    url: str
    method: str = "GET"
    max_body_bytes: int = 512
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None


@dataclass(frozen=True, slots=True)
class Assessment:
    ip: str | None = None
    error: ErrorCode | None = None
    # None lets the transport classify a failure from its status and body.
    verdict: VerdictName | None = None


class Target(Protocol):
    id: str
    version: str
    engine: str
    pool_eligible: bool

    def build_request(self) -> RequestSpec: ...
    def assess(self, status: int, body: bytes) -> Assessment: ...


@dataclass(frozen=True, slots=True)
class EchoIpTarget:
    id: str
    url: str
    # Every request costs money and returns the exit IP, so every echo target records it.
    pool_eligible: bool = True
    version: str = "1"
    engine: str = "http"

    def build_request(self) -> RequestSpec:
        return RequestSpec(self.url)

    def assess(self, status: int, body: bytes) -> Assessment:
        """Pass condition: a 200 whose whole body is one bare IP address."""
        if status == 407:
            return Assessment(error="proxy_auth", verdict=Verdict.PROXY_ERROR)
        if status != 200:
            return Assessment(error="http_status", verdict=failed_response(status, body))
        try:
            value = body.decode("ascii").strip()
            # A zone identifier describes a local interface, not an exit address.
            if "%" in value:
                raise ValueError
            return Assessment(ip=str(ipaddress.ip_address(value)), verdict=Verdict.OK)
        except (ValueError, UnicodeDecodeError):
            sniffed = sniff_body(body)
            if sniffed is not None:
                return Assessment(error=BODY_CODES[sniffed], verdict=sniffed)
            return Assessment(error="invalid_body", verdict=Verdict.ERROR)


@dataclass(frozen=True, slots=True)
class PageSpec:
    """A browser try's page, the selector that shows it loaded, and the settle time after it."""

    url: str
    wait_for: str | None = None
    settle_ms: int = 0


@dataclass(frozen=True, slots=True)
class PageView:
    """What a try saw after interact(): main-document status (None if none), title, the first
    4 KB of visible text and the match count of each named selector. Judges read only this."""

    final_url: str
    status: int | None
    title: str
    text_head: str
    found: dict[str, int] = field(default_factory=dict)
    timings_ms: dict[str, int | None] = field(default_factory=dict)


class PageDriver(Protocol):
    """All a browser target may do to a page (over the real browser or a fake site; ms)."""

    async def goto(self, url: str, timeout_ms: int) -> int | None: ...
    async def fill(self, selector: str, value: str) -> None: ...
    async def press(self, selector: str, key: str) -> None: ...
    async def click(self, selector: str) -> None: ...
    async def wait_for(self, selector: str, timeout_ms: int) -> bool: ...
    async def count(self, selector: str) -> int: ...
    async def text(self, selector: str) -> str: ...
    async def url(self) -> str: ...
    async def title(self) -> str: ...
    async def screenshot(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class BrowserTarget(Target):
    """A target measured with a real browser (K-BP1): page() from the settings inputs, an
    optional interact() through the driver, then judge(). build_request() only names the base
    URL; the HTTP runner never runs one. It records no exit IP, so it feeds no pool metric."""

    id: str
    url: str
    version: str = "1"
    engine: str = "browser"
    pool_eligible: bool = False

    def build_request(self) -> RequestSpec:
        return RequestSpec(self.url)

    def assess(self, status: int, body: bytes) -> Assessment:
        raise TypeError("A browser target is judged from a PageView, not an HTTP body")

    def page(self, context: RequestContext, inputs: tuple[Any, ...]) -> PageSpec:
        """The page for try context.request_index, from the target's settings inputs."""
        raise NotImplementedError

    async def interact(self, driver: PageDriver, spec: PageSpec) -> Mapping[str, int] | None:
        """Act on the loaded page before it is read; most targets only read it. Named numbers
        it returns (an AI source's answer length) are added to PageView.found."""
        return None

    def judge(self, view: PageView) -> Assessment:
        """The target's pass condition, after navigation errors and the shared markers."""
        raise NotImplementedError


def https_only(target: Target) -> Target:
    """The target as an 'https-only' network runs it: a plain-HTTP echo URL moves to HTTPS
    (same host and path; an explicit port 80 is dropped). Any other plain-HTTP target is
    refused, so such a network never receives a plain-HTTP request."""
    url = urlsplit(target.build_request().url)
    if url.scheme != "http":
        return target
    if not isinstance(target, EchoIpTarget):
        raise ValueError("This network accepts HTTPS tests only")
    netloc = url.netloc.removesuffix(":80")
    return replace(target, url=url._replace(scheme="https", netloc=netloc).geturl())


class TargetRegistry:
    def __init__(self) -> None:
        self._targets: dict[str, Target] = {}

    def register(self, target: Target) -> None:
        if target.id in self._targets:
            raise ValueError("Target ID is already registered; use a new registry to replace it")
        self._targets[target.id] = target

    def get(self, target_id: str) -> Target:
        try:
            return self._targets[target_id]
        except KeyError:
            raise ValueError("Unknown target ID") from None

    def all(self) -> tuple[Target, ...]:
        return tuple(self._targets.values())


registry = TargetRegistry()
registry.register(EchoIpTarget("generic_http", "http://ip-info.com/myip"))
registry.register(EchoIpTarget("generic_https", "https://ip-info.com/myip"))

# The browser page targets (K-BP1, browser.targets.pages), imported after the protocol above.
# They import this module back: that works because everything they use is defined above and
# proxy_index/__init__ (through runner) always loads this module before browser.targets.
from .browser.targets import pages  # noqa: E402

registry.register(pages.AmazonPdp())
registry.register(pages.GoogleSearch())
registry.register(pages.LinkedinPublic())
registry.register(pages.InstagramWeb())
registry.register(pages.CloudflareChallenge())

# The AI answer sources (K-BP1, browser.targets.ai_types), imported the same way.
from .browser.targets import ai_types  # noqa: E402

registry.register(ai_types.AiOverview())
registry.register(ai_types.ChatGpt())
registry.register(ai_types.Gemini())
