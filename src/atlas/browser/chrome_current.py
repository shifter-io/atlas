"""Whether the host's Chrome is the current Stable major (CHROME-CURRENT-1; docs/browser.html,
Chrome engine).

A site may answer nearly every try from a Chrome older than the current Stable major with its
bot check: such a run measures our browser, not the network. A target with CURRENT_CHROME set
(amazon_pdp) therefore runs on Chrome only while the host's Chrome major is at least the current
Stable one. Otherwise the host refuses the run at its start (Outdated): it is reported failed
with the error `chrome_outdated: ...`, so nothing judges or counts it (not measured), and the
manager raises the alert browser_chrome_outdated (orchestrator/browser_hosts.py).

The current Stable major is the highest major of the Stable releases Google serves now to at
least MAJORITY of its users (its public version history, RELEASES), so the small early rollout
of the next major does not count. Watch keeps both majors, refreshed every CHECK_S by loop():
the host's from `--version` (chrome.detect), so a Chrome updated on disk counts without a
restart, and Google's, the last one read kept when a fetch fails. While either is unknown,
nothing is refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..node import RunRefused
from . import chrome, engines
from .contracts import CHROME_OUTDATED

log = logging.getLogger(__name__)

RELEASES = (
    "https://versionhistory.googleapis.com/v1/chrome/platforms/{platform}/channels/stable"
    "/versions/all/releases?filter=endtime=none"
)
PLATFORMS = {"macos": "mac", "linux": "linux"}
MAJORITY = 0.5  # the share of users a release must be served to
CHECK_S = 600.0
FETCH_TIMEOUT_S = 20.0


class Outdated(RunRefused):
    """The host's Chrome is behind the current Stable major."""


def served_major(release: Any) -> int | None:
    """A release's major when Google serves it to at least MAJORITY of users, else None."""
    if not isinstance(release, dict):
        return None
    version, fraction = release.get("version"), release.get("fraction")
    if not isinstance(version, str) or not isinstance(fraction, int | float):
        return None
    major = version.split(".")[0]
    return int(major) if fraction >= MAJORITY and major.isdigit() else None


def stable_major(answer: Any) -> int | None:
    """The current Stable major of Google's `releases` answer; None when it names none."""
    releases = answer.get("releases") if isinstance(answer, dict) else None
    majors = [served_major(r) for r in releases] if isinstance(releases, list) else []
    return max((major for major in majors if major is not None), default=None)


def fetch_stable_major(os_name: str) -> int | None:
    """The current Stable major for this host OS from Google; None when it cannot be read."""
    url = RELEASES.format(platform=PLATFORMS[os_name])
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as answer:
            return stable_major(json.load(answer))
    except (OSError, ValueError) as exc:
        log.warning("Chrome Stable version not read (%s)", type(exc).__name__)
        return None


def behind(host: int | None, stable: int | None) -> bool:
    """Whether a known host major is older than a known current Stable major."""
    return host is not None and stable is not None and host < stable


class Watch:
    """The host's Chrome major and the current Stable major (None: not known yet)."""

    def __init__(
        self,
        executable: str | Path,
        os_name: str,
        *,
        fetch: Callable[[str], int | None] | None = None,
        interval: float = CHECK_S,
    ) -> None:
        self.executable, self.os_name, self.interval = executable, os_name, interval
        self.fetch = fetch or fetch_stable_major
        self.host: int | None = None
        self.stable: int | None = None

    def refresh(self) -> None:
        """Read both majors again (blocking: loop() runs it in a thread)."""
        try:
            self.host = int(chrome.detect(self.executable).split(".")[0])
        except chrome.ChromeRefused as exc:
            log.warning("Chrome version not read (%s)", exc)
            self.host = None
        stable = self.fetch(self.os_name)
        self.stable = self.stable if stable is None else stable
        if behind(self.host, self.stable):
            log.warning("Chrome %s is behind the current Stable %s", self.host, self.stable)

    async def loop(self, stop: asyncio.Event) -> None:
        """refresh() at once and every `interval` seconds until `stop`."""
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.refresh)
            except Exception as exc:  # boundary: the next pass reads again; type only
                log.warning("Chrome version check failed (%s)", type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.interval)

    def state(self) -> dict[str, int | None]:
        """The host line's `chrome_current`."""
        return {"host": self.host, "stable": self.stable}

    def check(self, target: Any, engine: str) -> None:
        """Refuse (Outdated) a Chrome run of a CURRENT_CHROME target while the host's Chrome
        is behind the current Stable major."""
        if engine != engines.CHROME or not getattr(target, "CURRENT_CHROME", False):
            return
        if behind(self.host, self.stable):
            raise Outdated(f"{CHROME_OUTDATED}: Chrome {self.host}, current Stable {self.stable}")
