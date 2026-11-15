"""PULSE-STATE-1 (2/2): a pulse droplet that stops working mid-hour has its pulses moved.

At every poll of the hour's wait (pulse_wait.wait_and_remove), orchestrator.pulse_moves.stalled
names the armed nodes with a run `pulse.stall_minutes` (3) overdue and no heartbeat, or no run
progressed or finished, in that time (starting one is no sign of work). Each such node is
handled once, at once:

- its pulses go to the armed droplet of the same network still up in another region with the
  fewest countries (it runs them from there, in their countries' own batches, as a cell handed
  off at the start: pulse_replace.hand_off), the stalled droplet then being removed as done;
- with no such droplet, its own cell is replaced once (pulse_replace.replace_once: the guarded
  removal and one new droplet, the same create, project and arming checks, one for one, so the
  cap holds) and the pulses wait for the new one (the same node row).

Its runs not uploaded yet move (pulse_moves.move_runs): those whose start passed start again
back to back; a run with no time left before LAST_START fails `node_lost`. `stalled` in
pulse.json records each ("node lost, cells moved"): the cell, when it was last seen working
(`last_seen`) and found stalled (`found_at`), where its pulses went, how many moved and how many
were lost. A cell with lost pulses is in `lost` (the hour partial, `pulse_cells_lost`); the
hour's stalls share one minor alert, `pulse_nodes_stalled`, one line ("pulse hour <hour>:
webshare/fra1 stopped at 11:10, 109 country pulses: 80 moved to webshare/ams3, 29 lost"), and
the reaper's `run_stale` and `run_not_started` alerts of the hour's nodes are acknowledged when
the hour ends (pulse_moves.acknowledge_runs). The operator looks into why a droplet stalled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..orchestrator import pulse_moves
from ..orchestrator.pulse_hour import Armed
from . import pulse_replace
from .pulse_alerts import alert, names, title


def _key(cell: Armed) -> tuple[str, str, str]:
    return (cell.provider, cell.product, cell.region)


def helper(name: str, armed: dict[str, Armed], skip: set[str]) -> str | None:
    """The armed node of the same network in another region, still up, with the fewest
    countries (then by name); None when there is none."""
    cell = armed[name]
    options = [
        (len(other.countries), other_name)
        for other_name, other in armed.items()
        if other_name not in skip
        and (other.provider, other.product) == (cell.provider, cell.product)
        and other.region != cell.region
    ]
    return min(options)[1] if options else None


@dataclass
class Stalls:
    """The hour's stalled nodes and where their pulses went."""

    hour_root: Path
    hour: datetime
    config: object  # the hour's FleetConfig, for a replacement
    labels: dict[str, str]  # node name -> "network/region"
    entries: list[dict] = field(default_factory=list)

    async def poll(self, armed: dict[str, Armed], gone: set[str], s) -> None:
        """Move the pulses of each armed node newly stalled (none removed already)."""
        now = datetime.fromtimestamp(s.clock.now(), UTC)
        by_key = {_key(cell): name for name, cell in armed.items()}
        handled = {entry["name"] for entry in self.entries}
        for row in await pulse_moves.stalled(s.db, self.hour, now):
            name = by_key.get((row["provider"], row["product"], row["region"]))
            if name is None or name in handled or name in gone:
                continue
            handled.add(name)
            self.entries.append(await self._move(name, row, armed, gone | handled, s))

    async def _move(self, name: str, row: dict, armed: dict[str, Armed], skip: set, s) -> dict:
        found, seen = datetime.fromtimestamp(s.clock.now(), UTC), row["last_seen"]
        entry = {"name": name, "cell": self.labels[name], "last_seen": seen and seen.isoformat()}
        to = helper(name, armed, skip)
        if to is not None:
            entry["to"], target = self.labels[to], _key(armed[to])
        else:
            [replaced] = await pulse_replace.replace_once(self.hour_root, self.config, [name], s)
            entry["to"] = "replaced"
            target = _key(armed[name]) if replaced["armed"] else None
        now = datetime.fromtimestamp(s.clock.now(), UTC)
        entry["found_at"] = found.isoformat()
        return entry | await pulse_moves.move_runs(s.db, self.hour, row["node_id"], target, now)

    def summary(self) -> list[dict]:
        """The `stalled` entries of pulse.json."""
        return [{k: v for k, v in entry.items() if k != "name"} for entry in self.entries]

    def lost(self) -> list[str]:
        """The cells whose stall lost pulses."""
        return sorted({entry["cell"] for entry in self.entries if entry["lost"]})


def _line(entry: dict) -> str:
    seen = entry["last_seen"]
    at = f" at {datetime.fromisoformat(seen):%H:%M}" if seen else ""
    total = entry["moved"] + entry["lost"]
    where = "its replacement" if entry["to"] == "replaced" else entry["to"]
    moved = f"{entry['moved']} moved to {where}" if entry["moved"] else ""
    lost = f"{entry['lost']} lost" if entry["lost"] else ""
    parts = ", ".join(part for part in (moved, lost) if part) or "none left"
    return f"{entry['cell']} stopped{at}, {total} country pulses: {parts}"


async def stall_alert(db, hour: datetime, stalled: list[dict]) -> None:
    """One minor alert for the hour's stalls (the loss pages as pulse_cells_lost)."""
    if not stalled:
        return
    message = f"{title(hour)}: {names([_line(entry) for entry in stalled], '; ')}"
    await alert(db, hour, "pulse_nodes_stalled", message, {"stalled": stalled}, "minor")
