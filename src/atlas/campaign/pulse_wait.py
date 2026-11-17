"""The pulse hour's wait (pulse_ondemand step 3): poll until the hour's runs of a set of armed
nodes are stored, removing each node at once when its own runs are (PULSE-COMPACT-1).

A run still queued or running at the run limit ends the wait (`still_open`, the hour timed out).
A run only `uploading` is on the manager already, waiting for it to store it (behind the hourly
calculation's ingest lock, PULSE-STATE-1): the wait goes on for it until the store limit, its
droplet staying up so an upload the manager refused can be sent again; then it is counted in
`waiting_to_store` and is no loss.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..orchestrator.pulse_hour import Armed, retire_nodes
from .pulse_removal import retire_many

WAIT_POLL_SECONDS = 15.0
# The runs of the hour's open batches not stored yet, with their node (provider, product and
# the node's region, which a handed-off country's batch does not share): `uploading` ones are
# with the manager already (PULSE-REPLACE-1).
BUSY_SQL = (
    "SELECT b.batch_id,br.status,n.provider,n.product,r.slug AS region FROM batch_runs br "
    "JOIN test_batches b USING(batch_id) JOIN nodes n ON n.node_id=br.node_id "
    "JOIN runner_regions r ON r.region_id=n.region_id WHERE b.kind='pulse' "
    "AND b.scheduled_for>=%s AND b.scheduled_for<%s AND b.status IN ('scheduled','running') "
    "AND br.status NOT IN ('done','failed','skipped','killed')"
)


@dataclass(frozen=True)
class Deadlines:
    runs: float  # a run still queued or running then ends the wait: timed_out
    stores: float  # runs only uploading (waiting to be stored) are waited for until then


async def remove_done(hour_root: Path, done: dict[str, Armed], s) -> dict:
    """Retire the nodes whose work is stored and remove their droplets at once (the guarded
    path, in parallel); each one's reason it is not gone. The final removal retries a failure."""
    try:
        await retire_nodes(s.db, list(done.values()))
    except Exception:  # boundary: a failed retire never keeps the droplets up
        pass
    return await retire_many(hour_root, done, s.cloud, s.clock)


def _left(rows: list) -> dict:
    """The batches with a run still queued or running (`still_open`) and, per node
    ("network/region"), how many of its runs those are (`open_nodes`); and the batches whose runs
    only wait for the manager to store them (`waiting_to_store`)."""
    running = [r for r in rows if r["status"] in ("queued", "running")]
    open_ = {r["batch_id"] for r in running}
    per_node = Counter(f"{r['provider']}/{r['region']}" for r in running)
    return {
        "still_open": len(open_),
        "open_nodes": dict(sorted(per_node.items())),
        "waiting_to_store": len({r["batch_id"] for r in rows} - open_),
    }


async def wait_and_remove(
    hour_root: Path,
    hour: datetime,
    armed: dict[str, Armed],
    s,
    limits: Deadlines,
    stalls=None,
    pulling: set = frozenset(),
) -> dict:
    """Poll until no run of the hour of the `armed` nodes is left to store, one is still queued
    or running at the run limit, or the store limit; at every poll move the pulses of a node
    that stalled (`stalls`, pulse_stall) and remove each armed node with none left, once (the
    final removal retries a failure). A node in `pulling` (provider, product, region; the
    paced late slot's, pulse_late.Puller, emptied as it goes) still gets runs: it is kept and
    the wait goes on. Returns _left's counts and the nodes removed."""
    tried: dict[str, str | None] = {}
    keys = {(cell.provider, cell.product, cell.region) for cell in armed.values()}
    while True:
        if stalls is not None:
            await stalls.poll(armed, set(tried), s)
        async with s.db.connection() as conn:
            cursor = await conn.execute(BUSY_SQL, (hour, hour + timedelta(hours=1)))
            found = await cursor.fetchall()
        rows = [r for r in found if (r["provider"], r["product"], r["region"]) in keys]
        busy = {(r["provider"], r["product"], r["region"]) for r in rows} | set(pulling)
        left = _left(rows)
        done = {
            name: cell
            for name, cell in armed.items()
            if name not in tried and (cell.provider, cell.product, cell.region) not in busy
        }
        if done:
            tried |= await remove_done(hour_root, done, s)
        limit = limits.runs if left["still_open"] else limits.stores
        if (not rows and not pulling) or s.clock.now() + WAIT_POLL_SECONDS > limit:
            return left | {"removed": [name for name, reason in tried.items() if reason is None]}
        await s.clock.sleep(WAIT_POLL_SECONDS)


def merge(first: dict, then: dict) -> dict:
    """Two phases' counts (the hour's, then a budgeted network's late phase) as one."""
    nodes = Counter(first["open_nodes"])
    nodes.update(then["open_nodes"])
    return {
        "still_open": first["still_open"] + then["still_open"],
        "open_nodes": dict(sorted(nodes.items())),
        "waiting_to_store": first["waiting_to_store"] + then["waiting_to_store"],
        "removed": first["removed"] + then["removed"],
    }


NOTHING = {"still_open": 0, "open_nodes": {}, "waiting_to_store": 0, "removed": []}
