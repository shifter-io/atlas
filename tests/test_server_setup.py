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
GO = re.search(r"^GO_VERSION=(\S+)$", SCRIPT.read_text(), re.M).group(1)
LAYOUT = {
    "production": {"api": "atlas-api@", "ports": ("8090", "8091", "8445", "8190", "8191")},
    "staging": {"api": "atlas-staging-api@", "ports": ("9090", "9091", "9445", "9190", "9191")},
}


def fake_bin(tmp_path: Path, uid: str, systemctl: str = "exit 3") -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (("id", f"echo {uid}"), ("systemctl", systemctl)):
        f = bin_dir / name
        f.write_text(f"#!/bin/sh\n{body}\n")
        f.chmod(0o755)
    return bin_dir


def dry_run(
    tmp_path: Path, *args: str, uid: str = "1000", systemctl: str = "exit 3", **extra: str
) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": f"{fake_bin(tmp_path, uid, systemctl)}:{os.environ['PATH']}",
        **extra,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
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
    blue, green, site, admin, green_admin = LAYOUT[env]["ports"]
    assert f"{home}/atlas/{env}/src" in out
    assert f"{home}/atlas/{env}/api/{COMMIT}/siteapi" in out
    assert f"{home}/.config/atlas/{env}/origin-key" in out
    assert f"systemctl --user restart {LAYOUT[env]['api']}blue" in out
    assert f"http://127.0.0.1:{admin}/healthz" in out
    assert f"Caddy site :{site} forwards to blue (127.0.0.1:{blue})" in out
    assert f"{home}/atlas/caddy/sites/{env}.caddy (mode 600)" in out
    assert f"{home}/.config/systemd/user/atlas-caddy.service" in out
    for tool in ("uv 0.13.8", "Python 3.12", f"Go {GO}", "Caddy 2.", "psql"):
        assert tool in out
    assert "sudo" not in out
    assert green not in out and green_admin not in out  # the first run starts blue only


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


@pytest.mark.parametrize(
    "args",
    [(), ("prod",), ("staging", "--ref"), ("staging", "--ref", "main"), ("staging", "--force")],
)
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


def stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def test_first_install_checks_out_the_commit(tmp_path):
    """A fresh clone of main at the given commit is clean: the run reaches the build step."""
    work = tmp_path / "work"
    (work / "api").mkdir(parents=True)
    (work / "api" / "go.mod").write_text("module example\n\ngo 1.0\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run([*git, "-C", str(work), "add", "-A"], check=True)
    subprocess.run([*git, "-C", str(work), "commit", "-qm", "c"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    bare = tmp_path / "repo.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    root = tmp_path / "home" / "atlas"
    stub(root / "bin" / "uv", 'echo "uv 0.13.8"')
    stub(root / "bin" / "caddy", 'echo "v2.11.9 h1:x"')
    stub(root / "bin" / "psql", 'echo "psql (PostgreSQL) 17.0"')
    stub(root / f"go-{GO}" / "bin" / "go", "exit 0")
    out = dry_run(tmp_path, "production", "--ref", commit, ATLAS_REPO_URL=str(bare))
    assert "has local changes" not in out.stderr
    assert "api/go.mod names Go 1.0" in out.stderr
    assert out.returncode == 1


def serving(tmp_path: Path, link: str) -> Path:
    """A production install serving blue (linked to `link`), the shared Caddyfile in place."""
    home = tmp_path / "home"
    api = home / "atlas" / "production" / "api"
    api.mkdir(parents=True)
    (api / "serving").write_text("blue\n")
    (api / "blue").symlink_to(link)
    caddy = home / "atlas" / "caddy"
    (caddy / "sites").mkdir(parents=True)
    tpl = (TEMPLATES / "Caddyfile").read_text().rstrip("\n") + "\n"
    (caddy / "Caddyfile").write_text(tpl.replace("@CADDY@", str(caddy)))
    private_files(tmp_path, "production", "k" * 64)
    return caddy


def test_switch_reloads_caddy_even_when_the_site_file_already_matches(tmp_path):
    """A cut-off run may have written the green site without reloading Caddy."""
    caddy = serving(tmp_path, "f" * 40)
    site = (TEMPLATES / "site.caddy").read_text().rstrip("\n") + "\n"
    for k, v in {"@ENV@": "production", "@SITE_PORT@": "8445", "@COLOUR@": "green"}.items():
        site = site.replace(k, v)
    site = site.replace("@UPSTREAM@", "8091").replace("@KEY@", "k" * 64)
    (caddy / "sites" / "production.caddy").write_text(site)
    (caddy / "sites" / "production.caddy").chmod(0o600)
    out = dry_run(tmp_path, "production", "--ref", COMMIT, "--dry-run", systemctl="exit 0")
    assert out.returncode == 0, out.stderr
    assert f"{caddy}/sites/production.caddy" not in out.stdout  # unchanged
    assert "+ systemctl --user reload atlas-caddy" in out.stdout
    assert "+ systemctl --user stop atlas-api@blue" in out.stdout


def test_same_commit_retires_a_colour_left_running(tmp_path):
    serving(tmp_path, COMMIT)
    out = dry_run(tmp_path, "production", "--ref", COMMIT, "--dry-run", systemctl="exit 0")
    assert out.returncode == 0, out.stderr
    assert "+ systemctl --user restart" not in out.stdout
    assert "+ systemctl --user stop atlas-api@green" in out.stdout
    assert "+ systemctl --user disable atlas-api@green" in out.stdout
