"""The Mac watch (launchd): manager status, alerts and traffic reports into the status file.

Alert-only: it reads over SSH, copies each new hourly traffic report, rewrites the status-file
header atomically and shows a macOS notification per new report or high alert. It never deletes
anything and writes no IP address.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

STALE = timedelta(minutes=3)
TITLE = "Proxy Index campaign"
# Runs on the manager: status.json, alerts after a byte offset, new reports and traffic.tsv.
REMOTE_READ = r"""
import json, pathlib, sys
root, offset, seen = pathlib.Path(sys.argv[1]), int(sys.argv[2]), set(sys.argv[3].split(","))
def text(path): return path.read_text() if path.exists() else None
alerts = root / "alerts.jsonl"
data = alerts.read_bytes() if alerts.exists() else b""
reports = sorted((root / "reports" / "traffic").glob("hour-*.md"))
print(json.dumps({"status": json.loads(text(root / "status.json") or "null"),
    "alerts": data[offset:].decode().splitlines(), "offset": len(data),
    "reports": {p.name: p.read_text() for p in reports if p.name not in seen},
    "tsv": text(root / "reports" / "traffic.tsv") or ""}))
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def ssh_fetch(inventory: dict, offset: int, seen: list[str]) -> dict:
    """One SSH call to the manager with the Mac key (key-only, known host required)."""
    root = inventory.get("campaign_root") or (
        f"/opt/proxy-index-campaign/campaigns/{inventory['campaign_id']}")
    command = ["ssh", "-i", inventory["identity"], "-o", "BatchMode=yes",
               "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10",
               "-o", f"UserKnownHostsFile={inventory['known_hosts']}",
               "-o", "StrictHostKeyChecking=yes", f"root@{inventory['host']}",
               shlex.join(["python3", "-c", REMOTE_READ, root, str(offset),
                           ",".join(seen) or "-"])]
    reply = subprocess.run(command, capture_output=True, text=True, timeout=60, check=True)
    return json.loads(reply.stdout)


def notify(text: str) -> None:
    """A macOS notification; the text goes in argv, never into the AppleScript source."""
    subprocess.run(["osascript", "-e", "on run argv", "-e",
                    f'display notification (item 1 of argv) with title "{TITLE}"',
                    "-e", "end run", text], capture_output=True, timeout=30, check=False)


@dataclass
class WatchIO:
    fetch: Callable[[dict, int, list[str]], dict] = field(default_factory=lambda: ssh_fetch)
    notify: Callable[[str], None] = field(default_factory=lambda: notify)
    clock: Callable[[], datetime] = utc_now
    sleep: Callable[[float], None] = time.sleep


def write_text(path: Path, text: str) -> None:
    """Atomic replace in the same directory (the status file is watched by other tools)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def hour_totals(tsv: str) -> dict[str, dict[str, int]]:
    """traffic.tsv -> hour -> network -> bytes (up + down)."""
    rows = [line.split("\t") for line in tsv.splitlines()[1:] if line.strip()]
    totals: dict[str, dict[str, int]] = defaultdict(dict)
    for row in rows:
        totals[row[0]][row[1]] = int(row[2]) + int(row[3])
    return totals


def report_message(name: str, totals: dict[str, dict[str, int]]) -> str:
    hour = name.removeprefix("hour-").removesuffix(".md")
    networks = totals.get(hour, {})
    top = max(networks, key=networks.__getitem__) if networks else None
    text = f"CAMP-1 hour {hour}: {sum(networks.values()) / 1e9:.2f} GB"
    return text + (f"; top network {top} {networks[top] / 1e9:.2f} GB" if top else "")


def header(state: str, now: datetime, status: dict | None, latest: str, high: int) -> str:
    hours = (status or {}).get("hours", [])
    counts = defaultdict(int)
    for row in hours:
        counts[row["state"]] += 1
    summary = ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
    return "\n".join([
        f"State: {state}", f"Updated UTC: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"Hours: {summary or 'unknown'} (of {len(hours) or '?'})",
        f"Live workers: {(status or {}).get('live_workers', 'unknown')}",
        f"Traffic-Latest: {latest or 'none'}",
        f"Alerts: {high} high"]) + "\n"


class Watch:
    def __init__(self, inventory: dict, status_file: Path, traffic_dir: Path, io: WatchIO):
        self.inventory, self.status_file, self.traffic_dir, self.io = (
            inventory, status_file, traffic_dir, io)
        self.state_file = status_file.with_name(f".{status_file.stem}.watch.json")
        self.memory = (json.loads(self.state_file.read_text()) if self.state_file.exists()
                       else {"offset": 0, "seen": [], "high": 0, "latest": "", "status": None,
                             "stale": False})

    def once(self) -> str:
        memory, now = self.memory, self.io.clock()
        try:
            data = self.io.fetch(self.inventory, memory["offset"], memory["seen"])
        except (OSError, ValueError, subprocess.SubprocessError):
            data = None
        if data is not None:
            self._absorb(data)
        status = memory["status"]
        stale = status is None or now - datetime.fromisoformat(status["now"]) > STALE
        if stale and not memory["stale"]:
            self.io.notify("CAMP-1 alert manager_stale: status.json older than 3 min")
        memory["stale"] = stale
        state = ("finished" if status and status.get("finished") and not stale
                 else "blocked" if stale or memory["high"] else "active")
        write_text(self.status_file, header(state, now, status, memory["latest"], memory["high"])
                   + self._body())
        write_text(self.state_file, json.dumps(memory, indent=2) + "\n")
        return state

    def _absorb(self, data: dict) -> None:
        memory = self.memory
        totals = hour_totals(data["tsv"])
        for name, text in sorted(data["reports"].items()):
            write_text(self.traffic_dir / name, text)
            memory["seen"].append(name)
            hour = name.removeprefix("hour-").removesuffix(".md")
            memory["latest"] = f"hour {hour} {self.traffic_dir / name}"
            self.io.notify(report_message(name, totals))
        for line in data["alerts"]:
            item = json.loads(line)
            if item.get("level") == "high":
                memory["high"] += 1
                self.io.notify(f"CAMP-1 alert {item.get('code')} ({item.get('key')})")
        memory["offset"], memory["status"] = data["offset"], data["status"]

    def _body(self) -> str:
        """Keep whatever follows the header (notes others add below the first blank line)."""
        if not self.status_file.exists():
            return ""
        _, _, rest = self.status_file.read_text().partition("\n\n")
        return f"\n{rest}" if rest else ""


def run_watch(inventory: Path, status_file: Path, traffic_dir: Path, *, interval: float = 60,
              io: WatchIO | None = None, iterations: int | None = None) -> str:
    """Poll until the campaign is finished (or `iterations` passes); returns the last state."""
    watch = Watch(json.loads(Path(inventory).read_text()), Path(status_file), Path(traffic_dir),
                  io or WatchIO())
    count = 0
    while True:
        state = watch.once()
        count += 1
        if state == "finished" or (iterations is not None and count >= iterations):
            return state
        watch.io.sleep(interval)


def _watch_command(args) -> int:
    state = run_watch(Path(args.inventory), Path(args.status_file), Path(args.traffic_dir),
                      interval=args.interval, iterations=1 if args.once else None)
    print(json.dumps({"state": state}))
    return 0


def register(subcommands, handlers: dict) -> None:
    watch = subcommands.add_parser("watch", help="Mac watch: status file, reports, notifications")
    watch.add_argument("--inventory", required=True)
    watch.add_argument("--status-file", required=True)
    watch.add_argument("--traffic-dir", required=True)
    watch.add_argument("--interval", type=float, default=60.0)
    watch.add_argument("--once", action="store_true")
    handlers.update(watch=_watch_command)
