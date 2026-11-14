"""The fake Canada server of tests/test_server_alerts.py and tests/test_server_watch.py: fake
systemctl, curl, journalctl, psql, openssl, df, hostname, id and nproc on PATH, a fake /proc
(ATLAS_ALERTS_PROC), a fake clock (ATLAS_ALERTS_NOW) and a fake home. By default every check of
scripts/server-alerts/checks.sh finds a healthy world; a test changes one fact at a time."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "server-alerts.sh"
TOKEN = "T0KEN-not-a-real-hook-7f3a"
WEBHOOK = f"https://hooks.example.invalid/services/{TOKEN}"
T0 = 1_800_000_000

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
        out[f.name] = dict(line.split("=", 1) for line in f.read_text().splitlines())
    return out

def text(name, default=""):
    p = fake / name
    return p.read_text() if p.exists() else default

if tool == "systemctl":
    flags = ("--user", "--quiet", "--no-legend", "--no-pager", "--plain", "--all")
    args = [a for a in args if a not in flags]
    u = units()
    if args[0] == "--failed":
        sys.stdout.write(text("failed-system"))
    elif args[0] == "list-units":
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
    elif args[0] == "is-enabled":
        sys.exit(0 if args[1] in u and u[args[1]]["UnitFileState"] == "enabled" else 1)
    elif args[0] == "is-failed":
        print(u.get(args[1], {}).get("ActiveState", "inactive"))
elif tool == "journalctl":
    unit = args[args.index("-u") + 1]
    if (fake / "journal-bytes").exists():
        sys.stdout.buffer.write(b"bad \xff byte\n")
    else:
        print(text("journal") if "--since" in args else f"last line of {unit} `x`")
elif tool == "curl":
    if "-K" in args:
        (fake / "curl-config").write_text(sys.stdin.read())
        if (fake / "slack-down").exists():
            sys.exit(22)
        with open(fake / "posts.jsonl", "a") as posts:
            posts.write(args[args.index("--data-binary") + 1] + "\n")
    elif args[-1].startswith("https://"):
        path = args[-1].split("/", 3)[3]
        if (fake / f"public-down-{path.replace('/', '-')}").exists():
            sys.exit(22)
        ok = '{"revision": "r", "builtAt": "b", "contract": "c"}'
        ok = '{"status": "ok"}' if path == "healthz" else ok
        print(text(f"public-{path.replace('/', '-')}", ok))
    else:
        port = args[-1].split(":")[2].split("/")[0]
        if not (fake / f"healthz-{port}").exists():
            sys.exit(7)
        print(text(f"healthz-{port}"))
elif tool == "openssl" and args[0] == "x509":
    print("notAfter=" + text("cert-end", "Jan  1 00:00:00 2030 GMT"))
elif tool == "openssl" and ":443" in args[args.index("-connect") + 1]:
    print("CONNECTION ESTABLISHED")
elif tool == "openssl":
    port = args[args.index("-connect") + 1].split(":")[1]
    print("CONNECTION ESTABLISHED" if (fake / f"tls-{port}").exists() else "connect error")
elif tool == "psql":
    with open(fake / "psql-env.jsonl", "a") as env:
        pg = {k: os.environ.get(k) for k in ("PGPASSWORD", "PGOPTIONS", "PGHOST", "PGUSER")}
        env.write(json.dumps(pg | {"query": args[args.index("-c") + 1]}) + "\n")
    query = args[args.index("-c") + 1]
    if (fake / "db-down").exists():
        sys.exit(2)
    jobs = "".join(f"{j}\t60\tok\t\n" for j in
        ("calculate", "estimates_release", "browser_plan", "raw_upload", "dbip_update"))
    if query == "SELECT 1":
        print(1)
    elif "max(alert_id)" in query:
        print(text("alert-max", "0"))
    elif "FROM job_runs" in query:
        sys.stdout.write(text("jobs", jobs))
    elif "FROM test_runs" in query:
        print(text("ingest-age", "60"))
    elif "current_calculation" in query:
        print(text("calc-age", "60"))
    elif "pg_stat_activity" in query:
        print(text("connections", "1\t20"))
    elif "FROM nodes" in query:
        sys.stdout.write(text("minis", ""))
    else:
        sys.stdout.write(text("alert-rows"))
elif tool == "df":
    print("Filesystem 1024-blocks Used Available Capacity Mounted")
    print(f"/dev/x 100 50 50 {text('disk', '40')}% /home")
elif tool == "hostname":
    print("canada-test")
elif tool == "id":
    print(1000)
elif tool == "nproc":
    print(4)
"""


class Server:
    def __init__(self, tmp: Path, *, fake_curl: bool = True) -> None:
        self.home = tmp / "home"
        self.fake = tmp / "fake"
        self.bin = tmp / "bin"
        self.proc = tmp / "proc"
        for d in (self.home, self.fake / "units", self.bin, self.proc):
            d.mkdir(parents=True)
        self.memory(4_000_000, 2_000_000)
        self.load("0.50")
        tools = ["systemctl", "journalctl", "openssl", "psql", "df", "hostname", "id", "nproc"]
        for tool in tools + (["curl"] if fake_curl else []):
            path = self.bin / tool
            path.write_text(f"#!{sys.executable}\n{FAKE}")
            path.chmod(0o755)
        self.now = T0
        for caddy in ("healthz-8080", "healthz-9080"):  # Caddy, in front of the serving colour
            self.set(caddy, '{"status": "ok"}')
        self.heartbeat_hour = "off"
        self.webhook(WEBHOOK)

    def webhook(self, url: str, mode: int = 0o600) -> None:
        path = self.home / ".config" / "atlas" / "slack-webhook"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(url + "\n")
        path.chmod(mode)

    def memory(self, total_kb: int, available_kb: int) -> None:
        meminfo = f"MemTotal: {total_kb} kB\nMemFree: 1 kB\nMemAvailable: {available_kb} kB\n"
        (self.proc / "meminfo").write_text(meminfo)

    def load(self, fifteen: str) -> None:
        (self.proc / "loadavg").write_text(f"0.10 0.20 {fifteen} 1/100 1234\n")

    def unit(
        self,
        name: str,
        active="active",
        filestate="enabled",
        restarts=0,
        result="success",
        **more: str,
    ):
        props = dict(ActiveState=active, UnitFileState=filestate, NRestarts=restarts, Result=result)
        props |= more
        (self.fake / "units" / name).write_text("".join(f"{k}={v}\n" for k, v in props.items()))

    def set(self, name: str, value: str | None) -> None:
        path = self.fake / name
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(value)

    def database(self, env: str = "production") -> None:
        """The API's read-only login (database-api.toml) and the manager's (database.toml)."""
        for name, user in (("database-api.toml", "api"), ("database.toml", "manager")):
            path = self.home / ".config" / "atlas" / env / name
            path.parent.mkdir(parents=True, exist_ok=True)
            fields = f'host = "127.0.0.1"\nport = "5432"\ndbname = "d"\nuser = "{user}"\n'
            path.write_text(f'[postgres]\n{fields}password = "pw-x"\n')

    def env(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "FAKE": str(self.fake),
            "ATLAS_ALERTS_NOW": str(self.now),
            "ATLAS_PYTHON": sys.executable,
            "ATLAS_ALERTS_PROC": str(self.proc),
            "ATLAS_ALERTS_HEARTBEAT_HOUR": self.heartbeat_hour,
        }

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = self.env()
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
