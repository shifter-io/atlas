"""Grammar flags and the [testing] table: the parts of a grammar that need no Grammar.

A `Flag` validates and renders one targeting or session parameter; `_range` and `_testing`
read port ranges and the [testing] table into Grammar fields. `providers.grammar` imports
these back.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ..models import NETWORK_PROTOCOLS
from ..models_validate import normalized_target_url

# [testing] keys naming the network's own IP-check URL, and the echo target each one serves.
ECHO_KEYS = {"echo_http": "generic_http", "echo_https": "generic_https"}
KINDS = ("country", "name", "integer", "boolean", "choice", "token", "session")
MAX_CONCURRENCY = 5000  # the campaign spec's own concurrency bound


class NoCountryList(ValueError):
    """The provider has no login or no exits for the requested country (a census round records
    it as no_list, without traffic); the message is fixed text, safe to print."""


def _range(value: Any, name: str) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(type(item) is int for item in value)
        or value[0] > value[1]
    ):
        raise ValueError(f"Grammar {name} must be an inclusive [low, high] integer range")
    return value[0], value[1]


@dataclass(frozen=True, slots=True)
class Flag:
    name: str
    format: str
    kind: str
    case: str | None = None
    spaces: str = "_"
    pattern: str | None = None
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    true: str | None = None
    false: str | None = None
    requires: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    requires_session: bool = False
    sticky_ports_only: bool = False
    scale: int = 1  # Rendered value multiplier, e.g. canonical minutes to native seconds.
    # Set on the provider's side (e.g. a dashboard list's geography) and never rendered: a
    # request may only repeat the value the config declares in `presets`.
    preset: bool = False
    # Country flags only: the upper-case ISO codes the provider serves, when it publishes
    # them; another country raises NoCountryList before traffic. Empty: not restricted.
    countries: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """Literal key word of the rendered flag, used to detect delimiter injection."""
        return re.split(r"[^A-Za-z0-9]", self.format.split("{}")[0] or "_")[0].lower()

    def normalize(self, value: Any, provider: str) -> Any:
        label = f"{provider} {self.name}"
        if self.kind == "boolean":
            if type(value) is not bool:
                raise ValueError(f"{label} must be a boolean")
            return value
        if self.kind == "integer":
            if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
                value = int(value)
            if type(value) is not int or not (
                (self.minimum is None or value >= self.minimum)
                and (self.maximum is None or value <= self.maximum)
            ):
                raise ValueError(f"{label} is outside its supported range")
            return value
        if not isinstance(value, str):
            raise ValueError(f"{label} must be a string")
        if self.kind in ("country", "name"):
            value = self.spaces.join(value.strip().split())
        if self.case == "lower":
            value = value.lower()
        elif self.case == "upper":
            value = value.upper()
        pattern = self.pattern or {
            "country": r"[A-Za-z]{2}",
            "name": r"[A-Za-z0-9_]+",
            "session": r"[A-Za-z0-9]+",
        }.get(self.kind)
        if self.kind == "choice":
            if value not in self.choices:
                raise ValueError(f"Unsupported {label} value")
        elif pattern is None or not re.fullmatch(pattern, value):
            raise ValueError(f"Invalid {label} syntax")
        if self.countries and value.upper() not in self.countries:
            raise NoCountryList(f"no {provider.lower()} exits in {value.upper()}")
        return value

    def render(self, value: Any) -> str | None:
        if self.kind == "boolean":
            text = self.true if value else self.false
            return text  # A boolean without a rendering is omitted.
        if self.kind == "integer":
            value *= self.scale
        return self.format.replace("{}", str(value))


def _flags(provider: str, specs: Mapping[str, Any]) -> dict[str, Flag]:
    """The grammar's [flags] table as checked Flags, keyed by name."""
    flags = {}
    for name, spec in specs.items():
        spec = dict(spec)
        kind = spec.pop("kind")
        if kind not in KINDS:
            raise ValueError(f"Unknown grammar flag kind for {provider} {name}")
        for key in ("requires", "conflicts", "choices", "countries"):
            if key in spec:
                spec[key] = tuple(spec[key])
        if "min" in spec:
            spec["minimum"] = spec.pop("min")
        if "max" in spec:
            spec["maximum"] = spec.pop("max")
        if spec.get("preset"):
            spec.setdefault("format", "")
        flags[name] = Flag(name=name, kind=kind, **spec)
    names = set(flags)
    for flag in flags.values():
        if set(flag.requires + flag.conflicts) - names:
            raise ValueError(f"Grammar {provider} {flag.name} references an unknown flag")
        if flag.kind != "session" and "session" in flag.requires + flag.conflicts:
            raise ValueError("Use requires_session for flags that need a session")
        if flag.countries and (
            flag.kind != "country"
            or not all(re.fullmatch(r"[A-Z]{2}", code) for code in flag.countries)
        ):
            raise ValueError(f"Grammar {provider} {flag.name} countries must be ISO codes")
        if flag.case not in (None, "lower", "upper") or type(flag.scale) is not int:
            raise ValueError(f"Grammar {provider} {flag.name} has an invalid rendering")
        if flag.preset and (
            flag.format or flag.kind in ("session", "boolean") or flag.requires or flag.conflicts
        ):
            raise ValueError(f"Grammar {provider} {flag.name} cannot be a preset")
    return flags


def _statuses(testing: Mapping[str, Any], key: str) -> tuple[int, ...]:
    values = testing.get(key, [])
    if (
        not isinstance(values, list)
        or len(set(values)) != len(values)
        or any(type(v) is not int or not 400 <= v <= 599 or v == 429 for v in values)
    ):
        raise ValueError(f"Grammar {key} must be distinct HTTP statuses 400-599 other than 429")
    return tuple(values)


def _echo_urls(testing: Mapping[str, Any]) -> Mapping[str, str]:
    """[testing] echo_http / echo_https by the echo target they serve (generic_http,
    generic_https): each a URL of that target's scheme, without credentials or a fragment."""
    urls = {}
    for key, target in ECHO_KEYS.items():
        if key in testing:
            try:
                urls[target] = normalized_target_url(target, testing[key])
            except ValueError:
                raise ValueError(f"Grammar {key} must be a {target} URL") from None
    return MappingProxyType(urls)


def _testing(testing: Mapping[str, Any]) -> dict[str, Any]:
    """The Grammar fields of the [testing] table: protocol, concurrency, answer classes and the
    network's own IP-check URLs."""
    protocol = testing.get("protocol", "default")
    if protocol not in NETWORK_PROTOCOLS:
        raise ValueError("Unknown grammar test protocol")
    ceiling = testing.get("max_concurrency")
    if ceiling is not None and (type(ceiling) is not int or not 1 <= ceiling <= MAX_CONCURRENCY):
        raise ValueError(f"Grammar max_concurrency must be an integer 1-{MAX_CONCURRENCY}")
    limits, empty = (
        _statuses(testing, "rate_limit_statuses"),
        _statuses(testing, "no_inventory_statuses"),
    )
    if set(limits) & set(empty):
        raise ValueError("Grammar rate-limit and no-IP statuses must be disjoint")
    connect = testing.get("rate_limit_connect", False)
    if type(connect) is not bool:
        raise ValueError("Grammar rate_limit_connect must be a boolean")
    return {
        "test_protocol": protocol,
        "max_concurrency": ceiling,
        "rate_limit_statuses": limits,
        "rate_limit_connect": connect,
        "no_inventory_statuses": empty,
        "echo_urls": _echo_urls(testing),
    }
