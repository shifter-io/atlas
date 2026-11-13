"""`campaign probe --remove` and `campaign shot --remove`: find a worker a stopped run left by
its exact name under the run's hour tag (and, when an id is recorded, only that id), then remove
it through `probe.release`. Never touches anything the record does not name.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..onboarding.files import atomic_json
from ..onboarding.manifest import timestamp
from . import ops_api
from .contracts import DropletRecord, Scope, audit_path, hour_tag, short_id
from .do_api import DigitalOcean
from .probe import ProbeError, adopt, release


async def recover_worker(
    cloud: DigitalOcean, run_scope: Scope, worker: DropletRecord, audit: Path, sleep: ops_api.Sleep
) -> list[DropletRecord]:
    """Find one recorded worker by its exact name under the run's hour tag (and, when an id is
    recorded, only that id), then `release`; `absent` when never created or already gone.
    Only a listing that answered may say "absent"; an unreadable one raises `ProbeError` and
    changes nothing. With no id recorded, every droplet with the name is ours (the hour tag,
    the exact name, adopted as our own create): the first becomes the worker, the others are
    released too and returned, as `unrecorded` records, for the caller's record."""
    if worker.status == "destroyed":
        return []
    extra: list[DropletRecord] = []
    try:
        if worker.droplet_id is None:
            found = await cloud.adopt_uncertain([worker.name], hour_tag(run_scope.campaign_id, 0))
            if found:
                worker.droplet_id, worker.error_class = found[0].id, None
            extra = [
                DropletRecord(
                    worker.network,
                    worker.region,
                    worker.hour,
                    worker.name,
                    droplet_id=d.id,
                    size=worker.size,
                    image_id=worker.image_id,
                    error_class="unrecorded",
                )
                for d in found[1:]
            ]
            gone = not found
        else:
            gone = await adopt(cloud, worker, run_scope.campaign_id, strict=True) is None and (
                await cloud.get(str(worker.droplet_id)) is None
            )
    except Exception as error:
        raise ProbeError(
            f"could not read the probe's droplets ({type(error).__name__}); "
            "nothing changed, run --remove again"
        ) from None
    if gone:
        worker.status = "absent"  # never created, or already gone
        return []
    for record in (worker, *extra):
        await release(cloud, run_scope, record, audit, sleep)
    return extra


async def recover(
    cloud: DigitalOcean, folder: Path, *, sleep: ops_api.Sleep = asyncio.sleep
) -> dict[str, Any]:
    """`campaign probe --remove <folder>`: remove a probe's worker that its own run left.

    Rebuilds the scope from the folder's record, finds the worker by its exact name under the
    probe's hour tag (and, when an id is recorded, only that id), then `release`. Writes the
    outcome back into the record; never touches anything the record does not name."""
    path = folder / "probe-result.json"
    try:
        result = json.loads(path.read_text())
        recorded = result["scope"]
        worker = DropletRecord(**result["worker"])
        probe_scope = Scope(
            result["probe_id"],
            short_id(result["probe_id"]),
            recorded["project_id"],
            recorded["manager_droplet_id"],
            datetime.fromisoformat(recorded["created_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ProbeError(f"no probe record in that folder ({type(error).__name__})") from None
    # A second droplet with the worker's name (S1r) is kept under `unrecorded` until removed.
    twins = [DropletRecord(**raw) for raw in result.get("unrecorded", [])]
    for record in (worker, *twins):
        twins += await recover_worker(cloud, probe_scope, record, audit_path(folder), sleep)
    if twins:
        result["unrecorded"] = [asdict(twin) for twin in twins]
    status = next(
        (t.status for t in twins if t.status not in ("destroyed", "absent")), worker.status
    )  # a twin still alive keeps the hint and the failure
    result.update(
        worker=asdict(worker),
        worker_status=status,
        recovered_at=timestamp(datetime.now(UTC).timestamp()),
    )
    if status in ("destroyed", "absent"):
        result.pop("recover", None)
    atomic_json(path, result)
    return {"probe_id": result["probe_id"], "worker_status": status}
