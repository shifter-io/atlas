"""Current Shifter Residential gateway, independent of measurement targets.

Reference: https://shifter.io/docs/products/residential-proxies/
No selector API or network lookup runs in the request path.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, ClassVar, Literal

from ..models import ProxyConfig, RequestContext

FLAGS = ("country", "region", "city", "asn", "strict", "sid", "ttl")
REGIONAL_GATEWAYS = MappingProxyType({
    "auto": "p.shifter.io", "fra": "fra.p.shifter.io", "ams": "ams.p.shifter.io",
    "lon": "lon.p.shifter.io", "nyc": "nyc.p.shifter.io", "tor": "tor.p.shifter.io",
    "sgp": "sgp.p.shifter.io", "blr": "blr.p.shifter.io", "syd": "syd.p.shifter.io",
})
SessionPolicy = Literal["rotate", "fixed", "per_request", "per_worker", "per_run"]


def _token(value: Any, name: str, *, hyphens: bool = False) -> str:
    pattern = r"[A-Za-z0-9_][A-Za-z0-9_.-]*" if hyphens else r"[A-Za-z0-9_]+"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"Invalid Shifter {name} syntax")
    # Hyphens are valid in session IDs, but cannot inject another username flag.
    if hyphens and re.search(
        r"(?:^|-)(?:customer|" + "|".join(FLAGS) + r")(?:-|$)", value.lower(),
    ):
        raise ValueError(f"Shifter {name} contains an ambiguous flag delimiter")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    if type(value) is not int or value < 1:
        raise ValueError(f"Shifter {name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class ShifterProxy:
    """Reusable immutable config. Account is the base ID, without targeting flags.

    None parameters are omitted. An explicit sid implies fixed sessions unless
    session_policy is supplied; otherwise rotation is the default. Generated
    session IDs are isolated by run, even when this object is reused concurrently.
    """

    account: str = field(repr=False)
    password: str = field(repr=False)
    parameters: Mapping[str, Any] = field(default_factory=dict, repr=False)
    extra_flags: Mapping[str, Any] = field(default_factory=dict, repr=False)
    scheme: Literal["http", "socks5"] = "http"
    gateway: str = "auto"
    session_policy: SessionPolicy | None = None
    pool: Literal["full_geo", "country_geo", "non_geo"] | None = None
    id: ClassVar[str] = "shifter"
    version: ClassVar[str] = "1"

    def __post_init__(self) -> None:
        if not isinstance(self.account, str):
            raise ValueError("Shifter account is required")
        account = self.account.removeprefix("customer-")
        object.__setattr__(self, "account", _token(account, "account", hyphens=True))
        if not isinstance(self.password, str) or not self.password:
            raise ValueError("Shifter password is required")
        if self.scheme not in ("http", "socks5"):
            raise ValueError("Shifter transport must be http or socks5")
        if self.gateway not in REGIONAL_GATEWAYS:
            raise ValueError("Unknown Shifter regional gateway")
        if self.pool not in (None, "full_geo", "country_geo", "non_geo"):
            raise ValueError("Unknown Shifter account pool")
        if not isinstance(self.parameters, Mapping) or not isinstance(self.extra_flags, Mapping):
            raise ValueError("Shifter parameters and extra_flags must be mappings")
        if set(self.parameters) - set(FLAGS):
            raise ValueError("Unknown Shifter parameter; use extra_flags for future flags")
        params = {key: value for key, value in self.parameters.items() if value is not None}
        for key in ("country", "region", "city"):
            if key in params:
                if not isinstance(params[key], str):
                    raise ValueError("Shifter geographic parameters must be strings")
                value = "_".join(params[key].strip().lower().split())
                params[key] = _token(value, key)
        if "country" in params and not re.fullmatch(r"[a-z]{2}", params["country"]):
            raise ValueError("Shifter country must be a two-letter code")
        for key in ("asn", "ttl"):
            if key in params:
                params[key] = _integer(params[key], key)
        if params.get("asn", 1) > 4294967295:
            raise ValueError("Shifter ASN must fit an unsigned 32-bit number")
        if "strict" in params:
            if type(params["strict"]) is not bool:
                raise ValueError("Shifter strict must be a boolean")
            if not params["strict"]:
                del params["strict"]  # Only strict-true is documented.
        if "sid" in params:
            params["sid"] = _token(params["sid"], "sid", hyphens=True)
        policy = self.session_policy
        if policy is None:
            policy = "fixed" if "sid" in params else "rotate"
        if policy not in ("rotate", "fixed", "per_request", "per_worker", "per_run"):
            raise ValueError("Unknown Shifter session policy")
        if (policy == "fixed") != ("sid" in params):
            raise ValueError("Only fixed sessions accept and require a supplied sid")
        if "ttl" in params and policy == "rotate":
            raise ValueError("Shifter ttl requires a supplied or generated session")
        geo = set(params) & {"country", "region", "city", "asn"}
        if (self.pool == "non_geo" and geo) or (
            self.pool == "country_geo" and geo - {"country"}
        ):
            raise ValueError("Shifter targeting is not supported by the configured account pool")
        extras = {}
        for key, value in self.extra_flags.items():
            if (not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", key)
                    or key in (*FLAGS, "customer", "account", "password")):
                raise ValueError("Invalid or reserved Shifter extra flag name")
            if value is None:
                continue
            if type(value) is bool:
                value = str(value).lower()
            elif type(value) is int:
                value = str(value)
            extras[key] = _token(value, "extra flag value")
            if any(re.search(rf"(?:^|-){key}(?:-|$)", item.lower())
                   for item in (account, params.get("sid", ""))):
                raise ValueError("Shifter extra flag conflicts with an authentication token")
        object.__setattr__(self, "parameters", MappingProxyType(params))
        object.__setattr__(self, "extra_flags", MappingProxyType(extras))
        object.__setattr__(self, "session_policy", policy)
        # Fail before sending traffic for SOCKS's 255-byte authentication limit.
        self.resolve(RequestContext("validation", 0, 0))

    @property
    def host(self) -> str:
        return REGIONAL_GATEWAYS[self.gateway]

    def resolve(self, context: RequestContext) -> ProxyConfig:
        params = dict(self.parameters)
        if self.session_policy in ("per_request", "per_worker", "per_run"):
            scope = {"per_request": context.request_index, "per_worker": context.worker_index,
                     "per_run": 0}[self.session_policy]
            seed = f"{context.run_id}:{self.session_policy}:{scope}".encode()
            params["sid"] = hashlib.sha256(seed).hexdigest()[:32]
        pieces = [f"customer-{self.account}"]
        for key in FLAGS:
            if key in params:
                value = "true" if params[key] is True else str(params[key])
                pieces.append(f"{key}-{value}")
        pieces.extend(f"{key}-{value}" for key, value in sorted(self.extra_flags.items()))
        username = "-".join(pieces)
        if self.scheme == "socks5" and any(
            not item.isascii() or len(item) > 255 for item in (username, self.password)
        ):
            raise ValueError("SOCKS5 authentication requires at most 255 ASCII bytes per field")
        return ProxyConfig(self.scheme, self.host, 443, username, self.password)

    def public(self) -> dict[str, Any]:
        params = dict(self.parameters)
        if "sid" in params:
            params["sid"] = "[redacted]"
        return {
            "proxy_protocol": self.scheme, "provider": self.id, "product": "residential",
            "requested_country": self.parameters.get("country"),
            "provider_config": {
                "adapter": self.id, "version": self.version,
                "gateway": {"host": self.host, "port": 443}, "pool": self.pool,
                "parameters": params, "session_policy": self.session_policy,
                "effective_ttl_seconds": (
                    self.parameters.get("ttl", 120) if self.session_policy != "rotate" else None
                ),
                # Future flags may contain credentials. Values are private by default.
                "extra_flags": dict.fromkeys(self.extra_flags, "[redacted]"),
            },
        }
