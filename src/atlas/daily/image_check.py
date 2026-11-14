"""IMAGE-AUTO-1: is the worker image still right for the code (`atlas daily image`)?

The code reaches every droplet at launch (campaign.current_wheel), so the image only has to
fit the code's locked dependencies and carry a DB-IP release young enough. Read only, from the
manager's files: the image the daily template names (its spec's `image_name`), that image's
build record in the image inventory (`campaign image build` records `requirements_sha256` and
`mmdb_built_at`) and `<env>/wheels/current.json` (server-setup.sh: the code's
`requirements_sha256`). Two checks:

- `dependencies`: the image's fingerprint equals the code's, else failed (rebuild the image).
  Code-only changes never touch it. With nothing to compare (an image built before the
  fingerprint, or no current.json) it is ok and says so; the launch's `uv pip check` still stops
  a wheel the image does not fit;
- `db-ip`: the image's DB-IP release is at most MMDB_AGE old, else failed; for an image built
  before the release date was recorded, the build's own date stands in (the release is older).

A template or inventory that cannot be read, or no built record of the template's image, is one
failed check, `record`. No message names a path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..campaign.tick import load_spec
from .verify import Check

MMDB_AGE = timedelta(days=35)  # DB-IP releases monthly: a week to rebuild after a new one


@dataclass(frozen=True)
class ImageFiles:
    template: Path  # the daily template campaign's root
    images: Path  # the image inventory (`campaign image build --inventory`)
    wheels: Path  # `<env>/wheels`, holding current.json


def _record(files: ImageFiles) -> tuple[str, dict[str, Any] | None]:
    """The template's image name and its newest built record (None when there is none)."""
    name = load_spec(files.template).image_name
    builds = json.loads(files.images.read_text()).get("builds", [])
    found = [b for b in builds if b.get("snapshot_name") == name and b.get("state") == "built"]
    return name, (found[-1] if found else None)


def _code_fingerprint(wheels: Path) -> str | None:
    path = wheels / "current.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text()).get("requirements_sha256")
    return value if isinstance(value, str) else None


def dependencies(build: dict[str, Any], wheels: Path) -> Check:
    image, code = build.get("requirements_sha256"), _code_fingerprint(wheels)
    if image is None or code is None:
        missing = "the image" if image is None else "current.json"
        return Check("image", "dependencies", True, f"not compared: {missing} records none")
    if image != code:
        return Check(
            "image", "dependencies", False, "the code's locked dependencies changed: rebuild"
        )
    return Check("image", "dependencies", True, f"the code's ({code[:12]})")


def db_ip(build: dict[str, Any], now: datetime) -> Check:
    if not build.get("mmdb_sha256"):
        return Check("image", "db-ip", False, "the image holds no DB-IP database")
    recorded = build.get("mmdb_built_at")
    age = now - datetime.fromisoformat(recorded or build["started_at"])
    detail = f"release {age.days} days old" if recorded else f"built {age.days} days ago"
    if age > MMDB_AGE:
        return Check("image", "db-ip", False, f"{detail}, over {MMDB_AGE.days}: rebuild")
    return Check("image", "db-ip", True, detail)


def checks(files: ImageFiles, now: datetime) -> list[Check]:
    try:
        name, build = _record(files)
    except (OSError, ValueError, AttributeError) as error:
        detail = f"the template or image inventory is unreadable ({type(error).__name__})"
        return [Check("image", "record", False, detail)]
    if build is None:
        return [Check("image", "record", False, f"no built record of {name} in the inventory")]
    try:
        return [dependencies(build, files.wheels), db_ip(build, now)]
    except (OSError, ValueError, KeyError) as error:
        return [Check("image", "record", False, f"a record is unreadable ({type(error).__name__})")]
