"""IMAGE-AUTO-1: the wheel of the code the manager runs, shipped to every droplet at launch.

Every droplet already gets its wheel on SSH stdin when it is armed and installs it into the
image's venv (fanout.INSTALL_WHEEL), so the image only has to hold the OS, Python, the locked
dependencies and the DB-IP release. `scripts/server-setup.sh` builds the checked-out main
commit's wheel into `<env>/wheels/<sha12>/` and names it in `<env>/wheels/current.json`
(`commit`, `wheel`, `sha256`, `requirements_sha256`). A daily hour and an on-demand pulse hour
ship that wheel instead of the one their template or `fleet.json` names, and record it with its
commit, so a code change reaches the droplets with the next hour and we always know which code
ran. Without the file both keep the wheel they name, as before.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..onboarding.files import digest

CURRENT = "current.json"
WHEEL_RE = r"[0-9a-f]{12}/atlas-[0-9A-Za-z.+]+-py3-none-any\.whl"


def requirements_fingerprint(text: str) -> str:
    """SHA-256 of a `uv export` requirements file without its comment and blank lines (the
    header names the command line), as server-setup.sh and `image build` both record it."""
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    return hashlib.sha256("".join(line + "\n" for line in lines).encode()).hexdigest()


class CurrentWheelError(ValueError):
    """`current.json` is unreadable or does not match its wheel; the message names no path."""


@dataclass(frozen=True)
class CurrentWheel:
    commit: str
    wheel: Path
    sha256: str

    @property
    def version(self) -> str:
        return self.wheel.name.split("-")[1]

    def record(self) -> dict[str, str]:
        """What an hour's outcome records: the code that ran."""
        return {"commit": self.commit, "wheel": self.wheel.name, "wheel_sha256": self.sha256}


def wheels_dir(environment_root: Path) -> Path:
    """`<env>/wheels`: beside `campaigns/` and `pulse/` on the manager."""
    return environment_root / "wheels"


def load(wheels: Path) -> CurrentWheel | None:
    """The current wheel, or None when the server setup never wrote one. A file that does not
    match its wheel refuses: the hour must not ship code nobody can name."""
    path = wheels / CURRENT
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        commit, name, sha256 = raw["commit"], raw["wheel"], raw["sha256"]
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise CurrentWheelError(f"{CURRENT} is unreadable ({type(error).__name__})") from None
    fields_ok = (
        isinstance(commit, str)
        and re.fullmatch(r"[0-9a-f]{40}", commit)
        and isinstance(name, str)
        and re.fullmatch(WHEEL_RE, name)
        and isinstance(sha256, str)
        and name.startswith(sha256[:12] + "/")
    )
    if not fields_ok:
        raise CurrentWheelError(f"{CURRENT} names no valid commit, wheel and SHA-256")
    wheel = wheels / name
    if not wheel.is_file() or digest(wheel) != sha256:
        raise CurrentWheelError(f"{CURRENT}: the wheel is missing or its SHA-256 differs")
    return CurrentWheel(commit, wheel, sha256)
