"""IMAGE-DAILY-1 (#1136): rebuild the worker image when the manager's DB-IP release changes.

The droplets keep DB-IP (the run-time rule that a success from an exit in another country is a
duplicate, and the stop decision, need it). The manager refreshes its copy every day (the
orchestrator's `dbip_update` job, 05:23 UTC); an image holds the release baked into it when it
was built. `atlas campaign image-daily`, run on the manager after the update, brings the image
up to the manager's release:

1. The manager's release (`<root>/db-ip/release.json`, the update's record) against the one in
   the image the daily template names (its build record in `.secrets/campaign-images.json`
   holds `mmdb_sha256`). Equal: nothing to do.
2. Otherwise a recorded build that already holds the release and the code's locked
   dependencies is taken (copied first to any region it misses, as after a failed copy), else
   one is built as `campaign image build` does: a lon1 builder, the manager's wheel and its
   requirements (`wheels/<sha12>/`, scripts/server-setup.sh), the manager's `dbip-full.mmdb`
   and the copies to every region side by side, named
   atlas-camp-worker-<version>-<wheel sha12>-<the release's date>.
3. The image must be in every region. Then, at a quiet minute (QUIET, never around the :56 and
   :57 launches), the daily template (`image_by_region`, `image_name`) and the pulse fleet
   (`fleet.json` `image_by_region`, snapshot ids) point at it, each file replaced in one step.
   An hour already running keeps the image it was launched with.

4. Then the old images go (image_prune): through the scope guard, once nothing uses them.

A failed attempt leaves the template and the fleet on the old image; the build removes its
builder through the scope guard whatever happens. It is tried once more RETRY_SECONDS later; a
second failure opens one major alert (ALERT, posted to Slack) with the reason: the failed
step and our own message, never a log line. `plan` reads only local files (the dry run).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..dbip.files import read_state
from ..enrichment import CURRENT
from ..onboarding.files import atomic_bytes, lock
from ..onboarding.scheduler import Clock
from ..regions import REGION_IDS
from . import current_wheel, image, ops_api, pulse_fleet
from .contracts import spec_path
from .do_api import DigitalOcean
from .manager_pulse import PULSE_ROOT
from .tick import load_spec

QUIET = (15, 45)  # the template switch only from :15 to :45 past an hour
RETRY_SECONDS = 30 * 60
ALERT = "image_daily_failed"  # image_daily_cli.ALERT
REQUIREMENTS = "requirements.txt"  # beside the wheel, written by scripts/server-setup.sh
BUILDER_KEY = "image-builder-key"  # the builder's SSH key, made on the manager once
# Where an alert goes: (the run's day, its message).
Alert = Callable[[date, str], Awaitable[None]]


class DailyImageError(RuntimeError):
    """A step that cannot go on; the message names no path, token or address."""


@dataclass(frozen=True)
class Paths:
    template: Path  # the daily template campaign (`daily/template`, a link to it)
    images: Path  # the image inventory
    wheels: Path  # `<root>/wheels`, holding current.json
    dbip: Path  # the manager's DB-IP directory: dbip-full.mmdb and release.json
    pulse: Path  # the pulse root, holding fleet.json once the pulses are set up
    builder_key: Path

    @classmethod
    def of(cls, root: Path, dbip: Path | None = None) -> Paths:
        """An environment's files under its manager root (`~/atlas/<env>`)."""
        secrets = root / ".secrets"
        return cls(
            root / "daily" / "template",
            secrets / "campaign-images.json",
            current_wheel.wheels_dir(root),
            dbip or root / "db-ip",
            root / Path(PULSE_ROOT).name,
            secrets / BUILDER_KEY,
        )

    @property
    def fleet(self) -> Path:
        return self.pulse / "fleet.json"


@dataclass(frozen=True)
class Code:
    """The manager's current wheel and its locked requirements."""

    wheel: Path
    sha256: str
    requirements: Path
    fingerprint: str


def builds(paths: Paths) -> list[dict[str, Any]]:
    if not paths.images.exists():
        return []
    return json.loads(paths.images.read_text()).get("builds", [])


def manager_release(dbip: Path) -> dict[str, str]:
    """The release the manager installed (`dbip update`'s release.json)."""
    state = read_state(dbip)
    source = state.get("source") or {}
    if not state.get("sha256") or not source.get("date"):
        raise DailyImageError("the manager has no installed DB-IP release (dbip_update)")
    return {"name": source["name"], "date": source["date"], "sha256": state["sha256"]}


def current_image(paths: Paths) -> dict[str, Any]:
    """The one image the daily template names, with its build record's DB-IP release."""
    spec = load_spec(paths.template)
    ids = set(spec.image_by_region.values())
    if len(ids) != 1:
        raise DailyImageError("the daily template names more than one image")
    image_id = ids.pop()
    found = [
        b
        for b in builds(paths)
        if b.get("state") == "built" and str(b.get("snapshot_id")) == image_id
    ]
    release = found[-1].get("mmdb_sha256") if found else None
    return {"id": image_id, "name": spec.image_name, "mmdb_sha256": release}


def code(paths: Paths) -> Code:
    """The current wheel and the requirements beside it, which must be the ones it records."""
    found = current_wheel.load(paths.wheels)
    if found is None:
        raise DailyImageError("no current wheel: run scripts/server-setup.sh")
    requirements = found.wheel.parent / REQUIREMENTS
    if not requirements.is_file():
        raise DailyImageError(f"no {REQUIREMENTS} beside the wheel: run scripts/server-setup.sh")
    fingerprint = current_wheel.requirements_fingerprint(requirements.read_text())
    recorded = json.loads((paths.wheels / current_wheel.CURRENT).read_text())
    if recorded.get("requirements_sha256") not in (None, fingerprint):
        raise DailyImageError(f"the {REQUIREMENTS} beside the wheel is not the recorded one")
    return Code(found.wheel, found.sha256, requirements, fingerprint)


def ready_builds(paths: Paths, release: dict[str, str], wanted: Code) -> list[dict[str, Any]]:
    """Recorded snapshots holding the release and the code's dependencies, newest first."""
    return [
        b
        for b in reversed(builds(paths))
        if b.get("snapshot_id")
        and b.get("mmdb_sha256") == release["sha256"]
        and b.get("requirements_sha256") == wanted.fingerprint
    ]


def targets(paths: Paths) -> dict[str, Any]:
    """What a switch rewrites: the template's campaign id and, if set up, the fleet file."""
    found: dict[str, Any] = {"daily_template": load_spec(paths.template).campaign_id}
    found["pulse_fleet"] = "fleet.json" if paths.fleet.exists() else None
    return found


def plan(paths: Paths) -> dict[str, Any]:
    """What a run would do, from the manager's files only (no cloud call)."""
    release, current = manager_release(paths.dbip), current_image(paths)
    found = {"release": release, "image": current, "targets": targets(paths)}
    if current["mmdb_sha256"] == release["sha256"]:
        return found | {"action": "none"}
    wanted = code(paths)
    ready = ready_builds(paths, release, wanted)
    if ready:
        name = ready[0]["snapshot_name"]
        return found | {"action": "reuse", "new_image": name, "candidates": ready}
    name = image.snapshot_name(wanted.wheel, wanted.sha256, date.fromisoformat(release["date"]))
    return found | {"action": "build", "new_image": name, "candidates": []}


def quiet_delay(now: datetime) -> float:
    """Seconds until the next quiet minute; 0 inside one."""
    start, end = QUIET
    if start <= now.minute < end:
        return 0.0
    then = now.replace(minute=start, second=0, microsecond=0)
    if now.minute >= end:
        then += timedelta(hours=1)
    return (then - now).total_seconds()


@dataclass
class Context:
    """What an applied run uses: the cloud, the files, where its alert goes and the clock."""

    cloud: DigitalOcean
    paths: Paths
    alert: Alert
    clock: Clock = field(default_factory=Clock)
    shell: image.Shell = image.run_shell

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock.now(), UTC)


def job(context: Context, release: dict[str, str]) -> image.ImageJob:
    wanted, paths = code(context.paths), context.paths
    return image.ImageJob(
        wanted.wheel,
        wanted.requirements,
        image.ensure_mac_key(paths.builder_key),
        paths.images,
        mmdb=paths.dbip / CURRENT,
        day=date.fromisoformat(release["date"]),
    )


async def _reuse(context: Context, record: dict[str, Any]) -> dict[str, str] | None:
    """A recorded snapshot of the release, copied to the regions it misses; None when gone."""
    image_id, name = str(record["snapshot_id"]), record["snapshot_name"]
    found = await context.cloud.image(image_id)
    if found is None or found.get("name") != name:
        return None
    regions = await ops_api.transfer(context.cloud, image_id, list(REGION_IDS), context.clock.sleep)
    if record.get("state") != "built":  # a build whose copies failed: finished now
        values = {k: v for k, v in record.items() if k not in ("started_at", "state")}
        build = image.Build(datetime.fromisoformat(record["started_at"]), "built", values)
        image.write_record(context.paths.images, build, regions=regions)
    return {"id": image_id, "name": name}


async def obtain(context: Context, planned: dict[str, Any]) -> dict[str, str]:
    """The image holding the release: a recorded one, else a new build."""
    for record in planned["candidates"]:
        found = await _reuse(context, record)
        if found is not None:
            return found
    entry = await image.build(
        context.cloud,
        job(context, planned["release"]),
        shell=context.shell,
        sleep=context.clock.sleep,
        now=context.now(),
    )
    return {"id": str(entry["snapshot_id"]), "name": entry["snapshot_name"]}


def regions_needed(paths: Paths) -> set[str]:
    """Every campaign region, and any other the template or the fleet launches in."""
    needed = set(REGION_IDS) | set(load_spec(paths.template).regions)
    if paths.fleet.exists():
        needed |= set(json.loads(paths.fleet.read_text()).get("regions") or [])
    return needed


async def verify(context: Context, new: dict[str, str]) -> None:
    found = await context.cloud.image(new["id"]) or {}
    missing = sorted(regions_needed(context.paths) - set(found.get("regions", [])))
    if found.get("name") != new["name"] or missing:
        raise DailyImageError(f"image {new['name']} is not in every region (missing {missing})")


def _new_fleet(paths: Paths, image_id: str) -> dict[str, Any] | None:
    """fleet.json pointing at `image_id`; None without a fleet. A file the pulses refuse as
    it is stops the switch before anything is written."""
    if not paths.fleet.exists():
        return None
    try:
        pulse_fleet.load_config(paths.pulse)
    except (pulse_fleet.FleetError, KeyError, TypeError, ValueError):
        raise DailyImageError("fleet.json is refused as it is: nothing was switched") from None
    raw = json.loads(paths.fleet.read_text())
    return raw | {"image_by_region": dict.fromkeys(raw["image_by_region"], image_id)}


def switch(paths: Paths, new: dict[str, str]) -> dict[str, Any]:
    """Point the pulse fleet and the daily template at `new`, each file in one step. The fleet
    goes first: after a failed template write the next try still sees the old release."""
    root = paths.template.resolve()
    spec = load_spec(root)
    fleet = _new_fleet(paths, new["id"])
    by_region = dict.fromkeys(spec.image_by_region, new["id"])
    updated = replace(spec, image_by_region=by_region, image_name=new["name"]).validate()
    if fleet is not None:
        atomic_bytes(paths.fleet, (json.dumps(fleet, indent=2) + "\n").encode())
    atomic_bytes(spec_path(root), updated.to_json().encode())
    found = "fleet.json" if fleet is not None else None
    return {"daily_template": spec.campaign_id, "pulse_fleet": found}


async def attempt(context: Context, planned: dict[str, Any], step: list[str]) -> dict[str, Any]:
    """One try: obtain, verify, wait for a quiet minute, switch. `step` names where it is."""
    step[0] = "build" if planned["action"] == "build" else "reuse"
    new = await obtain(context, planned)
    step[0] = "verify"
    await verify(context, new)
    step[0] = "switch"
    while delay := quiet_delay(context.now()):
        await context.clock.sleep(delay)
    return {"new_image": new, "switched": switch(context.paths, new)}


def reason(step: str, error: Exception) -> str:
    """Why a try failed, for the alert: the step and our own message or the error's class."""
    if isinstance(error, DailyImageError):
        return f"{step}: {error}"
    return f"{step}: {type(error).__name__}"


async def run(context: Context) -> dict[str, Any]:
    """Bring the image to the manager's release: two tries, then one alert."""
    with ExitStack() as held:
        try:
            held.enter_context(lock(context.paths.images.parent / ".image-daily.lock"))
        except ValueError:
            raise DailyImageError("another image-daily run is going on") from None
        return await _tries(context)


async def _try(context: Context, step: list[str]) -> dict[str, Any]:
    """Plan and, when the release changed, obtain and switch; `keep` is the image in use."""
    planned = plan(context.paths)
    if planned["action"] == "none":
        keep = {key: planned["image"][key] for key in ("id", "name")}
        return planned | {"state": "unchanged", "keep": keep}
    switched = await attempt(context, planned, step)
    return planned | switched | {"state": "switched", "keep": switched["new_image"]}


async def _tries(context: Context) -> dict[str, Any]:
    failures: list[str] = []
    for number in (1, 2):
        step = ["plan"]
        try:
            outcome = await _try(context, step)
        except Exception as error:  # boundary: kept for the alert; the old image stays in use
            failures.append(reason(step[0], error))
        else:
            return await _pruned(context, outcome | {"failures": failures})
        if number == 1:
            await context.clock.sleep(RETRY_SECONDS)
    day = context.now().date()
    message = f"worker image not rebuilt on {day}, the old one stays: {failures[-1]}"
    await context.alert(day, message)
    return {"state": "failed", "failures": failures}


async def _pruned(context: Context, outcome: dict[str, Any]) -> dict[str, Any]:
    """The old images removed (image_prune); a refusal, an unrecorded worker snapshot or an
    error is one alert. The switch stands either way."""
    from .image_prune import prune, report

    try:
        found = await prune(context, outcome["keep"])
        message = report(found)
    except Exception as error:  # boundary: the old images wait for the next run
        found = {"error": reason("prune", error)}
        message = found["error"]
    if message:
        day = context.now().date()
        await context.alert(day, f"worker images on {day}: {message}")
    return outcome | {"prune": found}
