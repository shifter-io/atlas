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
the hour ends (pulse_moves.acknowledge_runs).

The stalled droplet's own evidence is saved (right after its pulses moved to a sibling, before
the move when its replacement removes it first; PULSE-STATE-2) to
`<hour root>/stalled/<name>.log` (mode 600, at most LOG_BYTES; `log` in its entry): the node's
and the DB-IP updater's journal of the last 40 minutes, memory, load, the timers, kernel OOM
lines and the open connections, with every URL masked (DIAGNOSE). The stall of the last-armed
fra1 droplet in the 11:00 to 13:00 hours of 2026-11-15 had no logs left once it was removed;
these say why the next one stops. A droplet that does not answer in DIAGNOSE_SECONDS is noted as
`log_error` and its pulses move all the same.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..orchestrator import pulse_moves
from ..orchestrator.pulse_hour import Armed
from . import pulse_fleet, pulse_replace
from .pulse_alerts import alert, names, title

LOG_BYTES, DIAGNOSE_SECONDS = 400_000, 60
DIAGNOSE = (
    "{ journalctl -u atlas-pulse-node -u atlas-dbip-update --since -40min --no-pager -o short-iso"
    " | tail -n 3000; echo '--- free'; free -m; echo '--- uptime'; uptime;"
    " echo '--- timers'; systemctl list-timers --all --no-pager;"
    " echo '--- oom'; dmesg -T 2>/dev/null | grep -iE 'out of memory|killed process' | tail -n 20;"
    " echo '--- tcp'; ss -tn state established | wc -l; } 2>&1"
    " | sed -E 's#[a-z]+://[^ ]+#<url>#g' | tail -c " + str(LOG_BYTES)
)


async def diagnose(hour_root: Path, name: str, s) -> dict:
    """Save the stalled droplet's evidence (DIAGNOSE) to `stalled/<name>.log`; its entry."""
    record = pulse_fleet.load_nodes(hour_root).get(name)
    try:
        droplet = await s.cloud.get(record.droplet_id) if record and record.droplet_id else None
        if droplet is None:
            return {"log_error": "no droplet"}
        command = ["sh", "-c", DIAGNOSE]
        out = await s.remote.run(droplet, command, "stall log", seconds=DIAGNOSE_SECONDS)
    except Exception as error:  # boundary: the evidence never holds the move back
        return {"log_error": type(error).__name__}
    folder = hour_root / "stalled"
    folder.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(folder / f"{name}.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as sink:
        sink.write(out[-LOG_BYTES:])
    return {"log": f"stalled/{name}.log"}


def _key(cell: Armed) -> tuple[str, str, str]:
    return (cell.provider, cell.product, cell.region)


def helper(
    name: str, armed: dict[str, Armed], skip: set[str], taken: dict[str, int] | None = None
) -> str | None:
    """The armed node of the same network in another region, still up, with the least work:
    its own countries plus the runs already moved to it (`taken`, two per country), then by
    name; None when there is none."""
    cell, taken = armed[name], taken or {}
    options = [
        (len(other.countries) + taken.get(other_name, 0) / 2, other_name)
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
        taken = {e["to_node"]: e["moved"] for e in self.entries if e.get("to_node")}
        to = helper(name, armed, skip, taken)
        if to is None:  # its replacement removes the droplet: read it first
            entry |= await diagnose(self.hour_root, name, s)
            [replaced] = await pulse_replace.replace_once(self.hour_root, self.config, [name], s)
            entry["to"] = "replaced"
            target = _key(armed[name]) if replaced["armed"] else None
        else:
            entry["to"], entry["to_node"], target = self.labels[to], to, _key(armed[to])
        now = datetime.fromtimestamp(s.clock.now(), UTC)
        entry["found_at"] = found.isoformat()
        entry |= await pulse_moves.move_runs(s.db, self.hour, row["node_id"], target, now)
        if to is not None:  # still up: its pulses already restarted elsewhere
            entry |= await diagnose(self.hour_root, name, s)
        return entry

    def summary(self) -> list[dict]:
        """The `stalled` entries of pulse.json."""
        hidden = ("name", "to_node")
        return [{k: v for k, v in entry.items() if k not in hidden} for entry in self.entries]

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
