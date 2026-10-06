"""Geonode Residential username flags, separate from Shifter's grammar.

Reference: https://docs.geonode.com/docs/proxies/api-reference/geo-targeting/
Live gateways reject bare -strict; strict matching uses -strict-on, not -strict-true.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, ClassVar, Literal

from ..models import ProxyConfig, RequestContext

FLAGS = ("country", "state", "city", "asn", "strict", "session", "lifetime")


def _token(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError(f"Invalid Geonode {name} syntax")
    return value


def _integer(value: Any, name: str, maximum: int) -> int:
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"Geonode {name} is outside its supported range")
    return value


@dataclass(frozen=True, slots=True)
class GeonodeProxy:
    account: str = field(repr=False)
    password: str = field(repr=False)
    parameters: Mapping[str, Any] = field(default_factory=dict, repr=False)
    extra_flags: Mapping[str, Any] = field(default_factory=dict, repr=False)
    scheme: Literal["http", "socks5"] = "http"
    host: str = "proxy.geonode.io"
    port: int = 10000
    session_policy: str | None = None
    id: ClassVar[str] = "geonode"
    version: ClassVar[str] = "1"

    def __reduce__(self):
        return (type(self), (self.account, self.password, dict(self.parameters),
                            dict(self.extra_flags), self.scheme, self.host, self.port,
                            self.session_policy))

    def __post_init__(self) -> None:
        if not isinstance(self.account, str):
            raise ValueError("Geonode account is required")
        object.__setattr__(self, "account", _token(
            self.account.removeprefix("geonode_"), "account",
        ))
        if not isinstance(self.password, str) or not self.password:
            raise ValueError("Geonode password is required")
        if self.scheme not in ("http", "socks5"):
            raise ValueError("Geonode transport must be http or socks5")
        if not isinstance(self.host, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", self.host):
            raise ValueError("Invalid Geonode gateway host")
        _integer(self.port, "port", 65535)
        if type(self.port) is not int:
            raise ValueError("Geonode port must be an integer")
        if not isinstance(self.parameters, Mapping) or not isinstance(self.extra_flags, Mapping):
            raise ValueError("Geonode parameters and extra_flags must be mappings")
        if self.extra_flags:
            raise ValueError("Unsupported Geonode extra flags; use documented parameters")
        if set(self.parameters) - set(FLAGS):
            raise ValueError("Unknown Geonode parameter")
        params = {k: v for k, v in self.parameters.items() if v is not None}
        for key in ("country", "state", "city"):
            if key in params:
                if not isinstance(params[key], str):
                    raise ValueError("Geonode geographic parameters must be strings")
                params[key] = _token(params[key].strip().lower(), key)
        if "country" in params and not re.fullmatch(r"[a-z]{2}", params["country"]):
            raise ValueError("Geonode country must be a two-letter code")
        if "state" in params and "city" in params:
            raise ValueError("Geonode cannot combine state and city targeting")
        if set(params) & {"state", "city", "asn"} and "country" not in params:
            raise ValueError("Geonode state, city and ASN targeting require country")
        if "asn" in params:
            params["asn"] = _integer(params["asn"], "ASN", 4294967295)
        if "strict" in params:
            if type(params["strict"]) is not bool:
                raise ValueError("Geonode strict must be a boolean")
        if "session" in params:
            params["session"] = _token(params["session"], "session")
            if len(params["session"]) > 25:
                raise ValueError("Geonode session must contain at most 25 characters")
        policy = self.session_policy or ("fixed" if "session" in params else "gateway")
        if policy not in ("gateway", "rotate", "fixed", "per_request", "per_worker", "per_run"):
            raise ValueError("Unknown Geonode session policy")
        if (policy == "fixed") != ("session" in params):
            raise ValueError("Only fixed sessions accept and require a supplied session")
        rotating = (9000 <= self.port <= 9010 if self.scheme == "http"
                    else 11000 <= self.port <= 11010)
        if policy == "rotate" and not rotating:
            raise ValueError("Geonode rotation requires a rotating gateway port")
        if rotating and (policy not in ("gateway", "rotate") or "lifetime" in params):
            raise ValueError("Geonode sessions require a sticky gateway port")
        if "lifetime" in params:
            params["lifetime"] = _integer(params["lifetime"], "lifetime minutes", 1440)
            if params["lifetime"] < 3:
                raise ValueError("Geonode lifetime must be at least 3 minutes")
        object.__setattr__(self, "parameters", MappingProxyType(params))
        object.__setattr__(self, "extra_flags", MappingProxyType({}))
        object.__setattr__(self, "session_policy", policy)
        self.resolve(RequestContext("validation", 0, 0))

    def resolve(self, context: RequestContext) -> ProxyConfig:
        params = dict(self.parameters)
        if self.session_policy in ("per_request", "per_worker", "per_run"):
            scope = {"per_request": context.request_index, "per_worker": context.worker_index,
                     "per_run": 0}[self.session_policy]
            seed = f"{context.run_id}:{self.session_policy}:{scope}".encode()
            params["session"] = hashlib.sha256(seed).hexdigest()[:25]
        pieces = [f"geonode_{self.account}", "type-residential"]
        for key in FLAGS:
            if key in params:
                if key == "strict":
                    pieces.append("strict-on" if params[key] else "strict-off")
                else:
                    pieces.append(f"{key}-{params[key]}")
        username = "-".join(pieces)
        if self.scheme == "socks5" and any(
            not item.isascii() or len(item) > 255 for item in (username, self.password)
        ):
            raise ValueError("SOCKS5 authentication requires at most 255 ASCII bytes per field")
        return ProxyConfig(self.scheme, self.host, self.port, username, self.password)

    def public(self) -> dict[str, Any]:
        params = dict(self.parameters)
        if "session" in params:
            params["session"] = "[redacted]"
        return {
            "proxy_protocol": self.scheme, "provider": self.id, "product": "residential",
            "requested_country": self.parameters.get("country"),
            "provider_config": {
                "adapter": self.id, "version": self.version,
                "gateway": {"host": self.host, "port": self.port},
                "parameters": params, "session_policy": self.session_policy,
                "strict_flag_style": "on_off", "lifetime_minutes": params.get("lifetime"),
            },
        }
