"""The browser engines a host runs (K-BP3 `engine`) and the manifest's engine block (K-BP4 v2).

A run uses its target's engine: the [[target]]'s `engine`, else `[browser] engine`, else
Camoufox. The block names the engine and the version and user agent its browsers reported;
the user agent's major (Firefox/ for Camoufox, Chrome/ for Chrome) is the version's. No I/O.
"""

from __future__ import annotations

from typing import Any

from . import chrome
from .field_checks import check_fields, check_string

CAMOUFOX = "camoufox"
CHROME = "chrome"
ENGINES = (CAMOUFOX, CHROME)
DEFAULT_ENGINE = CAMOUFOX
ENGINE_FIELDS = ("name", "version", "user_agent")
VERSION = r"\d{1,4}(\.[0-9A-Za-z]+){0,4}"


def claimed_major(name: str, user_agent: Any) -> int | None:
    """The major a user agent claims for this engine, or None."""
    if name == CHROME:
        return chrome.user_agent_major(user_agent)
    from .contracts import user_agent_major  # contracts imports this module

    return user_agent_major(user_agent)


def check_engine(value: Any, camoufox: dict[str, str] | None) -> dict[str, str]:
    """A validated engine block; a Camoufox one with its camoufox block and agreeing with it,
    a Chrome one without."""
    block = check_fields(value, ENGINE_FIELDS, "engine")
    if block["name"] not in ENGINES:
        raise ValueError(f"engine.name: must be one of {list(ENGINES)}")
    version = check_string(block["version"], "engine.version", VERSION, "like 151.0.7")
    check_string(block["user_agent"], "engine.user_agent")
    if claimed_major(block["name"], block["user_agent"]) != int(version.split(".")[0]):
        raise ValueError("engine.user_agent: must claim the engine's major version")
    if (block["name"] == CAMOUFOX) != (camoufox is not None):
        raise ValueError("camoufox: a Camoufox bundle has the block, a Chrome one has none")
    if camoufox is not None and from_camoufox(camoufox) != block:
        raise ValueError("engine: disagrees with the camoufox block")
    return block


def from_camoufox(camoufox: dict[str, str]) -> dict[str, str]:
    """The engine block of a Camoufox identity (release, engine_version, user_agent)."""
    return {
        "name": CAMOUFOX,
        "version": camoufox["engine_version"],
        "user_agent": camoufox["user_agent"],
    }


def from_chrome(identity: dict[str, str]) -> dict[str, str]:
    """The engine block of a Chrome identity (driver.ChromeBrowser.identity)."""
    return {"name": CHROME, "version": identity["version"], "user_agent": identity["user_agent"]}
