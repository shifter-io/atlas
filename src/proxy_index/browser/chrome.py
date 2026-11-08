"""Google Chrome's helpers that need no Playwright: detection, fingerprints, user agent (D7).

driver.launch_chrome() runs the host's own Chrome (docs/browser.html, Runner): detect() refuses
a missing or too old one, fingerprint() draws each try's screen, language, time zone and
hardware from a seed, and the user-agent helpers make a headless Chrome claim the real one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import random
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..errors import UserError

log = logging.getLogger(__name__)

DEFAULT_EXECUTABLE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
# The Playwright 1.62.0 release notes ("Browser Versions") name Google Chrome 151 as the one
# stable channel it was tested against: the oldest it supports and the newest we tested.
CHROME_MIN_MAJOR = 151
CHROME_TESTED_MAJOR = 151
VERSION_LINE = re.compile(r"Google Chrome (\d+)\.\d+\.\d+\.\d+")
BRAND = "Google Chrome"
_warned: set[str] = set()


class ChromeRefused(UserError):
    """The local Chrome is missing, not executable, unreadable or older than the minimum."""


def detect(executable: str | Path) -> str:
    """The full version of the Chrome at `executable` (`<executable> --version`); refuses
    (ChromeRefused) a missing, unreadable or too old one, warns once about a newer one."""
    path = Path(executable)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ChromeRefused(f"Chrome is missing or not executable: {path}")
    try:
        output = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, timeout=30, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ChromeRefused(f"Chrome --version failed ({type(exc).__name__})") from None
    match = VERSION_LINE.search(output)
    if match is None:
        raise ChromeRefused("Chrome --version gave no 'Google Chrome <version>' line")
    version, major = match.group().removeprefix("Google Chrome "), int(match.group(1))
    if major < CHROME_MIN_MAJOR:
        raise ChromeRefused(f"Chrome {version} is older than the minimum {CHROME_MIN_MAJOR}")
    if major > CHROME_TESTED_MAJOR and version not in _warned:
        _warned.add(version)
        log.warning("Chrome %s is newer than the tested %s", version, CHROME_TESTED_MAJOR)
    return version


# Real macOS screens (width, height, device scale factor); the viewport is the screen less
# the menu bar, the Dock and the window's own toolbar.
SCREENS = ((1440, 900, 2), (1512, 982, 2), (1680, 1050, 1), (1920, 1080, 1), (2560, 1440, 1))
CHROME_HEIGHT = 120
HARDWARE_CONCURRENCY = (4, 6, 8, 10, 12)
DEVICE_MEMORY = (8, 16)
# By country: the locale, its Accept-Language and the time zones a user there has.
COUNTRIES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "US": (
        "en-US",
        "en-US,en;q=0.9",
        ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"),
    ),
    "CA": ("en-CA", "en-CA,en;q=0.9", ("America/Toronto", "America/Vancouver")),
    "GB": ("en-GB", "en-GB,en;q=0.9", ("Europe/London",)),
    "DE": ("de-DE", "de-DE,de;q=0.9,en;q=0.8", ("Europe/Berlin",)),
    "FR": ("fr-FR", "fr-FR,fr;q=0.9,en;q=0.8", ("Europe/Paris",)),
    "NL": ("nl-NL", "nl-NL,nl;q=0.9,en;q=0.8", ("Europe/Amsterdam",)),
    "AU": ("en-AU", "en-AU,en;q=0.9", ("Australia/Sydney", "Australia/Melbourne")),
}


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """One try's fingerprint; navigator.platform, canvas, WebGL, audio and fonts stay real."""

    screen_width: int
    screen_height: int
    device_scale_factor: int
    locale: str
    accept_language: str
    timezone_id: str
    hardware_concurrency: int
    device_memory: int

    def record(self) -> dict[str, Any]:
        """The try record's `fingerprint`: the values and the SHA-256 of their sorted JSON."""
        values = asdict(self)
        text = json.dumps(values, sort_keys=True, separators=(",", ":"))
        return {"values": values, "sha256": hashlib.sha256(text.encode()).hexdigest()}

    def context_options(self) -> dict[str, Any]:
        """Playwright's context options for this fingerprint."""
        return {
            "viewport": {"width": self.screen_width, "height": self.screen_height - CHROME_HEIGHT},
            "screen": {"width": self.screen_width, "height": self.screen_height},
            "device_scale_factor": self.device_scale_factor,
            "locale": self.locale,
            "timezone_id": self.timezone_id,
            "extra_http_headers": {"Accept-Language": self.accept_language},
        }

    def init_script(self) -> str:
        """The context init script: the hardware getters on Navigator.prototype."""
        values = {"hardwareConcurrency": self.hardware_concurrency}
        values["deviceMemory"] = self.device_memory
        return _HARDWARE_SCRIPT.replace("__VALUES__", json.dumps(values))


def fingerprint(seed: str, country: str) -> Fingerprint:
    """The fingerprint drawn from `seed` (run id plus try index) for the run's `country`;
    the same seed always gives the same one. A country without a table entry is refused."""
    if country not in COUNTRIES:
        raise ChromeRefused(f"no Chrome fingerprint table for country {country!r}")
    locale, accept_language, zones = COUNTRIES[country]
    # Reproducible, not secret: the seed is the run id and try index.
    draw = random.Random(hashlib.sha256(seed.encode()).digest())
    width, height, scale = draw.choice(SCREENS)
    return Fingerprint(
        screen_width=width,
        screen_height=height,
        device_scale_factor=scale,
        locale=locale,
        accept_language=accept_language,
        timezone_id=draw.choice(zones),
        hardware_concurrency=draw.choice(HARDWARE_CONCURRENCY),
        device_memory=draw.choice(DEVICE_MEMORY),
    )


# Getters that look native: their toString is the native getter's, and so is toString's own.
_HARDWARE_SCRIPT = """(() => {
  const values = __VALUES__;
  const proto = Navigator.prototype;
  const nativeToString = Function.prototype.toString;
  const shown = new Map();
  for (const [name, value] of Object.entries(values)) {
    const real = Object.getOwnPropertyDescriptor(proto, name);
    if (!real || !real.get) continue;
    const get = Object.getOwnPropertyDescriptor({ get [name]() { return value; } }, name).get;
    shown.set(get, nativeToString.call(real.get));
    Object.defineProperty(proto, name, { ...real, get });
  }
  const toString = new Proxy(nativeToString, {
    apply: (target, self, args) =>
      shown.has(self) ? shown.get(self) : Reflect.apply(target, self, args),
  });
  shown.set(toString, nativeToString.call(nativeToString));
  Object.defineProperty(Function.prototype, "toString", { value: toString });
})();"""


def ua_platform(os_name: str) -> tuple[str, str]:
    """(user-agent platform token, Client Hints platform) of a host OS (macos or linux)."""
    if os_name == "macos":
        return "Macintosh; Intel Mac OS X 10_15_7", "macOS"
    return "X11; Linux x86_64", "Linux"


def user_agent(version: str, os_name: str) -> str:
    """Chrome's standard (reduced) user agent for this version on this OS."""
    major = version.split(".")[0]
    return (
        f"Mozilla/5.0 ({ua_platform(os_name)[0]}) AppleWebKit/537.36 (KHTML, like Gecko)"
        f" Chrome/{major}.0.0.0 Safari/537.36"
    )


def user_agent_metadata(version: str, os_name: str) -> dict[str, Any]:
    """CDP's userAgentMetadata for Emulation.setUserAgentOverride: the brands a real Chrome
    of this version sends (navigator.userAgentData and the Sec-CH-UA headers)."""
    major = version.split(".")[0]
    names = ((BRAND, major, version), ("Chromium", major, version), ("Not=A?Brand", "24", "24"))
    machine = platform.machine().lower()
    return {
        "brands": [{"brand": name, "version": short} for name, short, _ in names],
        "fullVersionList": [{"brand": name, "version": full} for name, _, full in names],
        "platform": ua_platform(os_name)[1],
        "platformVersion": platform.mac_ver()[0] if os_name == "macos" else "",
        "architecture": "arm" if machine in ("arm64", "aarch64") else "x86",
        "model": "",
        "mobile": False,
        "bitness": "64",
        "wow64": False,
    }


def user_agent_major(user_agent: Any) -> int | None:
    """The Chrome major a user agent claims (`Chrome/<major>.`; HeadlessChrome claims none)."""
    if not isinstance(user_agent, str):
        return None
    match = re.search(r"(?<![A-Za-z])Chrome/(\d{1,4})\.", user_agent)
    return int(match.group(1)) if match else None


def brands_major(brands: Any) -> int | None:
    """The major navigator.userAgentData.brands gives "Google Chrome", or None."""
    for entry in brands if isinstance(brands, list) else ():
        if isinstance(entry, dict) and entry.get("brand") == BRAND:
            version = str(entry.get("version", ""))
            return int(version) if version.isdigit() else None
    return None
