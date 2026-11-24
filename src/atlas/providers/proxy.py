"""GrammarProxy: one provider's configuration, validated and resolved through its grammar.

`providers.grammar` loads and parses the grammars; this module checks a configuration
against one and renders per-request proxy credentials without I/O or shared mutable state.
`providers.grammar` imports `GrammarProxy` back, so existing imports keep working.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ..models import NetworkProtocol, ProxyConfig, RequestContext
from .grammar import ALPHABETS, HOST, Grammar, load_grammar

POLICIES = ("rotate", "gateway", "fixed", "per_request", "per_worker", "per_run")
GENERATED = ("per_request", "per_worker", "per_run")


@dataclass(frozen=True, slots=True)
class GrammarProxy:
    """Immutable provider configuration resolved through the provider's grammar.

    None parameters are omitted. A supplied session implies fixed sessions unless
    session_policy is given; generated sessions are isolated by run, worker or request.
    """

    provider: str
    account: str | None = field(default=None, repr=False)
    password: str = field(default="", repr=False)
    parameters: Mapping[str, Any] = field(default_factory=dict, repr=False)
    extra_flags: Mapping[str, Any] = field(default_factory=dict, repr=False)
    scheme: str = "http"
    gateway: str | None = None
    host: str | None = None
    port: int | None = None
    session_policy: str | None = None
    pool: str | None = None
    # Values of the grammar's preset flags, as configured on the provider's side.
    presets: Mapping[str, Any] = field(default_factory=dict)

    def __reduce__(self):
        # Spawned HTTP workers rebuild the config through validation.
        return (
            _rebuild,
            (
                self.provider,
                self.account,
                self.password,
                dict(self.parameters),
                dict(self.extra_flags),
                self.scheme,
                self.gateway,
                self.host,
                self.port,
                self.session_policy,
                self.pool,
                dict(self.presets),
            ),
        )

    @property
    def id(self) -> str:
        return self.provider

    @property
    def grammar(self) -> Grammar:
        return load_grammar(self.provider)

    @property
    def test_protocol(self) -> NetworkProtocol:
        return load_grammar(self.provider).test_protocol

    @property
    def echo_urls(self) -> Mapping[str, str]:
        """The network's own IP-check URLs by echo target id ([testing] echo_http, echo_https)."""
        return load_grammar(self.provider).echo_urls

    @property
    def forbidden_host(self) -> tuple[str, str] | None:
        """The gateway's "destination forbidden for our account" answer ([testing]
        forbidden_host: header name and text), or None."""
        return load_grammar(self.provider).forbidden_host

    def __post_init__(self) -> None:
        g = load_grammar(self.provider)
        name = g.id.capitalize()
        self._account(g, name)
        if not isinstance(self.password, str) or not self.password:
            raise ValueError(f"{name} password is required")
        if self.scheme not in g.ports:
            raise ValueError(f"{name} transport must be one of: {', '.join(g.ports)}")
        self._gateway(g, name)
        params = self._normalized(g, name)
        self._check_params(g, params, name)
        policy = self._policy(g, params, name)
        self._ports_and_pool(g, params, policy, name)
        extras = self._extras(g, params, name)
        object.__setattr__(self, "parameters", MappingProxyType(params))
        object.__setattr__(self, "extra_flags", MappingProxyType(extras))
        object.__setattr__(self, "session_policy", policy)
        # Fail before sending traffic, e.g. for SOCKS's 255-byte authentication limit.
        self.resolve(RequestContext("validation", 0, 0))

    def _account(self, g: Grammar, name: str) -> None:
        if g.account_required:
            if not isinstance(self.account, str):
                raise ValueError(f"{name} account is required")
            account = self.account
            for prefix in g.strip_prefixes:
                account = account.removeprefix(prefix)
            if not re.fullmatch(g.account_pattern, account) or (
                g.target == "username" and set(account.lower().split(g.separator)) & g.reserved
            ):
                raise ValueError(f"Invalid {name} account syntax")
            object.__setattr__(self, "account", account)
        elif self.account is not None:
            raise ValueError(f"{name} authenticates without an account")

    def _normalized(self, g: Grammar, name: str) -> dict[str, Any]:
        """The supplied parameters, normalized, with presets applied and empty booleans dropped."""
        if not isinstance(self.parameters, Mapping) or not isinstance(self.extra_flags, Mapping):
            raise ValueError(f"{name} parameters and extra_flags must be mappings")
        if (
            set(self.parameters)
            - set(g.flags)
            - ({"session"} if g.session_mode == "port" else set())
        ):
            raise ValueError(f"Unknown {name} parameter")
        if g.slot_flag in self.parameters:
            raise ValueError(f"{name} {g.slot_flag} is set from the session slot")
        params = {}
        for key, value in self.parameters.items():
            if value is None:
                continue
            if key == "session" and g.session_mode == "port":  # A zero-based slot index.
                low, high = g.session_ports
                if type(value) is not int or not 0 <= value <= high - low:
                    raise ValueError(f"{name} session slot is outside the port range")
                params[key] = value
                continue
            params[key] = g.flags[key].normalize(value, name)
        params = self._presets(g, params, name)
        for key, flag in g.flags.items():
            if flag.kind == "boolean" and key in params and flag.render(params[key]) is None:
                del params[key]
        return params

    def _check_params(self, g: Grammar, params: Mapping[str, Any], name: str) -> None:
        for key in list(params):
            if key == "session" and g.session_mode == "port":
                continue
            flag = g.flags[key]
            if key == g.session:
                continue  # Session requirements apply to generated sessions too (below).
            missing = [r for r in flag.requires if r != "session" and r not in params]
            if missing:
                raise ValueError(f"{name} {key} requires {', '.join(missing)}")
            if set(flag.conflicts) & set(params):
                raise ValueError(f"{name} cannot combine {key} with {flag.conflicts[0]}")
        for key, value in params.items():
            # A two-letter country cannot carry a separator; ISO codes such as CC, CR or ST
            # may still equal a flag key and are valid in the country slot. Checked here, not
            # assumed, so a grammar that widens a country pattern keeps the guard.
            if isinstance(value, str) and not (
                g.flags[key].kind == "country" and re.fullmatch(r"[A-Za-z]{2}", value)
            ):
                self._check_delimiters(g, value, name, key)

    def _policy(self, g: Grammar, params: Mapping[str, Any], name: str) -> str:
        session_key = g.session or "session"
        policy = self.session_policy or ("fixed" if session_key in params else g.no_session_policy)
        if policy not in POLICIES:
            raise ValueError(f"Unknown {name} session policy")
        if (policy == "fixed") != (session_key in params):
            raise ValueError("Only fixed sessions accept and require a supplied session")
        if policy in GENERATED and g.generate is None:
            raise ValueError(f"{name} sessions cannot be generated; supply a session")
        if g.session is None and g.session_mode == "flag" and policy not in ("rotate", "gateway"):
            raise ValueError(f"{name} does not support sessions")
        if policy == "gateway" and g.no_session_policy == "rotate" and not g.rotating_ports:
            raise ValueError(f"{name} rotates per request; use the rotate policy")
        if policy == "rotate" and g.no_session_policy != "rotate" and not g.rotating_ports:
            raise ValueError(f"{name} rotation is configured by the provider, not per request")
        sticky = policy not in ("rotate", "gateway")
        if sticky and g.session is not None:
            missing = [r for r in g.flags[g.session].requires if r not in params]
            if missing:
                raise ValueError(f"{name} sessions require {', '.join(missing)}")
        if any(g.flags[k].requires_session for k in params if k in g.flags) and not sticky:
            raise ValueError(f"{name} session lifetime requires a supplied or generated session")
        return policy

    def _ports_and_pool(
        self, g: Grammar, params: Mapping[str, Any], policy: str, name: str
    ) -> None:
        sticky = policy not in ("rotate", "gateway")
        rotating = g.rotating_ports.get(self.scheme)
        if rotating is not None:
            on_rotating = rotating[0] <= self.port <= rotating[1]
            if on_rotating and (
                sticky
                or any(
                    g.flags[k].requires_session or g.flags[k].sticky_ports_only
                    for k in params
                    if k in g.flags
                )
            ):
                raise ValueError(f"{name} sessions require a sticky gateway port")
            if policy == "rotate" and not on_rotating:
                raise ValueError(f"{name} rotation requires a rotating gateway port")
        if self.pool is not None:
            if self.pool not in g.pools:
                raise ValueError(f"Unknown {name} account pool")
            if set(params) & g.pools[self.pool]:
                raise ValueError(f"{name} targeting is not supported by the configured pool")

    def _gateway(self, g: Grammar, name: str) -> None:
        if self.host is not None and self.gateway is not None:
            # Rebuilt copies carry both; anything else is an ambiguous choice.
            if g.hosts.get(self.gateway) != self.host:
                raise ValueError(f"Choose either a named {name} gateway or a host")
        elif self.host is not None:
            if (
                not g.host_override
                or not isinstance(self.host, str)
                or not HOST.fullmatch(self.host)
            ):
                raise ValueError(f"Invalid {name} gateway host")
        else:
            gateway = self.gateway or g.default_host
            if gateway not in g.hosts:
                raise ValueError(f"Unknown {name} regional gateway")
            object.__setattr__(self, "host", g.hosts[gateway])
            object.__setattr__(self, "gateway", gateway)
        if self.port is None:
            object.__setattr__(self, "port", g.ports[self.scheme])
        elif not g.port_override and self.port != g.ports[self.scheme]:
            raise ValueError(f"{name} uses fixed gateway ports")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError(f"{name} port must be an integer between 1 and 65535")

    def _presets(self, g: Grammar, params: dict[str, Any], name: str) -> dict[str, Any]:
        """Fill preset flags from `presets` and refuse any other value: the credential
        cannot change them, so a request for another value would be mislabelled."""
        bound = {key for key, flag in g.flags.items() if flag.preset}
        if not isinstance(self.presets, Mapping) or set(self.presets) - bound:
            raise ValueError(f"{name} presets accept only: {', '.join(sorted(bound)) or 'none'}")
        declared = {key: g.flags[key].normalize(value, name) for key, value in self.presets.items()}
        object.__setattr__(self, "presets", MappingProxyType(declared))
        for key in bound & set(params):
            if key not in declared:
                raise ValueError(
                    f"{name} {key} is set on the provider's side; declare it in "
                    "the config's presets"
                )
            if params[key] != declared[key]:
                raise ValueError(f"{name} {key} differs from the provider-side preset")
        return declared | params

    def _check_delimiters(self, g: Grammar, value: str, name: str, key: str) -> None:
        # Values may contain the separator (e.g. hyphenated Shifter sids or Byteful's
        # new_york) but must not smuggle another flag into the credential.
        words = {re.split(r"[^a-z0-9]", token)[0] for token in value.lower().split(g.separator)}
        if words & g.reserved:
            raise ValueError(f"{name} {key} contains an ambiguous flag delimiter")

    def _extras(self, g: Grammar, params: Mapping[str, Any], name: str) -> dict[str, str]:
        if self.extra_flags and not g.extra_flags:
            raise ValueError(f"Unsupported {name} extra flags; use documented parameters")
        extras = {}
        for key, value in self.extra_flags.items():
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]*", key)
                or key in g.flags
                or key in g.reserved
                or key in ("account", "password")
            ):
                raise ValueError(f"Invalid or reserved {name} extra flag name")
            if value is None:
                continue
            value = str(value).lower() if type(value) is bool else value
            value = str(value) if type(value) is int else value
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_]+", value):
                raise ValueError(f"Invalid {name} extra flag value")
            self._check_delimiters(g, value, name, key)
            session = str(params.get(g.session or "", ""))
            if any(
                re.search(
                    rf"(?:^|{re.escape(g.separator)}){key}(?:{re.escape(g.separator)}|$)",
                    item.lower(),
                )
                for item in (self.account or "", session)
            ):
                raise ValueError(f"{name} extra flag conflicts with an authentication token")
            extras[key] = value
        return extras

    def _generated(self, g: Grammar, context: RequestContext) -> str | int:
        scope = {
            "per_request": context.request_index,
            "per_worker": context.worker_index,
            "per_run": 0,
        }[self.session_policy]
        seed = f"{context.run_id}:{self.session_policy}:{scope}".encode()
        digest = hashlib.sha256(seed)
        if g.session_mode == "flag" and g.generate == "hex":
            return digest.hexdigest()[: g.generate_length]  # Stable with earlier releases.
        number = int.from_bytes(digest.digest(), "big")
        if g.session_mode == "port":
            low, high = g.session_ports
            if self.session_policy == "per_request":  # The next port, wrapping to the first.
                return context.request_index % (high - low + 1)
            return number % (high - low + 1)
        alphabet = ALPHABETS[g.generate]
        chars = []
        for _ in range(g.generate_length):
            number, index = divmod(number, len(alphabet))
            chars.append(alphabet[index])
        if g.generate == "digits" and chars[0] == "0":
            chars[0] = "1"  # Avoid numbers that a gateway could parse differently.
        return "".join(chars)

    def resolve(self, context: RequestContext) -> ProxyConfig:
        g = load_grammar(self.provider)
        params = dict(self.parameters)
        port = self.port
        if self.session_policy in GENERATED:
            params[g.session or "session"] = self._generated(g, context)
        if g.session_mode == "port" and "session" in params:
            slot = params.pop("session")
            port = g.session_ports[0] + slot
            if g.slot_flag and self.session_policy != "per_request":
                params[g.slot_flag] = slot  # Sticky: the slot names the session too.
        pieces = [
            rendered
            for key, flag in g.flags.items()
            if key in params and not flag.preset
            if (rendered := flag.render(params[key])) is not None
        ]
        pieces.extend(
            f"{key}{g.separator}{value}" for key, value in sorted(self.extra_flags.items())
        )
        if g.rotation_suffix and self.session_policy == "rotate":
            pieces.append(g.rotation_suffix)
        fields = {"account": self.account or "", "password": self.password}
        base = (g.flagged if pieces and g.flagged else getattr(g, g.target)).format(**fields)
        flagged = base + (g.first_separator if base and pieces else "") + g.separator.join(pieces)
        if not flagged:
            raise ValueError(f"{g.id.capitalize()} requires at least one targeting flag")
        other = "password" if g.target == "username" else "username"
        values = {g.target: flagged, other: getattr(g, other).format(**fields)}
        if self.scheme == "socks5" and any(
            not item.isascii() or len(item) > 255 for item in values.values()
        ):
            raise ValueError("SOCKS5 authentication requires at most 255 ASCII bytes per field")
        return ProxyConfig(self.scheme, self.host, port, values["username"], values["password"])

    def public(self) -> dict[str, Any]:
        g = load_grammar(self.provider)
        params = dict(self.parameters)
        session_key = g.session or "session"
        if session_key in params:
            params[session_key] = "[redacted]"
        ttl = self.parameters[g.ttl] * g.ttl_seconds if g.ttl in self.parameters else None
        if ttl is None and self.session_policy not in ("rotate", "gateway"):
            ttl = g.default_ttl_seconds
        return {
            "proxy_protocol": self.scheme,
            "provider": self.provider,
            "product": g.product,
            "requested_country": (
                self.parameters["country"].lower() if "country" in self.parameters else None
            ),
            "provider_config": {
                "adapter": self.provider,
                "version": g.version,
                "gateway": {"host": self.host, "port": self.port},
                "pool": self.pool,
                "parameters": params,
                "session_policy": self.session_policy,
                "effective_ttl_seconds": ttl,
                # Future flags may contain credentials. Values are private by default.
                "extra_flags": dict.fromkeys(self.extra_flags, "[redacted]"),
            },
            # Only non-default networks publish it, so default run configs are unchanged.
        } | ({"test_protocol": g.test_protocol} if g.test_protocol != "default" else {})


def _rebuild(*args: Any) -> GrammarProxy:
    return GrammarProxy(*args)
