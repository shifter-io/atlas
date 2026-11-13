"""scripts/server-alerts.sh (#1007) against fake systemctl, curl, journalctl, psql, openssl, df
and hostname on PATH, with a fake clock (ATLAS_ALERTS_NOW) and a fake home."""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "server-alerts.sh"
TOKEN = "T0KEN-not-a-real-hook-7f3a"
WEBHOOK = f"https://hooks.example.invalid/services/{TOKEN}"
T0 = 1_800_000_000

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="GNU tools (stat -c, flock)")

# One fake for every tool: argv[0] names it; state lives in $FAKE as small files.
FAKE = r"""
import json, os, sys
from pathlib import Path
fake = Path(os.environ["FAKE"])
tool, args = Path(sys.argv[0]).name, sys.argv[1:]
with open(fake / "argv.jsonl", "a") as log:
    log.write(json.dumps([tool, *args]) + "\n")

def units():
    out = {}
    for f in sorted((fake / "units").glob("*")):
        out[f.name] = dict(line.split("=", 1) for line in f.read_text().split())
    return out

def text(name, default=""):
    p = fake / name
    return p.read_text() if p.exists() else default

if tool == "systemctl":
    flags = ("--user", "--quiet", "--no-legend", "--no-pager", "--plain", "--all")
    args = [a for a in args if a not in flags]
    u = units()
    if args[0] == "list-units":
        active = "--state=active" in args
        for name, p in u.items():
            if not active or p["ActiveState"] == "active":
                print(name, "loaded", p["ActiveState"], "x", "description")
    elif args[0] == "list-unit-files":
        for name, p in u.items():
            if p["UnitFileState"] == "enabled":
                print(name, "enabled", "enabled")
    elif args[0] == "show":
        sys.stdout.write("".join(f"{k}={v}\n" for k, v in u[args[1]].items()))
    elif args[0] == "is-active":
        sys.exit(0 if args[1] in u and u[args[1]]["ActiveState"] == "active" else 3)
    elif args[0] == "is-failed":
        print(u.get(args[1], {}).get("ActiveState", "inactive"))
elif tool == "journalctl":
    unit = args[args.index("-u") + 1]
    print(text("journal") if "--since" in args else f"last line of {unit} `x`")
elif tool == "curl":
    if "-K" in args:
        (fake / "curl-config").write_text(sys.stdin.read())
        if (fake / "slack-down").exists():
            sys.exit(22)
        with open(fake / "posts.jsonl", "a") as posts:
            posts.write(args[args.index("--data-binary") + 1] + "\n")
    else:
        port = args[-1].split(":")[2].split("/")[0]
        if not (fake / f"healthz-{port}").exists():
            sys.exit(7)
        print(text(f"healthz-{port}"))
elif tool == "openssl":
    port = args[args.index("-connect") + 1].split(":")[1]
    print("CONNECTION ESTABLISHED" if (fake / f"tls-{port}").exists() else "connect error")
elif tool == "psql":
    with open(fake / "psql-env.jsonl", "a") as env:
        pg = {k: os.environ.get(k) for k in ("PGPASSWORD", "PGOPTIONS", "PGHOST")}
        env.write(json.dumps(pg) + "\n")
    query = args[args.index("-c") + 1]
    if (fake / "db-down").exists():
        sys.exit(2)
    if query == "SELECT 1":
        print(1)
    elif "max(alert_id)" in query:
        print(text("alert-max", "0"))
    else:
        sys.stdout.write(text("alert-rows"))
elif tool == "df":
    print("Filesystem 1024-blocks Used Available Capacity Mounted")
    print(f"/dev/x 100 50 50 {text('disk', '40')}% /home")
elif tool == "hostname":
    print("canada-test")
elif tool == "id":
    print(1000)
"""


class Server:
    def __init__(self, tmp: Path, *, fake_curl: bool = True) -> None:
        self.home = tmp / "home"
        self.fake = tmp / "fake"
        self.bin = tmp / "bin"
        for d in (self.home, self.fake / "units", self.bin):
            d.mkdir(parents=True)
        tools = ["systemctl", "journalctl", "openssl", "psql", "df", "hostname", "id"]
        for tool in tools + (["curl"] if fake_curl else []):
            path = self.bin / tool
            path.write_text(f"#!{sys.executable}\n{FAKE}")
            path.chmod(0o755)
        self.now = T0
        self.webhook(WEBHOOK)

    def webhook(self, url: str, mode: int = 0o600) -> None:
        path = self.home / ".config" / "atlas" / "slack-webhook"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(url + "\n")
        path.chmod(mode)

    def unit(self, name: str, active="active", filestate="enabled", restarts=0, result="success"):
        props = dict(ActiveState=active, UnitFileState=filestate, NRestarts=restarts, Result=result)
        (self.fake / "units" / name).write_text("".join(f"{k}={v}\n" for k, v in props.items()))

    def set(self, name: str, value: str | None) -> None:
        path = self.fake / name
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(value)

    def database(self, env: str = "production") -> None:
        path = self.home / ".config" / "atlas" / env / "database-api.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = 'host = "127.0.0.1"\nport = "5432"\ndbname = "d"\nuser = "u"\npassword = "pw-x"'
        path.write_text(f"[postgres]\n{fields}\n")

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "FAKE": str(self.fake),
            "ATLAS_ALERTS_NOW": str(self.now),
            "ATLAS_PYTHON": sys.executable,
        }
        command = ["bash", str(SCRIPT), *args]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
        if check:
            assert result.returncode == 0, result.stderr
        return result

    def check(self, env: str = "production") -> list[str]:
        """Runs one check; returns the texts posted by it."""
        before = len(self.posts())
        self.run("check", env)
        return self.posts()[before:]

    def posts(self) -> list[str]:
        path = self.fake / "posts.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["text"] for line in path.read_text().splitlines()]

    def argv(self) -> list[list[str]]:
        return [json.loads(line) for line in (self.fake / "argv.jsonl").read_text().splitlines()]


@pytest.fixture
def server(tmp_path: Path) -> Server:
    s = Server(tmp_path)
    (s.home / "atlas" / "production" / "alerts-state").mkdir(parents=True)
    (s.home / "atlas" / "staging" / "alerts-state").mkdir(parents=True)
    return s


HEAD = "<!everyone> Atlas production on canada-test: "
STAGING = "<!everyone> Atlas staging on canada-test: "


def test_restart_is_posted_with_the_journal(server: Server) -> None:
    server.unit("atlas-orchestrator.service", restarts=1)
    server.set("tls-8443", "")
    server.set("tls-8444", "")
    assert server.check() == []  # the first sight is the baseline
    server.unit("atlas-orchestrator.service", restarts=3)
    [text] = server.check()
    detail = "restarted 2 time(s) since the last check"
    assert text.startswith(f"{HEAD}atlas-orchestrator.service: restarted: {detail}\n```\n")
    assert "last line of atlas-orchestrator.service x" in text and text.endswith("\n```")
    assert server.check() == []


def test_could_not_restart_is_limited_and_recovered_is_posted_once(server: Server) -> None:
    server.unit("atlas-api@blue.service", active="failed", result="exit-code")
    [text] = server.check()
    detail = "could not restart: the unit is failed (result: exit-code)"
    assert text.startswith(f"{HEAD}atlas-api@blue.service: {detail}")
    server.now += 599
    assert server.check() == []
    server.now += 1
    assert len(server.check()) == 1
    server.unit("atlas-api@blue.service")
    server.set("healthz-8090", '{"status": "ok"}')
    [text] = server.check()
    assert text.startswith(HEAD + "atlas-api@blue.service: recovered: active again")
    assert server.check() == []


def test_on_failure_posts_could_not_restart_for_the_units_environment(server: Server) -> None:
    server.run("failed", "atlas-staging-orchestrator.service")
    [text] = server.posts()
    assert text.startswith(
        f"{STAGING}atlas-staging-orchestrator.service: could not restart: the unit failed"
    )
    server.unit("atlas-staging-orchestrator.service")
    server.set("tls-9443", "")
    server.set("tls-9444", "")
    [text] = server.check("staging")
    assert "atlas-staging-orchestrator.service: recovered" in text


def test_disk_threshold(server: Server) -> None:
    server.set("disk", "85")
    assert server.check() == []
    server.set("disk", "86")
    assert server.check() == [HEAD + "disk: check failed: the home file system is 86% full"]
    server.set("disk", "50")
    assert server.check() == [HEAD + "disk: recovered: ok again"]


def test_only_enabled_or_active_units_of_the_environment_are_watched(server: Server) -> None:
    server.unit("atlas-manager.target", active="failed", filestate="disabled")  # off until GO
    server.unit("atlas-analyze.service", active="failed", filestate="static")  # from its timer
    server.unit("atlas-staging-api@blue.service", active="failed")  # the other environment
    server.unit("atlas-hourly-20261114T03.service", active="failed", filestate="transient")
    texts = server.check()
    services = [t.split(": ")[1] for t in texts]
    assert services == ["atlas-analyze.service", "atlas-hourly-20261114T03.service"]
    assert "check failed: the hourly test failed" in texts[1]
    server.now += 3600
    assert [t.split(": ")[1] for t in server.check()] == ["atlas-analyze.service"]  # hourly: once


def test_health_checks(server: Server) -> None:
    server.unit("atlas-api@green.service")
    server.unit("atlas-orchestrator.service")
    server.set("healthz-8091", '{"unserved": [{"status": "ok"}], "status": "degraded"}')
    server.set("tls-8443", "")
    server.database()
    server.set("db-down", "")
    texts = server.check()
    assert texts == [
        HEAD + "atlas-api@green /healthz: check failed: /healthz status is degraded",
        HEAD + "manager port 8444: check failed: port 8444 does not accept TLS",
        HEAD + "database: check failed: SELECT 1 failed",
    ]
    server.set("healthz-8091", '{"status":"ok"}')
    server.set("tls-8444", "")
    server.set("db-down", None)
    assert len(server.check()) == 3  # each recovered
    envs = [json.loads(line) for line in (server.fake / "psql-env.jsonl").read_text().splitlines()]
    assert envs[-1]["PGPASSWORD"] == "pw-x"
    assert "default_transaction_read_only=on" in envs[-1]["PGOPTIONS"]
    assert not any("pw-x" in " ".join(argv) for argv in server.argv())


def test_staging_uses_its_own_ports(server: Server) -> None:
    server.unit("atlas-staging-api@blue.service")
    server.unit("atlas-staging-orchestrator.service")
    server.set("tls-9443", "")
    server.set("tls-9444", "")
    assert server.check("staging") == [
        f"{STAGING}atlas-staging-api@blue /healthz: check failed: no answer on port 9090"
    ]


def test_manager_alerts_and_create_failed(server: Server) -> None:
    server.database()
    server.set("alert-max", "5")
    assert server.check() == []  # starts after the newest row
    server.set("alert-rows", "6\tpulse_stalled\tno pulse for 30 minutes\n7\tdisk\tfull\n")
    server.set("journal", "launch: droplet create_failed: quota\n")
    texts = server.check()
    assert [t.split("\n")[0] for t in texts] == [
        HEAD + "manager alert 6 (pulse_stalled): check failed: no pulse for 30 minutes",
        HEAD + "manager alert 7 (disk): check failed: full",
        HEAD + "test machines: check failed: launch: droplet create_failed: quota",
    ]
    seen = server.home / "atlas" / "production" / "alerts-state" / "alert-id"
    assert seen.read_text().strip() == "7"
    query = [a for a in server.argv() if a[0] == "psql"][-1]
    assert "alert_id > 6" not in query[-1] and "alert_id > 5" in query[-1]
    journal = [a for a in server.argv() if a[0] == "journalctl"]
    assert any("--since" in a and f"@{T0 - 120}" in a for a in journal)


def test_a_failed_post_is_retried_on_the_next_check(server: Server) -> None:
    server.set("disk", "99")
    server.set("slack-down", "")
    result = server.run("check", "production", check=False)
    assert result.returncode == 1 and "posting to Slack failed" in result.stderr
    server.set("slack-down", None)
    assert len(server.check()) == 1


def test_the_webhook_never_appears_in_argv_or_output(server: Server) -> None:
    server.set("disk", "99")
    server.unit("atlas-orchestrator.service", active="failed")
    result = server.run("check-all")
    assert len(server.posts()) == 3  # the orchestrator, and the shared disk for each environment
    assert server.fake.joinpath("curl-config").read_text() == f'url = "{WEBHOOK}"\n'
    argv = (server.fake / "argv.jsonl").read_text()
    logs = argv + "".join(server.posts()) + result.stdout + result.stderr
    assert TOKEN not in logs


@pytest.mark.parametrize("mode", [0o644, 0o640])
def test_the_webhook_file_must_be_mode_600(server: Server, mode: int) -> None:
    server.webhook(WEBHOOK, mode)
    result = server.run("install", "production", check=False)
    assert result.returncode == 1 and "must be mode 600" in result.stderr
    assert TOKEN not in result.stderr
    assert server.posts() == []


def test_install_writes_units_and_drop_ins_and_is_idempotent(tmp_path: Path) -> None:
    server = Server(tmp_path)
    server.unit("atlas-api@blue.service")
    server.unit("atlas-orchestrator.service", active="inactive", filestate="enabled")
    server.unit("atlas-manager.target", active="inactive", filestate="disabled")
    server.unit("atlas-caddy.service")
    server.unit("atlas-staging-api@blue.service")
    server.run("install", "production")
    units = server.home / ".config" / "systemd" / "user"
    written = sorted(str(p.relative_to(units)) for p in units.rglob("*") if p.is_file())
    assert written == [
        "atlas-alert@.service",
        "atlas-alerts.service",
        "atlas-alerts.timer",
        "atlas-api@.service.d/atlas-alert.conf",
        "atlas-caddy.service.d/atlas-alert.conf",
        "atlas-orchestrator.service.d/atlas-alert.conf",
    ]
    drop_in = (units / "atlas-caddy.service.d/atlas-alert.conf").read_text()
    assert drop_in == "[Unit]\nOnFailure=atlas-alert@%n.service\n"
    assert (server.home / "atlas" / "bin" / "atlas-alerts").read_bytes() == SCRIPT.read_bytes()
    assert (server.home / "atlas" / "production" / "alerts-state").is_dir()
    assert server.posts() == ["Atlas production: alerts installed on canada-test"]
    calls = [a[1:] for a in server.argv() if a[0] == "systemctl" and "show" not in a]
    assert ["--user", "daemon-reload"] in calls
    assert ["--user", "enable", "--now", "--quiet", "atlas-alerts.timer"] in calls

    times = {p: p.stat().st_mtime_ns for p in units.rglob("*") if p.is_file()}
    (server.fake / "argv.jsonl").unlink()
    server.run("install", "production")
    assert {p: p.stat().st_mtime_ns for p in units.rglob("*") if p.is_file()} == times
    assert ["systemctl", "--user", "daemon-reload"] not in server.argv()
    server.run("test", "production")
    assert server.posts()[-1] == "Atlas production: alerts installed on canada-test"


def test_install_refuses_root(tmp_path: Path) -> None:
    server = Server(tmp_path)
    (server.bin / "id").write_text("#!/bin/sh\necho 0\n")
    result = server.run("install", "staging", check=False)
    assert result.returncode == 1 and "never as root" in result.stderr


def test_usage_and_unknown_environment(server: Server) -> None:
    assert server.run("check", "qa", check=False).returncode == 1
    assert "usage" in server.run("nothing", check=False).stderr


@pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")
def test_a_real_curl_posts_json_to_a_local_receiver(tmp_path: Path) -> None:
    received: list[tuple[str, str, bytes]] = []

    class Receiver(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, self.headers["Content-Type"], body))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args) -> None:
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        server = Server(tmp_path, fake_curl=False)
        os.symlink(shutil.which("curl"), server.bin / "curl")
        server.webhook(f"http://127.0.0.1:{httpd.server_address[1]}/services/{TOKEN}")
        server.run("test", "staging")
    finally:
        httpd.shutdown()
    [(path, ctype, body)] = received
    assert path == f"/services/{TOKEN}" and ctype == "application/json"
    assert json.loads(body) == {"text": "Atlas staging: alerts installed on canada-test"}
