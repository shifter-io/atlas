"""Manage only this project's PostgreSQL cluster; never touch another data directory."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql

from proxy_index.providers.credentials import read_credentials

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".state" / "postgres"
DATA = STATE / "data"
PRIVATE = ROOT / ".secrets"
# A throwaway test cluster (CI) need not survive a crash; the local and production
# clusters never get these.
EPHEMERAL = "fsync = off\nsynchronous_commit = off\nfull_page_writes = off\n"


def save_private(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write("[postgres]\n")
        for key, value in data.items():
            stream.write(f"{key} = {json.dumps(str(value))}\n")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init", "start", "stop", "status"))
    parser.add_argument("--bin-dir", type=Path)
    parser.add_argument("--port", type=int, default=55432)
    parser.add_argument(
        "--ephemeral",
        action="store_true",
        help="a new cluster skips crash safety (fsync, synchronous_commit, full_page_writes); "
        "only for a throwaway test cluster",
    )
    return parser.parse_args()


def load_runtime(args):
    """Return the saved runtime and binary directory, recording them on the first init."""
    runtime_path = STATE / "runtime.json"
    if runtime_path.exists():
        runtime = json.loads(runtime_path.read_text())
        return runtime, Path(runtime["bin_dir"])
    if args.command != "init":
        raise ValueError("Initialize the project cluster first")
    binary = args.bin_dir or Path(shutil.which("postgres") or "").parent
    binary = binary.resolve()
    subprocess.run([str(binary / "postgres"), "--version"], check=True)
    STATE.mkdir(parents=True, mode=0o700)
    PRIVATE.mkdir(exist_ok=True, mode=0o700)
    PRIVATE.chmod(0o700)
    runtime = {"bin_dir": str(binary), "port": args.port}
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")
    return runtime, binary


def load_credentials(runtime):
    """Return the admin and app credentials, generating any that are missing."""
    admin_path = PRIVATE / "postgres-admin.toml"
    app_path = PRIVATE / "database.toml"
    base = {"host": "127.0.0.1", "port": runtime["port"]}
    if not admin_path.exists():
        save_private(
            admin_path,
            base
            | {"dbname": "postgres", "user": "proxy_admin", "password": secrets.token_urlsafe(32)},
        )
    if not app_path.exists():
        save_private(
            app_path,
            base
            | {
                "dbname": "proxy_reviews",
                "user": "proxy_reviews",
                "password": secrets.token_urlsafe(32),
            },
        )
    return read_credentials(admin_path, "postgres"), read_credentials(app_path, "postgres")


def init_cluster(binary, admin, runtime, *, ephemeral=False):
    """Create the data directory and point the server at its address and socket."""
    password_path = PRIVATE / ".initdb-password"
    try:
        with os.fdopen(
            os.open(password_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as stream:
            stream.write(admin["password"] + "\n")
        subprocess.run(
            [
                str(binary / "initdb"),
                "-D",
                str(DATA),
                "-U",
                admin["user"],
                "--auth=scram-sha-256",
                "--encoding=UTF8",
                "--locale=C",
                "--data-checksums",
                f"--pwfile={password_path}",
            ],
            check=True,
        )
    finally:
        password_path.unlink(missing_ok=True)
    sockets = STATE / "sockets"
    sockets.mkdir(mode=0o700, exist_ok=True)
    socket_value = str(sockets).replace("'", "''")
    with (DATA / "postgresql.conf").open("a") as stream:
        stream.write(
            f"\nlisten_addresses = '127.0.0.1'\nport = {runtime['port']}\n"
            f"unix_socket_directories = '{socket_value}'\n"
            "password_encryption = 'scram-sha-256'\n" + (EPHEMERAL if ephemeral else "")
        )


def ensure_database(admin, app):
    """Create the app role and its database when they do not exist yet."""
    with psycopg.connect(**admin, autocommit=True, connect_timeout=5) as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (app["user"],)).fetchone():
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(app["user"]), sql.Literal(app["password"])
                )
            )
        if not conn.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (app["dbname"],)
        ).fetchone():
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(app["dbname"]), sql.Identifier(app["user"])
                )
            )


def main():
    args = parse_args()
    runtime, binary = load_runtime(args)
    ctl = [str(binary / "pg_ctl"), "-D", str(DATA)]
    if args.command in ("status", "stop"):
        return subprocess.run(
            ctl + (["status"] if args.command == "status" else ["stop", "-m", "fast", "-w"])
        ).returncode
    admin, app = load_credentials(runtime)
    if not (DATA / "PG_VERSION").exists():
        if args.command != "init":
            raise ValueError("Initialize the project cluster first")
        init_cluster(binary, admin, runtime, ephemeral=args.ephemeral)
    running = subprocess.run(ctl + ["status"], capture_output=True).returncode == 0
    if not running:
        subprocess.run(ctl + ["start", "-l", str(STATE / "server.log"), "-w"], check=True)
    ensure_database(admin, app)
    print(f"Project PostgreSQL ready: 127.0.0.1:{runtime['port']}/{app['dbname']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        raise SystemExit(
            f"PostgreSQL operation failed ({type(exc).__name__}); "
            "check the local server log and configuration"
        ) from None
