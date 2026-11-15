"""A manager root for the daily image rebuild tests (IMAGE-DAILY-1): the DB-IP directory, the
current wheel, the daily template, the image inventory and the pulse fleet (fakes only)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from campaign_image_cases import PUBLIC_KEY
from campaign_tick_cases import make_spec
from pulse_fleet_cases import make_root as make_fleet

from atlas.campaign.contracts import spec_path
from atlas.campaign.current_wheel import requirements_fingerprint
from atlas.campaign.image_daily import Paths
from atlas.regions import REGION_IDS

OLD_ID = "500"
OLD_NAME = "atlas-camp-worker-0.9.4-0123456789ab-20261113"
RELEASE = b"the 2026-11-15 release"
RELEASE_SHA = hashlib.sha256(RELEASE).hexdigest()
WHEEL = b"wheel bytes"
WHEEL_SHA = hashlib.sha256(WHEEL).hexdigest()
REQUIREMENTS = "# uv export header\naiohttp==3.14.3\n"
FINGERPRINT = requirements_fingerprint(REQUIREMENTS)
NEW_NAME = f"atlas-camp-worker-0.9.5-{WHEEL_SHA[:12]}-20261115"
CID = "20261101t000000z-0123456789ab"
AT_0550 = datetime(2026, 11, 15, 5, 50, tzinfo=UTC)


class Reader:
    """A fake maxminddb reader of a DB-IP database."""

    def metadata(self):
        return type(
            "Metadata", (), {"database_type": "DBIP-Location-ISP", "build_epoch": 1_763_164_800}
        )()

    def close(self):
        pass


class Shell:
    """Fake SSH: `true` answers; the provision run ends with each verdict in turn."""

    def __init__(self, *verdicts):
        self.verdicts, self.calls = list(verdicts or ["clean"]), []

    async def __call__(self, argv, stdin):
        self.calls.append(argv)
        if argv[-1] == "true":
            return 0, ""
        verdict = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
        return (0 if verdict == "clean" else 1), f"apt output 10.1.2.3\n{verdict}\n"


class Clock:
    """Wall time that moves only when slept; records each sleep."""

    def __init__(self, at: datetime = AT_0550):
        self.t, self.sleeps = at.timestamp(), []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@dataclass
class Alerts:
    sent: list

    async def __call__(self, day, message):
        self.sent.append((day, message))


def old_build(**values) -> dict:
    return {
        "started_at": "2026-11-13T10:00:00+00:00",
        "state": "built",
        "snapshot_id": OLD_ID,
        "snapshot_name": OLD_NAME,
        "mmdb_sha256": "0" * 64,
        "requirements_sha256": FINGERPRINT,
        **values,
    }


def write_builds(paths: Paths, *builds: dict) -> None:
    paths.images.parent.mkdir(parents=True, exist_ok=True)
    paths.images.write_text(json.dumps({"builds": list(builds)}))


def manager(tmp_path: Path, *, fleet: bool = True) -> Paths:
    """A production manager root on the old image, with a newer DB-IP release installed."""
    root = tmp_path / "production"
    paths = Paths.of(root)
    paths.dbip.mkdir(parents=True)
    (paths.dbip / "dbip-full.mmdb").write_bytes(RELEASE)
    source = {"name": "dbip-full-2026-11-15.mmdb", "date": "2026-11-15"}
    release = {"file": "dbip-full.mmdb", "size": len(RELEASE), "sha256": RELEASE_SHA}
    (paths.dbip / "release.json").write_text(json.dumps(release | {"source": source}))
    folder = paths.wheels / WHEEL_SHA[:12]
    folder.mkdir(parents=True)
    (folder / "atlas-0.9.5-py3-none-any.whl").write_bytes(WHEEL)
    (folder / "requirements.txt").write_text(REQUIREMENTS)
    current = {
        "commit": "c" * 40,
        "wheel": f"{WHEEL_SHA[:12]}/atlas-0.9.5-py3-none-any.whl",
        "sha256": WHEEL_SHA,
        "requirements_sha256": FINGERPRINT,
    }
    (paths.wheels / "current.json").write_text(json.dumps(current))
    template = root / "campaigns" / CID
    template.mkdir(parents=True)
    spec = make_spec(
        campaign_id=CID, image_by_region={"lon1": OLD_ID, "nyc3": OLD_ID}, image_name=OLD_NAME
    )
    spec_path(template).write_text(spec.validate().to_json())
    paths.template.parent.mkdir()
    paths.template.symlink_to(template)
    write_builds(paths, old_build())
    paths.builder_key.write_text("private part, never read\n")
    paths.builder_key.with_name(paths.builder_key.name + ".pub").write_text(PUBLIC_KEY + "\n")
    if fleet:
        made = make_fleet(tmp_path, regions=("lon1", "fra1"))
        raw = json.loads((made / "fleet.json").read_text())
        made.rename(paths.pulse)
        raw["image_by_region"] = dict.fromkeys(raw["regions"], int(OLD_ID))
        paths.fleet.write_text(json.dumps(raw))
    return paths


def old_image(fake, image_id: str = OLD_ID, name: str = OLD_NAME, regions=REGION_IDS) -> None:
    fake.images[image_id] = {
        "id": int(image_id),
        "name": name,
        "type": "snapshot",
        "regions": list(regions),
        "droplet": "1",
    }
