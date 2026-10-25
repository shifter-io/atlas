"""Provider extension point and portable, secret-free TOML configuration loading."""
from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import Any

from ..models import ProxySource
from .credentials import read_credentials
from .grammar import GrammarProxy, grammar_ids, load_grammar


class ProviderRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., ProxySource]] = {}

    def register(self, provider_id: str, factory: Callable[..., ProxySource]) -> None:
        if provider_id in self._factories:
            raise ValueError("Provider ID is already registered")
        self._factories[provider_id] = factory

    def create(self, provider_id: str, **options: Any) -> ProxySource:
        if provider_id not in self._factories:
            raise ValueError("Unknown provider adapter")
        return self._factories[provider_id](**options)

    def all(self) -> tuple[str, ...]:
        return tuple(self._factories)


class NoCountryList(ValueError):
    """The credentials hold per-country list logins but none for the requested country; the
    message is fixed text, safe to print."""


# A provider whose login fixes the country (a preset flag) may hold one login per country.
_COUNTRY_KEY = re.compile(r"(account|password)_([a-z]{2})")


def _country_logins(private: dict[str, str]) -> tuple[dict[str, str], dict[str, dict]]:
    """Split `account_<cc>`/`password_<cc>` pairs (cc lower-case ISO) from the credentials."""
    rest, logins = {}, {}
    for key, value in private.items():
        match = _COUNTRY_KEY.fullmatch(key)
        if match:
            logins.setdefault(match[2], {})[match[1]] = value
        else:
            rest[key] = value
    if any(len(pair) != 2 for pair in logins.values()):
        raise ValueError("Per-country credentials need both account_<cc> and password_<cc>")
    return rest, logins


registry = ProviderRegistry()
for _provider in grammar_ids():
    registry.register(_provider, partial(GrammarProxy, _provider))


def load_provider(
    path: str | Path, *, parameters: Mapping[str, Any] | None = None,
    session_policy: str | None = None, providers: ProviderRegistry = registry,
    environ: Mapping[str, str] | None = None,
    credentials_file: str | Path | None = None,
) -> ProxySource:
    """Load public config with private credentials from a file or the environment.

    Per-test parameters override the file. None explicitly removes a file value.
    Adapter settings are provider-owned so future providers need no target edits.
    With per-country logins in the credentials file and a preset country, a requested country
    takes its own login and preset; one without a login raises `NoCountryList`.
    """
    with Path(path).open("rb") as stream:
        data = tomllib.load(stream)
    allowed = {"schema_version", "provider", "credentials", "settings", "parameters", "extra_flags"}
    if set(data) - allowed or type(data.get("schema_version")) is not int or (
        data["schema_version"] != 1
    ):
        raise ValueError("Unsupported provider configuration schema")
    credentials = data.get("credentials", {})
    settings = data.get("settings", {})
    params = data.get("parameters", {})
    extras = data.get("extra_flags", {})
    if not all(isinstance(section, dict) for section in (credentials, settings, params, extras)):
        raise ValueError("Provider configuration sections must be tables")
    if any(key in settings for key in ("account", "password", "parameters", "extra_flags")):
        raise ValueError("Use the dedicated credentials and parameters configuration sections")
    env = os.environ if environ is None else environ
    private = (read_credentials(credentials_file, data.get("provider"))
               if credentials_file is not None else None)
    private, logins = _country_logins(private) if private is not None else (None, {})
    options = dict(settings)
    credential_names = set()
    for key, env_name in credentials.items():
        if not key.endswith("_env") or not isinstance(env_name, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", env_name
        ):
            raise ValueError("Credentials must reference environment variable names")
        name = key.removesuffix("_env")
        credential_names.add(name)
        if name in options or name in ("parameters", "extra_flags", "session_policy"):
            raise ValueError("Credential name conflicts with provider settings")
        value = private.get(name) if private is not None else env.get(env_name)
        if not value:
            raise ValueError("Required provider credential is missing")
        options[name] = value
    if private is not None and set(private) != credential_names:
        raise ValueError("Private credential fields must match the provider configuration")
    params = params | dict(parameters or {})
    if logins:
        preset = (settings.get("presets") or {}).get("country")
        if not isinstance(preset, str) or not {"account", "password"} <= credential_names:
            raise ValueError("Per-country credentials need a preset country and an account")
        country = params.get("country")
        country = country.lower() if isinstance(country, str) else None
        if country in logins:
            options |= logins[country]
            options["presets"] = dict(settings["presets"]) | {"country": country}
        elif country not in (None, preset.lower()):
            raise NoCountryList(f"no {data.get('provider')} list for {country.upper()}")
    if session_policy is not None:
        options["session_policy"] = session_policy
    return providers.create(
        data.get("provider"), parameters=params, extra_flags=extras, **options,
    )


__all__ = ["GrammarProxy", "NoCountryList", "ProviderRegistry", "load_grammar", "load_provider",
           "registry"]
