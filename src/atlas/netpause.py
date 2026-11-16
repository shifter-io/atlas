"""PROXY-AUTH-PAUSE-1: a network's tests paused and resumed in a manager's files.

The same three moves as the operator's pause by hand (docs/campaign.html, "Re-enabling
proxy-cheap"), each one skipped when it is already done, so a call is safe to repeat:

- pulses: its entry of `pulse/fleet.json` moves to `pulse/paused-networks.json` (merged with
  the networks already there), so the HH:57 pulse hour plans none of its droplets;
- daily: `daily/formulas/<network>.json` moves to `daily/formulas-paused/`, so the HH:56 daily
  hour plans none of its countries;
- browser: its host entry in the browser settings ("<network>/<product>") is renamed
  "<network>/paused": the host keeps its slots, so every host's start times stay the same, and
  the planner closes the paused slots at once without a run (no node of that name). The edited
  file must pass browser.settings.load_settings before it replaces the live one.

Copies of every file changed go to `archive/<network>-<pause|resume>-<UTC time>/` under the
root first. The hours read these files at HH:56 and HH:57, so nothing moves at minutes QUIET.
Resuming (`atlas daily resume-network`) first runs a small login test through the network's
pulse provider config and credentials (login_test) and refuses while it does not pass.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import proxy_login
from .errors import UserError
from .onboarding.files import atomic_json, private_dir, read_json

QUIET = range(54, 59)  # the daily hour reads its formulas at HH:56, the pulse hour at HH:57
PAUSED = "paused"  # a paused network's browser entry: "<network>/paused"
LOGIN_TARGET = "generic_https"
LOGIN_REQUESTS = 5


class PauseError(UserError):
    """A pause or resume that cannot be done (named without values)."""


@dataclass(frozen=True)
class Places:
    """The manager root (its pulse and daily folders) and the browser settings file, if any."""

    root: Path
    browser: Path | None = None

    @property
    def fleet(self) -> Path:
        return self.root / "pulse" / "fleet.json"

    @property
    def held(self) -> Path:
        return self.root / "pulse" / "paused-networks.json"

    def formula(self, network: str, paused: bool = False) -> Path:
        folder = "formulas-paused" if paused else "formulas"
        return self.root / "daily" / folder / f"{network}.json"


def quiet(now: datetime) -> bool:
    """Whether `now` is in the minutes the hours read the files (nothing moves then)."""
    return now.minute in QUIET


def pause(places: Places, network: str, browser: str, now: datetime) -> tuple[list, list]:
    """Pause the network's pulses, daily tests and browser entry `browser`; returns the parts
    now paused (moved now or before) and the problems (a browser edit the settings refuse)."""
    names = (browser, f"{network}/{PAUSED}")
    return _move(places, network, names, _copies(places, network, now))[:2]


def resume(places: Places, network: str, browser: str, now: datetime) -> tuple[list, list, bool]:
    """Put back what pause() moved; returns the parts back in place, the problems and whether
    anything moved now. A daily formula already back in place (a newer one) is kept, the paused
    copy left where it is."""
    names = (f"{network}/{PAUSED}", browser)
    return _move(places, network, names, _copies(places, network, now, "resume"), back=True)


def _copies(places: Places, network: str, now: datetime, action: str = "pause") -> Path:
    return places.root / "archive" / f"{network}-{action}-{now:%Y%m%dT%H%M%S}"


def _move(places: Places, network: str, names: tuple, copies: Path, back: bool = False):
    done, problems = [], []
    if _fleet(places, network, copies, back):
        done.append("pulses")
    if _formula(places, network, copies, back):
        done.append("daily")
    try:
        if _browser(places.browser, *names, copies):
            done.append("browser")
    except UserError as error:
        problems.append(f"browser settings refused the edit ({error})")
    return done, problems, copies.exists()  # every move keeps its copies there first


def _keep(copies: Path, *paths: Path) -> None:
    private_dir(copies)
    for path in paths:
        if path.exists():
            shutil.copy2(path, copies / f"{path.parent.name}-{path.name}")


def _fleet(places: Places, network: str, copies: Path, back: bool) -> bool:
    """Move the fleet entry; True when it is (now) on the asked side."""
    if not places.fleet.exists():
        return False
    fleet = read_json(places.fleet)
    held = read_json(places.held) if places.held.exists() else {}
    source, target = (held, fleet["networks"]) if back else (fleet["networks"], held)
    if network in source and network not in target:
        _keep(copies, places.fleet, places.held)
        target[network] = source.pop(network)
        atomic_json(places.held, held)
        atomic_json(places.fleet, fleet)
    return network in target


def _formula(places: Places, network: str, copies: Path, back: bool) -> bool:
    live, held = places.formula(network), places.formula(network, paused=True)
    source, target = (held, live) if back else (live, held)
    if source.exists() and not (back and live.exists()):
        _keep(copies, live, held)
        target.parent.mkdir(mode=0o700, exist_ok=True)
        os.replace(source, target)
    return live.exists() if back else held.exists() and not live.exists()


def _browser(path: Path | None, old: str, new: str, copies: Path) -> bool:
    """Rename the browser entry `old` to `new` everywhere in the settings file, checked first."""
    if path is None or not path.exists():
        return False
    from .browser.settings import load_settings

    text = path.read_text()
    if f'"{old}"' in text:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
        temporary.write_text(text.replace(f'"{old}"', f'"{new}"'))
        os.chmod(temporary, 0o600)
        try:
            load_settings(temporary)
        except UserError:
            temporary.unlink()
            raise
        _keep(copies, path)
        os.replace(temporary, path)
        text = path.read_text()
    return f'"{new}"' in text


def entry(places: Places, network: str) -> dict:
    """The network's pulse fleet entry, paused or not: its product, provider config, credentials."""
    for path, key in ((places.held, None), (places.fleet, "networks")):
        found = read_json(path) if path.exists() else {}
        found = found.get(key, {}) if key else found
        if network in found:
            return found[network]
    raise PauseError(f"{network}: no pulse fleet entry, so no login to test")


def login_test(places: Places, network: str) -> dict:
    """LOGIN_REQUESTS requests through the network's pulse config and credentials; the summary."""
    from .models import RunConfig
    from .providers import load_provider
    from .runner import run
    from .sinks import JsonSink

    found, pulse = entry(places, network), places.root / "pulse"
    proxy = load_provider(
        pulse / found["provider_config"], credentials_file=pulse / found["credentials"]
    )
    config = RunConfig(LOGIN_TARGET, proxy, LOGIN_REQUESTS, LOGIN_REQUESTS, processes=1)
    with tempfile.TemporaryDirectory() as folder:
        return asyncio.run(run(config, JsonSink(folder))).to_dict()


def passed(summary: dict) -> bool:
    """A login test passes with at least one answer and no request refused at login."""
    errors = summary.get("errors") or {}
    return bool(summary.get("successes")) and not errors.get(proxy_login.ERROR)
