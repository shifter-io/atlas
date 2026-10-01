"""Targets describe and assess requests; they do not schedule or persist them."""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Protocol

from .models import ErrorCode, VerdictName
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
    pool_eligible: bool = False
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
registry.register(EchoIpTarget("generic_http", "http://checkip.amazonaws.com/", True))
registry.register(EchoIpTarget("generic_https", "https://checkip.amazonaws.com/"))
