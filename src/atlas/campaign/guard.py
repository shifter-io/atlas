"""The Mac's campaign watch as a launchd agent: install, remove and status.

The agent runs `atlas campaign watch` (CAMP-1d), which only reads the manager's status
and alerts; it never removes or changes any cloud resource. KeepAlive restarts it until it exits
0, which it does once the campaign is finished. Logs go to ~/Library/Logs/atlas-campaign/.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

from .. import legacy_names

LABEL = "io.atlas.campaign-watch"
# The agent installed before the rename (legacy_names): install and remove unload it too.
LEGACY_LABEL = legacy_names.WATCH_LABEL
Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def plist_path(home: Path, label: str = LABEL) -> Path:
    return home / "Library" / "LaunchAgents" / f"{label}.plist"


def _remove_legacy(home: Path, run: Runner) -> bool:
    """Unload the pre-rename agent and remove its plist; True when a plist was there."""
    run(["launchctl", "bootout", f"{_domain()}/{LEGACY_LABEL}"])
    path = plist_path(home, LEGACY_LABEL)
    present = path.exists()
    path.unlink(missing_ok=True)
    return present


def logs_dir(home: Path) -> Path:
    return home / "Library" / "Logs" / "atlas-campaign"


def watch_command(inventory: Path, status_file: Path, traffic_dir: Path) -> list[str]:
    """The installed console script next to this interpreter, with absolute paths only."""
    script = Path(sys.executable).with_name("atlas")
    return [
        str(script),
        "campaign",
        "watch",
        "--inventory",
        str(inventory.resolve()),
        "--status-file",
        str(status_file.resolve()),
        "--traffic-dir",
        str(traffic_dir.resolve()),
    ]


def render(command: list[str], home: Path) -> bytes:
    """The agent's plist: the packaged template with the command and the log paths."""
    template = files("atlas.campaign").joinpath("assets", "launchd-watch.plist")
    agent = plistlib.loads(template.read_bytes())
    logs = logs_dir(home)
    agent.update(
        ProgramArguments=command,
        StandardOutPath=str(logs / "watch.out.log"),
        StandardErrorPath=str(logs / "watch.err.log"),
    )
    return plistlib.dumps(agent)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(command: list[str], home: Path, run: Runner = _run) -> Path:
    """Write the plist and (re)load the agent; returns the plist path."""
    path = plist_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    logs_dir(home).mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(render(command, home))
    path.chmod(0o644)
    _remove_legacy(home, run)  # never two watches
    run(["launchctl", "bootout", f"{_domain()}/{LABEL}"])  # not loaded yet is fine
    loaded = run(["launchctl", "bootstrap", _domain(), str(path)])
    if loaded.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed ({loaded.returncode})")
    return path


def remove(home: Path, run: Runner = _run) -> bool:
    """Unload the agent (and the pre-rename one) and remove its plist; True when a plist was
    there."""
    path = plist_path(home)
    run(["launchctl", "bootout", f"{_domain()}/{LABEL}"])
    present = path.exists()
    path.unlink(missing_ok=True)
    return _remove_legacy(home, run) or present


def status(home: Path, run: Runner = _run) -> dict[str, object]:
    found = run(["launchctl", "print", f"{_domain()}/{LABEL}"])
    fields = dict(
        line.strip().split(" = ", 1)
        for line in found.stdout.splitlines()
        if line.startswith("\t") and line.count("\t") == 1 and " = " in line
    )
    return {
        "installed": plist_path(home).exists(),
        "loaded": found.returncode == 0,
        "state": fields.get("state"),
        "last_exit_code": fields.get("last exit code"),
    }
