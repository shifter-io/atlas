"""scripts/server-setup.sh: the --dry-run plan per environment, its layout and its refusals.

CI has no user systemd, so every test runs the plan (--dry-run) with a temporary home and
fake `id` and `systemctl` commands; nothing is downloaded, built or started.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "server-setup.sh"
TEMPLATES = ROOT / "scripts" / "server-setup"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
LAYOUT = {
    "production": {"api": "atlas-api@", "ports": ("8090", "8091", "8445")},
    "staging": {"api": "atlas-staging-api@", "ports": ("9090", "9091", "9445")},
}


def fake_bin(tmp_path: Path, uid: str) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (("id", f"echo {uid}"), ("systemctl", "exit 3")):
        f = bin_dir / name
        f.write_text(f"#!/bin/sh\n{body}\n")
        f.chmod(0o755)
    return bin_dir


def dry_run(tmp_path: Path, *args: str, uid: str = "1000") -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": f"{fake_bin(tmp_path, uid)}:{os.environ['PATH']}",
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60
    )


def plan(tmp_path: Path, env: str) -> str:
    out = dry_run(tmp_path, env, "--ref", COMMIT, "--dry-run")
    assert out.returncode == 0, out.stderr
    return out.stdout


def private_files(tmp_path: Path, env: str, key: str) -> Path:
    priv = tmp_path / "home" / ".config" / "atlas" / env
    priv.mkdir(parents=True)
    priv.parent.chmod(0o700)
    priv.chmod(0o700)
    for name, text in (("origin-key", key), ("database-api.toml", "[postgres]\n")):
        (priv / name).write_text(text)
        (priv / name).chmod(0o600)
    return priv


@pytest.mark.parametrize("env", sorted(LAYOUT))
def test_plan_uses_the_layout(tmp_path, env):
    out = plan(tmp_path, env)
    home = tmp_path / "home"
    blue, green, site = LAYOUT[env]["ports"]
    assert f"{home}/atlas/{env}/src" in out
    assert f"{home}/atlas/{env}/api/{COMMIT}/siteapi" in out
    assert f"{home}/.config/atlas/{env}/origin-key" in out
    assert f"systemctl --user restart {LAYOUT[env]['api']}blue" in out
    assert f"http://127.0.0.1:{blue}/healthz" in out
    assert f"Caddy site :{site} forwards to blue (127.0.0.1:{blue})" in out
    assert f"{home}/atlas/caddy/sites/{env}.caddy (mode 600)" in out
    assert f"{home}/.config/systemd/user/atlas-caddy.service" in out
    for tool in ("uv 0.13.8", "Python 3.12", "Go 1.27.3", "Caddy 2.", "psql"):
        assert tool in out
    assert "sudo" not in out
    assert green not in out  # the first run starts blue only


def test_environments_share_no_name_port_or_folder(tmp_path):
    prod = plan(tmp_path / "p", "production")
    stag = plan(tmp_path / "s", "staging")
    for port in LAYOUT["staging"]["ports"]:
        assert port not in prod
    for port in LAYOUT["production"]["ports"]:
        assert port not in stag
    assert "staging" not in prod
    assert "production" not in stag
    assert "atlas-api@" not in stag


def test_first_install_reports_missing_private_files(tmp_path):
    out = plan(tmp_path, "staging")
    priv = tmp_path / "home" / ".config" / "atlas" / "staging"
    assert f"install -d -m 700 {priv}" in out
    assert f"create {priv}/origin-key (32 random bytes)" in out
    assert f"MISSING private file {priv}/database-api.toml" in out


def test_origin_key_is_never_printed(tmp_path):
    key = "f" * 64
    private_files(tmp_path, "production", key)
    out = dry_run(tmp_path, "production", "--ref", COMMIT, "--dry-run")
    assert out.returncode == 0, out.stderr
    assert key not in out.stdout + out.stderr
    assert "MISSING" not in out.stdout
    assert "origin-key (32 random bytes)" not in out.stdout


def test_private_file_with_wrong_mode_stops_naming_only_the_file(tmp_path):
    priv = private_files(tmp_path, "production", "secret-value")
    (priv / "database-api.toml").chmod(0o644)
    out = dry_run(tmp_path, "production", "--ref", COMMIT, "--dry-run")
    assert out.returncode == 1
    assert f"private file {priv}/database-api.toml must be mode 600" in out.stderr
    assert "secret-value" not in out.stdout + out.stderr


def test_refuses_root(tmp_path):
    out = dry_run(tmp_path, "production", "--dry-run", uid="0")
    assert out.returncode == 1
    assert "refusing to run as root" in out.stderr


@pytest.mark.parametrize("args", [(), ("prod",), ("staging", "--ref"), ("staging", "--force")])
def test_bad_arguments(tmp_path, args):
    out = dry_run(tmp_path, *args)
    assert out.returncode == 2
    assert "usage: server-setup.sh" in out.stderr


def test_script_needs_no_root():
    text = SCRIPT.read_text()
    assert not re.search(r"\bsudo\b", text)
    assert not re.search(r"\bapt(-get)? +install\b", text)
    assert "apt-get download" in text and "dpkg -x" in text
    assert os.stat(SCRIPT).st_mode & stat.S_IXUSR


def test_new_files_use_the_new_name_only():
    for f in [SCRIPT, *TEMPLATES.iterdir()]:
        text = f.read_text().lower()
        assert "proxy-index" not in text and "proxy_index" not in text, f.name


def test_api_unit_template():
    unit = (TEMPLATES / "api@.service").read_text()
    for line in (
        "MemoryMax=7G",
        "Environment=GOMEMLIMIT=6451MiB",
        "EnvironmentFile=-@PRIV@/api.env",
        "--row-cache 512",
        "--database-file @PRIV@/database-api.toml",
        "--files-dir @API@/files-%i",
        "--listen ${ATLAS_API_LISTEN}",
        "$ATLAS_API_FLAGS",
    ):
        assert line in unit


@pytest.mark.parametrize("name", ["api@.service", "atlas-caddy.service"])
def test_units_carry_the_restart_policy(name):
    unit = (TEMPLATES / name).read_text()
    policy = (
        "Restart=on-failure",
        "RestartSec=10",
        "StartLimitIntervalSec=600",
        "StartLimitBurst=5",
    )
    for line in policy:
        assert re.search(rf"^{line}$", unit, re.M), (name, line)


def test_caddy_site_answers_403_without_the_header():
    site = (TEMPLATES / "site.caddy").read_text()
    assert "tls internal" in site
    assert "@cdn header X-Atlas-Origin-Key @KEY@" in site
    assert "reverse_proxy 127.0.0.1:@UPSTREAM@" in site
    assert re.search(r"^\trespond 403$", site, re.M)
    assert "import sites/*.caddy" in (TEMPLATES / "Caddyfile").read_text()
