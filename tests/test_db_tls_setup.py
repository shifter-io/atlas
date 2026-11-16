"""SEC-DBTLS-2: scripts/db-tls-setup.sh prints working steps and runs nothing; server-setup.sh
installs the root certificate and gives the remote database files sslmode = "verify-full".
"""

import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import trustme
from test_server_setup import COMMIT, dry_run

from atlas.providers.dbconnect import connect_options

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "db-tls-setup.sh"
AWK = ROOT / "scripts" / "server-setup" / "db-verify-full.awk"
PASSWORD = "not-a-real-password-7"


def steps(*args: str, path: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": path or os.environ["PATH"]}
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=30
    )


def blocks(text: str) -> dict[int, str]:
    """The printed steps by number (`## 1.` ...)."""
    found = re.split(r"^## (\d)\. .*$", text, flags=re.M)
    return {int(n): body for n, body in zip(found[1::2], found[2::2], strict=True)}


def shell(block: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-euc", block], cwd=cwd, capture_output=True, text=True)


def test_prints_steps_and_runs_nothing(tmp_path):
    marker = tmp_path / "ran"
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("openssl", "psql", "scp", "ssh", "install", "chmod", "getent"):
        (tools / name).write_text(f"#!/bin/sh\necho {name} >>{marker}\n")
        (tools / name).chmod(0o755)
    out = steps("db-1.example.test", path=f"{tools}:{os.environ['PATH']}")
    assert out.returncode == 0, out.stderr
    assert not marker.exists()
    assert sorted(blocks(out.stdout)) == [1, 2, 3, 4, 5, 6]
    assert "subjectAltName = DNS:db-1.example.test" in out.stdout


@pytest.mark.parametrize("server", ["10.20.30.40", "fd00::5"])
def test_an_address_is_named_as_ip(server):
    out = steps(server)
    assert out.returncode == 0, out.stderr
    assert f"subjectAltName = IP:{server}" in out.stdout
    assert f"hostaddr={server} " in out.stdout


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("db;rm",),
        ("$(id)",),
        ("-db",),
        ("db", "--ca-dir"),
        ("db", "--ca-dir", "a b"),
        ("db", "--ca-dir", "-rf"),
        ("db", "-x"),
    ],
)
def test_bad_arguments(args):
    out = steps(*args)
    assert out.returncode == 2
    assert "usage: db-tls-setup.sh" in out.stderr
    assert out.stdout == ""


def test_ca_key_stays_on_the_mac_and_settings_are_complete():
    text = steps("db-1.example.test").stdout
    for line in text.splitlines():
        if "scp" in line:
            assert "ca.crt" in line and "ca.key" not in line
    for setting in ("ssl = on", "ssl_cert_file", "ssl_key_file", "ssl_min_protocol_version"):
        assert f"ALTER SYSTEM SET {setting}" in text
    assert "pg_reload_conf()" in text
    assert "sslrootcert=system" in text  # check C2: another CA is refused
    assert "host=atlas-c2.invalid" in text  # check C2: another name is refused


@pytest.mark.parametrize(
    ("server", "flag"), [("db-1.example.test", "-verify_hostname"), ("10.20.30.40", "-verify_ip")]
)
def test_printed_certificate_steps_work(tmp_path, server, flag):
    """Steps 1 to 3 as printed: the CA, the server's own key and request, the signed certificate."""
    ca, data = tmp_path / "ca", tmp_path / "data"
    data.mkdir()
    printed = blocks(steps(server, "--ca-dir", str(ca)).stdout)
    for number, cwd in ((1, tmp_path), (2, data)):
        run = shell(printed[number], cwd)
        assert run.returncode == 0, run.stderr
    csr = next(data.glob("atlas-db-*.csr"))
    (ca / csr.name).write_bytes(csr.read_bytes())
    run = shell(printed[3], tmp_path)
    assert run.returncode == 0, run.stderr
    crt = ca / csr.with_suffix(".crt").name
    assert (ca / "ca.key").stat().st_mode & 0o077 == 0
    assert not list(ca.glob("atlas-db-*.key")) and not list(data.glob("ca.*"))
    verify = ["openssl", "verify", "-CAfile", str(ca / "ca.crt"), "-purpose", "sslserver"]
    assert subprocess.run([*verify, flag, server, str(crt)], capture_output=True).returncode == 0
    other = "10.9.9.9" if flag == "-verify_ip" else "db-2.example.test"
    assert subprocess.run([*verify, flag, other, str(crt)], capture_output=True).returncode != 0
    # Running step 1 again never replaces the CA.
    again = shell(printed[1], tmp_path)
    assert again.returncode != 0 and "a CA is here already" in again.stdout


def verify_full(text: str, root: str = "/srv/db-root.crt") -> str:
    command = ["awk", "-v", f"root={root}", "-f", str(AWK)]
    out = subprocess.run(command, input=text, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_awk_sets_verify_full_and_keeps_every_other_line(tmp_path):
    original = (
        '# the API login\n[postgres]\nhost = "db-1.example.test"\nport = "5432"\n'
        f'user = "atlas_api"\npassword = "{PASSWORD}"\nsslmode = "require"\n'
        '# sslmode = "disable" stays a comment\n'
    )
    done = verify_full(original)
    data = tomllib.loads(done)["postgres"]
    assert data["sslmode"] == "verify-full" and data["sslrootcert"] == "/srv/db-root.crt"
    assert data["password"] == PASSWORD and data["host"] == "db-1.example.test"
    assert '# sslmode = "disable" stays a comment' in done
    assert verify_full(done) == done  # a second run changes nothing
    path = tmp_path / "database.toml"
    path.write_text(done)
    path.chmod(0o600)
    assert connect_options(path)["sslmode"] == "verify-full"


def private_env(tmp_path: Path, env: str, root: bytes | None) -> Path:
    conf = tmp_path / "home" / ".config" / "atlas"
    priv = conf / env
    priv.mkdir(parents=True)
    for folder in (conf, priv):
        folder.chmod(0o700)
    files = {
        "origin-key": "k" * 64,
        "database-api.toml": f'[postgres]\nhost = "db-1.example.test"\npassword = "{PASSWORD}"\n',
        "database.toml": f'[postgres]\nhost = "127.0.0.1"\npassword = "{PASSWORD}"\n',
    }
    for name, text in files.items():
        (priv / name).write_text(text)
        (priv / name).chmod(0o600)
    if root is not None:
        (conf / "db-root.crt").write_bytes(root)
    return priv


def test_setup_installs_the_root_and_switches_remote_files(tmp_path):
    priv = private_env(tmp_path, "production", trustme.CA().cert_pem.bytes())
    out = dry_run(tmp_path, "production", "--ref", COMMIT, "--dry-run")
    assert out.returncode == 0, out.stderr
    assert f"+ write {priv}/db-root.crt (mode 644)" in out.stdout
    assert f"+ write {priv}/database-api.toml (mode 600)" in out.stdout
    assert "database-api.toml uses verify-full" in out.stdout
    assert f"{priv}/database.toml" not in out.stdout  # a local host keeps its mode
    assert PASSWORD not in out.stdout + out.stderr


def test_setup_without_a_root_changes_no_database_file(tmp_path):
    priv = private_env(tmp_path, "staging", None)
    out = dry_run(tmp_path, "staging", "--ref", COMMIT, "--dry-run")
    assert out.returncode == 0, out.stderr
    assert "db-root.crt" not in out.stdout
    assert f"{priv}/database-api.toml" not in out.stdout


def test_setup_refuses_a_root_file_with_a_key(tmp_path):
    ca = trustme.CA()
    private_env(tmp_path, "production", ca.cert_pem.bytes() + ca.private_key_pem.bytes())
    out = dry_run(tmp_path, "production", "--ref", COMMIT, "--dry-run")
    assert out.returncode == 1
    assert "holds a private key" in out.stderr
    assert "BEGIN" not in out.stdout + out.stderr


def test_setup_refuses_a_root_file_that_is_no_certificate(tmp_path):
    private_env(tmp_path, "production", b"not a certificate\n")
    out = dry_run(tmp_path, "production", "--ref", COMMIT, "--dry-run")
    assert out.returncode == 1
    assert "is not a certificate" in out.stderr
