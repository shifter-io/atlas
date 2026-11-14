"""Worker host sampler: CPU (total, per core, steal), load, memory and the round's RSS.

Runs on the worker beside the round, in its own unit:
`python -m atlas.campaign.sampler --interval 1 --output <round dir>/host-samples.jsonl`.
It appends one JSON line per interval and stops when the round unit has no process left.
`summarize` turns the file into the `DropletRecord` CPU and memory fields.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path

ROUND_CGROUP = "/sys/fs/cgroup/system.slice/atlas-camp-round.service"
Ticks = dict[str, tuple[int, int, int]]  # cpu name -> (total, idle, steal)


def cpu_ticks(stat_text: str) -> Ticks:
    """Parse `/proc/stat`: the `cpu` total line and every `cpuN` line."""
    found = {}
    for line in stat_text.splitlines():
        fields = line.split()
        if fields and fields[0].startswith("cpu"):
            values = [int(v) for v in fields[1:9]] + [0] * (8 - len(fields[1:9]))
            found[fields[0]] = (sum(values), values[3] + values[4], values[7])
    return found


def busy(now: tuple[int, int, int], before: tuple[int, int, int]) -> tuple[float, float]:
    """(busy %, steal %) between two readings of one cpu line."""
    total = now[0] - before[0]
    if total <= 0:
        return 0.0, 0.0
    return 100 * (total - (now[1] - before[1])) / total, 100 * (now[2] - before[2]) / total


def memory_used_pct(meminfo_text: str) -> float:
    values = {
        line.split(":")[0]: int(line.split()[1])
        for line in meminfo_text.splitlines()
        if ":" in line and len(line.split()) > 1
    }
    return 100 * (1 - values["MemAvailable"] / values["MemTotal"])


def round_rss_mb(proc: Path, cgroup: Path) -> float | None:
    """Resident memory of every process in the round unit; None when the unit has none."""
    try:
        pids = (cgroup / "cgroup.procs").read_text().split()
    except OSError:
        return None
    pages = 0
    for pid in pids:
        try:
            pages += int((proc / pid / "statm").read_text().split()[1])
        except (OSError, IndexError, ValueError):
            continue
    return pages * os.sysconf("SC_PAGE_SIZE") / 1024**2 if pids else None


def sample(proc: Path, cgroup: Path, before: Ticks) -> tuple[dict | None, Ticks]:
    """One sample against the previous ticks; the line is None when the round has ended."""
    ticks = cpu_ticks((proc / "stat").read_text())
    rss = round_rss_mb(proc, cgroup)
    if rss is None:
        return None, ticks
    line: dict = {
        "time": time.time(),
        "rss_mb": round(rss, 1),
        "load1": float((proc / "loadavg").read_text().split()[0]),
        "mem_used_pct": round(memory_used_pct((proc / "meminfo").read_text()), 1),
    }
    if "cpu" in before and "cpu" in ticks:
        line["cpu_pct"], line["steal_pct"] = (
            round(v, 1) for v in busy(ticks["cpu"], before["cpu"])
        )
        cores = [busy(ticks[n], before[n])[0] for n in ticks if n != "cpu" and n in before]
        line["core_max_pct"] = round(max(cores, default=0.0), 1)
    return line, ticks


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def summarize(path: Path, since: float | None = None) -> dict[str, float | None]:
    """The record's CPU fields from a samples file; missing or broken lines count as absent.
    Samples before `since` (the round's first start; the idle lead-in) are ignored."""
    lines = []
    try:
        for text in path.read_text().splitlines():
            try:
                lines.append(json.loads(text))
            except ValueError:
                continue
    except OSError:
        pass

    def values(key):
        return [
            float(item[key])
            for item in lines
            if isinstance(item.get(key), (int, float))
            and (since is None or item.get("time", since) >= since)
        ]

    cpu = values("cpu_pct")
    return {
        "cpu_p50_pct": percentile(cpu, 0.5),
        "cpu_p95_pct": percentile(cpu, 0.95),
        "cpu_max_pct": max(cpu, default=None),
        "cpu_peak_pct": max(cpu, default=None),
        "cpu_core_max_pct": max(values("core_max_pct"), default=None),
        "steal_p95_pct": percentile(values("steal_pct"), 0.95),
        "load1_p95": percentile(values("load1"), 0.95),
        "mem_used_max_pct": max(values("mem_used_pct"), default=None),
        "rss_peak_mb": max(values("rss_mb"), default=None),
    }


def main(argv: list[str] | None = None, *, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(prog="atlas.campaign.sampler")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--proc", type=Path, default=Path("/proc"))
    parser.add_argument("--cgroup", type=Path, default=Path(ROUND_CGROUP))
    args = parser.parse_args(argv)
    ticks: Ticks = {}
    with args.output.open("a") as stream:
        while True:
            line, ticks = sample(args.proc, args.cgroup, ticks)
            if line is None:
                return 0
            stream.write(json.dumps(line, allow_nan=False) + "\n")
            stream.flush()
            sleep(args.interval)


if __name__ == "__main__":  # pragma: no cover - the worker's entry point
    raise SystemExit(main())
