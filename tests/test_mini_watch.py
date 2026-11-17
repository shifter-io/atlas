"""scripts/mini-watch.sh (MONITOR-1, #1094), the browser mini's own watchdog, against fake
launchctl, sysctl, df, id and curl on PATH, a fake clock and a fake home. It runs under the Linux
bash of CI; the script keeps to bash 3.2 (macOS) and tries GNU date before BSD date."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mini-watch.sh"
TOKEN = "T0KEN-not-a-real-hook-mini-51c2"
WEBHOOK = f"https://hooks.example.invalid/services/{TOKEN}"
T0 = 1_800_000_000  # 2027-01-15 08:00 UTC
LABEL = "atlas.browser-node"
HEAD = "<!everyone> Atlas mini browser-2: "
BASH = os.environ.get("MINI_WATCH_BASH", "bash")  # a bash 3.2 build checks the macOS shell

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="GNU date and tools")

FAKE = r"""
import json, os, sys
from pathlib import Path
fake = Path(os.environ["FAKE"])
tool, args = Path(sys.argv[0]).name, sys.argv[1:]
with open(fake / "argv.jsonl", "a") as log:
    log.write(json.dumps([tool, *args]) + "\n")

def text(name, default=""):
    p = fake / name
    return p.read_text() if p.exists() else default

if tool == "launchctl":
    state, domain = text("service", "running"), text("domain", "gui")
    if state == "missing" or not args[1].startswith(domain + "/"):
        print(f"Could not find service in domain for port", file=sys.stderr)
        sys.exit(113)
    print(f"{args[1]} = {{\n\tactive count = 1\n\tstate = {state}\n}}")
elif tool == "sysctl":
    print(text("loadavg", "{ 1.00 1.00 1.00 }") if args[-1] == "vm.loadavg" else 4)
elif tool == "df":
    print("Filesystem 1024-blocks Used Available Capacity Mounted")
    print(f"/dev/disk1 100 50 50 {text('disk', '40')}% /")
elif tool == "id":
    print(501)
elif tool == "curl":
    (fake / "curl-config").write_text(sys.stdin.read())
    if (fake / "slack-down").exists():
        sys.exit(22)
    with open(fake / "posts.jsonl", "a") as posts:
        posts.write(args[args.index("--data-binary") + 1] + "\n")
"""


def iso(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


class Mini:
    def __init__(self, tmp: Path) -> None:
        self.home, self.fake, self.bin = tmp / "home", tmp / "fake", tmp / "bin"
        self.node = tmp / "node-state"
        for d in (self.home, self.fake, self.bin, self.node / "logs"):
            d.mkdir(parents=True)
        for tool in ("launchctl", "sysctl", "df", "id", "curl"):
            path = self.bin / tool
            path.write_text(f"#!{sys.executable}\n{FAKE}")
            path.chmod(0o755)
        webhook = self.home / ".config" / "atlas" / "slack-webhook"
        webhook.parent.mkdir(parents=True)
        webhook.write_text(WEBHOOK + "\n")
        webhook.chmod(0o600)
        self.now = T0
        self.heartbeat_hour = "off"
        self.gap = "600"  # once() moves the clock by hours: the queue test sets the real gap
        self.log(run_at=T0 - 600, pending=0)

    def log(self, run_at: int | None, pending: int, rotated: bool = False) -> None:
        """The node's log: a run line finished at run_at and an hour line with pending bundles."""
        lines = [{"at": iso(T0 - 3600), "kind": "run", "deadline": iso(T0)}]
        if run_at is not None:
            lines.append(
                {"at": iso(run_at), "batch": 7, "deadline": iso(run_at + 60), "kind": "run"}
            )
        lines.append({"at": iso(T0 - 300), "kind": "hour", "outbox_pending": pending})
        path = self.node / "logs" / "browser-node.jsonl"
        body = "".join(json.dumps(line, sort_keys=True) + "\n" for line in lines)
        if rotated:
            path.with_name("browser-node.jsonl.1").write_text(body)
            body = ""
        path.write_text(body)

    def set(self, name: str, value: str | None) -> None:
        path = self.fake / name
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(value)

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "FAKE": str(self.fake),
            "ATLAS_ALERTS_NOW": str(self.now),
            "ATLAS_ALERTS_HEARTBEAT_HOUR": self.heartbeat_hour,
        }
        result = subprocess.run(
            [BASH, str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60
        )
        if check:
            assert result.returncode == 0, result.stderr
        return result

    def check(self) -> list[str]:
        before = len(self.posts())
        self.run(
            "check",
            "--host",
            "browser-2",
            "--state",
            str(self.node),
            "--label",
            LABEL,
            "--gap-minutes",
            self.gap,
        )
        return self.posts()[before:]

    def posts(self) -> list[str]:
        path = self.fake / "posts.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["text"] for line in path.read_text().splitlines()]


@pytest.fixture
def mini(tmp_path: Path) -> Mini:
    return Mini(tmp_path)


def once(mini: Mini, service: str, detail: str, heal) -> None:
    assert mini.check() == [f"{HEAD}{service}: check failed: {detail}"]
    mini.now += 3600
    assert mini.check() == []
    heal()
    assert mini.check() == [f"{HEAD}{service}: recovered: ok again"]


def launchctl_calls(mini: Mini) -> list[list[str]]:
    calls = [json.loads(a) for a in (mini.fake / "argv.jsonl").read_text().splitlines()]
    return [call[1:] for call in calls if call[0] == "launchctl"]


def test_a_healthy_mini_posts_nothing(mini: Mini) -> None:
    assert mini.check() == []
    assert launchctl_calls(mini) == [["print", f"gui/501/{LABEL}"]]


def test_the_node_as_a_system_daemon(mini: Mini) -> None:
    """The minis run the node from /Library/LaunchDaemons: the system domain is read too (#1107)."""
    mini.set("domain", "system")
    assert mini.check() == []
    assert launchctl_calls(mini) == [["print", f"gui/501/{LABEL}"], ["print", f"system/{LABEL}"]]
    mini.set("domain", "nowhere")
    once(mini, "browser service", f"{LABEL} is not loaded", lambda: mini.set("domain", "system"))


@pytest.mark.parametrize(
    ("state", "detail"),
    [("missing", f"{LABEL} is not loaded"), ("waiting", f"{LABEL} is loaded but not running")],
)
def test_browser_service_not_running(mini: Mini, state: str, detail: str) -> None:
    mini.set("service", state)
    once(mini, "browser service", detail, lambda: mini.set("service", None))


def test_queue_late_after_two_gaps(mini: Mini) -> None:
    mini.gap = "27"
    mini.log(run_at=T0 - 54 * 60, pending=0)
    assert mini.check() == []  # 2 x 27 minutes: not yet late
    mini.log(run_at=T0 - 55 * 60, pending=0)
    once(
        mini,
        "queue",
        "late: the last run finished 55 min ago (2 gaps: 54 min)",
        lambda: mini.log(run_at=mini.now - 60, pending=0),
    )


def test_the_rotated_log_is_read_and_no_run_is_a_failure(mini: Mini) -> None:
    mini.log(run_at=T0 - 60, pending=0, rotated=True)
    assert mini.check() == []
    (mini.node / "logs" / "browser-node.jsonl.1").unlink()
    assert mini.check() == [f"{HEAD}queue: check failed: no finished run in the node's log"]


def test_uploads_disk_and_load(mini: Mini) -> None:
    mini.log(run_at=T0 - 60, pending=4)
    once(
        mini,
        "uploads",
        "4 run bundles are waiting to upload",
        lambda: mini.log(run_at=mini.now - 60, pending=3),
    )
    mini.set("disk", "86")
    once(mini, "disk", "the disk is 86% full", lambda: mini.set("disk", "85"))
    mini.set("loadavg", "{ 9.00 9.00 8.50 }")
    once(mini, "load", "the 15-minute load is 8.50 on 4 CPUs", lambda: mini.set("loadavg", None))


def test_a_failed_post_is_tried_again(mini: Mini) -> None:
    mini.set("service", "missing")
    mini.set("slack-down", "")
    result = mini.run(
        "check", "--host", "browser-2", "--state", str(mini.node), "--label", LABEL, check=False
    )
    assert result.returncode == 1 and "posting to Slack failed" in result.stderr
    mini.set("slack-down", None)
    assert len(mini.check()) == 1


def test_heartbeat_silence_and_test_post(mini: Mini) -> None:
    mini.heartbeat_hour = "8"
    assert mini.check() == ["Atlas mini browser-2: all good"]
    assert mini.check() == []
    mini.run("silence", "08")  # a leading zero is not octal
    mini.set("disk", "99")
    assert mini.check() == []
    mini.now += 8 * 60
    mini.set("disk", None)
    mini.run("silence", "30")
    mini.set("disk", "99")
    assert mini.check() == []
    mini.now += 1800
    assert mini.check() == [f"{HEAD}disk: check failed: the disk is 99% full"]
    mini.run("test", "--host", "browser-2")
    assert mini.posts()[-1] == "Atlas mini browser-2: watchdog installed"


def test_the_webhook_stays_secret_and_private(mini: Mini) -> None:
    mini.set("service", "missing")
    result = mini.run("check", "--host", "browser-2", "--state", str(mini.node), "--label", LABEL)
    argv = (mini.fake / "argv.jsonl").read_text()
    assert TOKEN not in argv + result.stdout + result.stderr + "".join(mini.posts())
    assert (mini.fake / "curl-config").read_text() == f'url = "{WEBHOOK}"\n'
    (mini.home / ".config" / "atlas" / "slack-webhook").chmod(0o644)
    result = mini.run("test", "--host", "browser-2", check=False)
    assert result.returncode == 1 and "must be mode 600" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("check", "--host", "mini", "--state", "x", "--label", LABEL),
        ("check", "--host", "browser-2"),
        ("check", "--host", "browser-2", "--state", "x", "--label", LABEL, "--gap-minutes", "0"),
        ("silence", "soon"),
        ("nothing",),
    ],
)
def test_bad_arguments(mini: Mini, args: tuple[str, ...]) -> None:
    result = mini.run(*args, check=False)
    assert result.returncode == 1 and result.stderr.startswith("mini-watch: ")


def test_live_router_a_late_queue_that_recovers_within_the_hour_posts_nothing(mini) -> None:
    (mini.home / ".config" / "atlas" / "alert-router").write_text("live\n")
    mini.gap = "27"
    mini.log(run_at=T0 - 55 * 60, pending=0)  # late: 2 gaps of 27 min
    assert mini.check() == []
    mini.now += 30 * 60
    mini.log(run_at=mini.now - 60, pending=0)
    assert mini.check() == []
    mini.log(run_at=mini.now - 61 * 60 - 55 * 60, pending=0)
    mini.check()
    mini.now += 61 * 60
    [text] = mini.check()
    assert text.startswith(HEAD + "queue: check failed: late: ")
