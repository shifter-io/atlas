"""IMAGE-DAILY-1 (#1136): after a switch, remove the old worker images; keep exactly one.

`image_daily.run` calls `prune` once the daily template and the pulse fleet point to the new
image (and also when nothing changed, so a removal left over from an earlier day is finished).
A recorded image (a build in the image inventory with its snapshot id) other than the current
one is removed through the scope guard (`ops_api.release`, kind snapshot) only when:

- the current image is in every region its template and fleet launch in (a fresh GET);
- no daily template and no pulse fleet on this server points to it: this environment's and
  every other environment's beside it (staging launches from production's image, read only);
- a fresh GET names it `atlas-camp-worker-*` (never a snapshot named otherwise);
- no droplet under a campaign or pulse role tag (both environments, old names too) was created
  from it. Such a droplet belongs to an hour launched before the switch: the check is repeated
  every WAIT_POLL_SECONDS until WAIT_LIMIT_SECONDS, then the image is left for the next run.

A removed build keeps its record, its snapshot id moved to `removed_snapshot_id`. Snapshots no
record names (a pre-rename `pi-camp-worker-*`, one built elsewhere) are never removed: they are
reported in `extra`, and so is any image still kept; with either, `exactly_one` is false.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from ..legacy_names import current as current_name
from . import image, ops_api
from .contracts import ALL_ROLES, PROJECT_NAME, with_legacy
from .do_api import SNAPSHOT_NAME_RE
from .image_daily import Context, Paths, builds, verify
from .scope import OwnershipError
from .tick import load_spec

WAIT_POLL_SECONDS = 5 * 60.0
WAIT_LIMIT_SECONDS = 3 * 3600.0
NOT_WORKER = "not an atlas-camp-worker image"


def referenced(paths: Paths) -> set[str]:
    """Every image id a daily template or pulse fleet beside this environment's names."""
    found: set[str] = set()
    for root in sorted(paths.template.parents[1].parent.iterdir()):
        other = Paths.of(root)
        if other.template.exists():
            found |= set(load_spec(other.template).image_by_region.values())
        if other.fleet.exists():
            images = json.loads(other.fleet.read_text()).get("image_by_region") or {}
            found |= {str(value) for value in images.values()}
    return found


async def users(context: Context, image_id: str) -> list[str]:
    """The droplets under a campaign or pulse role tag created from the image."""
    tags = with_legacy(sorted({tag for tag, _ in ALL_ROLES}))
    found = set()
    for tag in tags:
        for raw in await context.cloud._pages("/v2/droplets", "droplets", {"tag_name": tag}):
            if str((raw.get("image") or {}).get("id")) == image_id:
                found.add(str(raw["id"]))
    return sorted(found)


def candidates(paths: Paths, keep: set[str]) -> dict[str, dict[str, Any]]:
    """The recorded images not kept, by snapshot id (the newest record of each)."""
    found = {str(b["snapshot_id"]): b for b in builds(paths) if b.get("snapshot_id")}
    return {image_id: b for image_id, b in found.items() if image_id not in keep}


def mark_removed(paths: Paths, record: dict[str, Any], now: datetime) -> None:
    values = {k: v for k, v in record.items() if k not in ("started_at", "state", "snapshot_id")}
    build = image.Build(datetime.fromisoformat(record["started_at"]), record["state"], values)
    removed = {"removed_snapshot_id": str(record["snapshot_id"]), "removed_at": now.isoformat()}
    image.write_record(paths.images, build, **removed)


async def _removable(context: Context, image_id: str) -> str:
    """Why the image must stay ("" when it may go now): its name, or droplets still on it."""
    found = await context.cloud.image(image_id)
    if found is None:
        return ""
    if not re.fullmatch(SNAPSHOT_NAME_RE, str(found.get("name"))):
        return NOT_WORKER
    return "in use" if await users(context, image_id) else ""


async def _wait(context: Context, pending: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Each image's reason to stay once the waiting is over ({} when all may go)."""
    waited = 0.0
    while True:
        reasons = {i: await _removable(context, i) for i in pending}
        held = {i: r for i, r in reasons.items() if r}
        if not any(r == "in use" for r in held.values()) or waited >= WAIT_LIMIT_SECONDS:
            return held
        await context.clock.sleep(WAIT_POLL_SECONDS)
        waited += WAIT_POLL_SECONDS


async def _extra(context: Context, keep: str, removed: set[str]) -> list[dict[str, str]]:
    """Worker snapshots besides the kept one (also pre-rename names), for the report."""
    listed = await context.cloud._pages("/v2/snapshots", "snapshots", {"resource_type": "droplet"})
    worker = re.compile(SNAPSHOT_NAME_RE)
    return [
        {"id": str(s["id"]), "name": str(s.get("name"))}
        for s in listed
        if worker.fullmatch(current_name(str(s.get("name"))))
        and str(s["id"]) != keep
        and str(s["id"]) not in removed
    ]


async def prune(context: Context, keep: dict[str, str]) -> dict[str, Any]:
    """Remove every recorded image but `keep` (id, name) that nothing uses any more."""
    paths, kept = context.paths, keep["id"]
    await verify(context, keep)
    named = referenced(paths)
    recorded = set(candidates(paths, set()))
    pending = candidates(paths, named | {kept})
    held = await _wait(context, pending)
    scope = ops_api.ops_scope(await context.cloud.project_id(PROJECT_NAME), context.now())
    audit = paths.images.with_name("campaign-ops-audit.jsonl")
    removed, refused = set(), {}
    for image_id, record in pending.items():
        if image_id in held:
            continue
        try:
            await ops_api.release(context.cloud, "snapshot", image_id, scope, audit)
        except OwnershipError as error:
            refused[image_id] = f"refused: {', '.join(error.failed)}"
            continue
        mark_removed(paths, record, context.now())
        removed.add(image_id)
    extra = await _extra(context, kept, removed)
    return {
        "kept": keep,
        "removed": sorted(removed),
        "held": held | refused,
        "refused": sorted(refused),
        "referenced_elsewhere": sorted(named - {kept}),
        "extra": extra,
        "unrecorded": [e["name"] for e in extra if e["id"] not in recorded],
        "exactly_one": not extra,
    }


def report(found: dict[str, Any]) -> str:
    """What the owner must hear about a prune ("" when nothing): a refused removal, a recorded
    image named otherwise or a worker snapshot no record names (both to be removed by hand), or
    an image another template still names."""
    parts = [f"refused {', '.join(found['refused'])}"] if found["refused"] else []
    named = [i for i, why in found["held"].items() if why == NOT_WORKER]
    if named:
        parts.append(f"recorded but not named atlas-camp-worker-*, left alone: {', '.join(named)}")
    if found["unrecorded"]:
        parts.append(f"not recorded, left alone: {', '.join(found['unrecorded'])}")
    if found["referenced_elsewhere"]:
        parts.append(f"another template or fleet names {', '.join(found['referenced_elsewhere'])}")
    return "; ".join(parts)
