#!/usr/bin/env python3
"""Start/status/stop the loopback measurement API; no collection or schedules."""

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".state/measurements-api"
PID = STATE / "pid"


def alive():
    if not PID.exists():
        return None
    pid = int(PID.read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    return pid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status"])
    args = parser.parse_args()
    pid = alive()
    if args.action == "stop" and pid:
        os.kill(pid, signal.SIGTERM)
        PID.unlink(missing_ok=True)
        pid = None
    elif args.action == "start" and not pid:
        STATE.mkdir(parents=True, exist_ok=True)
        with (STATE / "server.log").open("ab") as output:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "from proxy_index.cli import main; raise SystemExit(main())",
                    "serve",
                    "--database-file",
                    ".secrets/database.toml",
                ],
                cwd=ROOT,
                env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
                stdout=output,
                stderr=output,
                start_new_session=True,
            )
        pid = child.pid
        PID.write_text(str(pid))
    print(
        json.dumps(
            {
                "running": bool(pid),
                "pid": pid,
                "url": "http://127.0.0.1:8081",
                "log": str(STATE / "server.log"),
            }
        )
    )


if __name__ == "__main__":
    main()
