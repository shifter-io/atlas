"""PULSE-STATE-2: what a node's heartbeat says about its host's pressure, from Linux /proc.

- io_pressure: the share of the last 10 s some task waited for I/O (PSI `some avg10`, percent,
  /proc/pressure/io);
- mem_available_mb: the memory available for new work (/proc/meminfo MemAvailable);
- swap_pages: the pages swapped in and out since the previous heartbeat (/proc/vmstat pswpin
  + pswpout; None on the first one).

On 2026-11-15 a pulse droplet starved on disk (I/O pressure 92 to 97% while its node used 12%
of the CPU) stalled for the hour, and only ssh showed why. With these in its heartbeat the node
row shows it, the pulse hour's stall check can move its pulses (pulse_moves,
pulse.stall_io_pressure) and its stall record keeps them. A value the host does not give (no
PSI, not Linux) is None.
"""

from __future__ import annotations

from pathlib import Path

PROC = Path("/proc")


def _fields(path: Path) -> dict[str, str]:
    """The `name value…` lines of a /proc file; {} when it cannot be read."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    return {name.rstrip(":"): rest for name, _, rest in (line.partition(" ") for line in lines)}


class HostHealth:
    """Samples the host's pressure for each heartbeat; keeps the swap counter between them."""

    def __init__(self, proc: Path = PROC) -> None:
        self.proc = proc
        self._swapped: int | None = None

    def sample(self) -> dict[str, float | int | None]:
        return {
            "io_pressure": self._io(),
            "mem_available_mb": self._memory(),
            "swap_pages": self._swap(),
        }

    def _io(self) -> float | None:
        line = _fields(self.proc / "pressure" / "io").get("some", "")
        values = dict(item.split("=", 1) for item in line.split() if "=" in item)
        try:
            return float(values["avg10"])
        except (KeyError, ValueError):
            return None

    def _memory(self) -> int | None:
        value = _fields(self.proc / "meminfo").get("MemAvailable", "").split()
        return int(value[0]) // 1024 if value and value[0].isdigit() else None

    def _swap(self) -> int | None:
        counters = _fields(self.proc / "vmstat")
        if not all(counters.get(key, "").strip().isdigit() for key in ("pswpin", "pswpout")):
            return None
        total = int(counters["pswpin"]) + int(counters["pswpout"])
        before, self._swapped = self._swapped, total
        return None if before is None else max(0, total - before)
