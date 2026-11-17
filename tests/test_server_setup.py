"""scripts/server-setup.sh: the --dry-run plan per environment, its layout and its refusals.

CI has no user systemd, so every test runs the plan (--dry-run) with a temporary home and
fake `id` and `systemctl` commands; nothing is downloaded, built or started.
"""

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "server-setup.sh"
TEMPLATES = ROOT / "scripts" / "server-setup"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
GO = re.search(r"^GO_VERSION=(\S+)$", SCRIPT.read_text(), re.M).group(1)
LAYOUT = {
    # blue, green, HTTPS site, blue admin, green admin, plain HTTP site
    "production": {"api": "atlas-api@", "ports": ("8090", "8091", "8445", "8190", "8191", "8080")},
    "staging": {
        "api": "atlas-staging-api@",
        "ports": ("9090", "9091", "9445", "9190", "9191", "9080"),
    },
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
    login = "[postgres]\n"
    for name, text in (("origin-key", key), ("database-api.toml", login), ("database.toml", login)):
        (priv / name).write_text(text)
        (priv / name).chmod(0o600)
    return priv


@pytest.mark.parametrize("env", sorted(LAYOUT))
def test_plan_uses_the_layout(tmp_path, env):
    out = plan(tmp_path, env)
    home = tmp_path / "home"
    blue, green, site, admin, green_admin, http = LAYOUT[env]["ports"]
    assert f"{home}/atlas/{env}/src" in out
    assert f"{home}/atlas/{env}/api/{COMMIT}/siteapi" in out
    assert f"{home}/.config/atlas/{env}/origin-key" in out
    assert f"systemctl --user restart {LAYOUT[env]['api']}blue" in out
    assert f"http://127.0.0.1:{admin}/healthz" in out
    assert (
        f"Caddy site :{site} and plain HTTP :{http} forward to the healthy colour"
        f" (blue 127.0.0.1:{blue}, then green 127.0.0.1:{green})"
    ) in out
    assert f"{home}/atlas/caddy/sites/{env}.caddy (mode 600)" in out
    assert f"{home}/.config/systemd/user/atlas-caddy.service" in out
    for tool in ("uv 0.13.8", "Python 3.12", f"Go {GO}", "Caddy 2.", "psql"):
        assert tool in out
    assert "sudo" not in out
    assert f"{LAYOUT[env]['api']}green" not in out and green_admin not in out  # blue only
    assert f"self-check: /v1/version and /v1/home?range=7d through http://127.0.0.1:{http}" in out


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
    assert f"MISSING private file {priv}/database.toml" in out  # the catalog seed's login


@pytest.mark.parametrize("env", sorted(LAYOUT))
def test_plan_seeds_the_catalog_before_anything_is_switched(tmp_path, env):
    """SEED-ON-DEPLOY-1 (#1284): the seed and its catalog check run with the manager's login,
    automatically on every run, before the units and the new colour."""
    out = plan(tmp_path, env)
    home = tmp_path / "home"
    seed = (
        f"+ {home}/atlas/{env}/venv/bin/atlas db-seed --only targets --require-catalog"
        f" --database-file {home}/.config/atlas/{env}/database.toml"
    )
    assert seed in out
    assert out.index(seed) < out.index("api@.service") < out.index("systemctl --user restart")


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
    assert "reverse_proxy 127.0.0.1:@BLUE_PORT@ 127.0.0.1:@GREEN_PORT@ {" in site
    assert re.search(r"^\trespond 403$", site, re.M)
    assert "import sites/*.caddy" in (TEMPLATES / "Caddyfile").read_text()


SITE_PORTS = {"production": ("8445", "8080"), "staging": ("9445", "9080")}


def rendered_site(
    key: str = "k" * 64, env: str = "production", caddy: str = "/srv/atlas/caddy"
) -> str:
    """site.caddy as server-setup.sh's render writes it (default: production)."""
    site = (TEMPLATES / "site.caddy").read_text().rstrip("\n") + "\n"
    https, http = SITE_PORTS[env]
    for k, v in {
        "@ENV@": env,
        "@SITE_PORT@": https,
        "@HTTP_PORT@": http,
        "@BLUE_PORT@": LAYOUT[env]["ports"][0],
        "@GREEN_PORT@": LAYOUT[env]["ports"][1],
        "@CADDY@": caddy,
    }.items():
        site = site.replace(k, v)
    site = site.replace("@KEY@", key)
    assert not re.search(r"@[A-Z_]+@", site)
    return site


def listeners(site: str) -> dict[str, str]:
    """The rendered site's top-level blocks other than the snippet, by their address line."""
    return {
        line: block(site, line)
        for line in re.findall(r"^(\S[^\n]*?) \{$", site, re.M)
        if not line.startswith("(")
    }


@pytest.mark.parametrize("env", sorted(SITE_PORTS))
def test_site_has_https_with_the_key_check_and_plain_http_without(env):
    """ORIGIN-HTTP-1: the CDN fetches over plain HTTP with no header; HTTPS keeps the key check."""
    https, http = SITE_PORTS[env]
    site = rendered_site(env=env)
    got = listeners(site)
    assert set(got) == {f"https://atlas-origin:{https}, https://:{https}", f"http://:{http}"}
    snippets = re.findall(r"^\((\S+)\) \{$", site, re.M)
    assert snippets == [f"atlas-{env}-colours", f"atlas-{env}-origin"]
    for body in got.values():  # one log, the request limits and the colours on both
        assert re.search(rf"^\timport atlas-{env}-origin$", body, re.M)
        assert len(re.findall(rf"^\t+import atlas-{env}-colours$", body, re.M)) == 1
    secure, plain = got[f"https://atlas-origin:{https}, https://:{https}"], got[f"http://:{http}"]
    assert re.search(r"^\t@cdn header X-Atlas-Origin-Key k{64}$", secure, re.M)
    assert re.search(r"^\trespond 403$", secure, re.M)
    blue, green = LAYOUT[env]["ports"][:2]
    proxy = rf"^\treverse_proxy 127\.0\.0\.1:{blue} 127\.0\.0\.1:{green} \{{$"
    assert len(re.findall(proxy, site, re.M)) == 1
    assert "X-Atlas-Origin-Key" not in plain and "respond" not in plain and "@" not in plain
    other = SITE_PORTS["staging" if env == "production" else "production"]
    assert not any(port in site for port in other)


def test_plain_http_listener_never_redirects_and_has_no_hsts():
    site = rendered_site()
    http = listeners(site)["http://:8080"]
    assert "tls" not in http and "Strict-Transport" not in http
    assert not re.search(r"^\s*redir\b", site, re.M)
    caddyfile = (TEMPLATES / "Caddyfile").read_text()
    assert re.search(r"^\tauto_https disable_redirects$", caddyfile, re.M)


def block(text: str, opener: str) -> str:
    """The body of the Caddyfile block that starts with the line `opener {`."""
    start = text.index(opener + " {\n") + len(opener) + 3
    depth, i = 1, start
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[start : i - 1]


def deletes(log_filter: str, header: str) -> bool:
    """Whether a Caddy log filter deletes the request header `header`."""
    return bool(re.search(rf"^\t+request>headers>{header} delete$", log_filter, re.M))


def test_caddy_bounds_slow_and_large_requests():
    servers = block((TEMPLATES / "Caddyfile").read_text(), "\tservers")
    timeouts = block(servers, "\t\ttimeouts")
    for line in ("read_header 5s", "read_body 10s", "write 60s", "idle 2m"):
        assert re.search(rf"^\t+{line}$", timeouts, re.M), line
    assert re.search(r"^\t+max_header_size 64KB$", servers, re.M)
    assert re.search(r"^\t+max_size 1MB$", block(rendered_site(), "\trequest_body"), re.M)


def test_caddy_admin_is_never_on_the_network():
    admin = re.findall(r"^\s*admin (\S+)", (TEMPLATES / "Caddyfile").read_text(), re.M)
    assert admin == ["unix/@CADDY@/admin.sock"]


def test_access_log_drops_the_secret_header():
    site = rendered_site()
    log = block(site, "\tlog")
    assert "output file /srv/atlas/caddy/logs/production-access.log" in log
    assert re.search(r"^\t+mode 600$", log, re.M)
    header = re.search(r"@cdn header (\S+) ", site).group(1)
    assert deletes(block(log, "\t\tformat filter"), header)
    assert site.count("k" * 64) == 1  # the key only in the matcher, never in the log settings


def test_caddy_own_log_drops_the_secret_header():
    """Caddy's error log (a 502 while a colour restarts) also carries the request headers."""
    caddyfile = (TEMPLATES / "Caddyfile").read_text()
    log = block(caddyfile, "\tlog default")
    header = re.search(r"@cdn header (\S+) ", rendered_site()).group(1)
    assert deletes(block(log, "\t\tformat filter"), header)


def test_secret_check_runs_before_proxying():
    site = rendered_site()
    cdn = block(site, "\thandle @cdn")
    assert site.index("@cdn header X-Atlas-Origin-Key " + "k" * 64) < site.index("handle @cdn")
    assert re.search(r"^\trespond 403$", site, re.M)  # no body: no JSON, no version
    https = listeners(site)["https://atlas-origin:8445, https://:8445"]
    assert https.count("import atlas-production-colours") == 1
    assert "import atlas-production-colours" in cdn
    assert 'header Strict-Transport-Security "max-age=31536000"' in cdn  # forwarded HTTPS only
    assert len(re.findall(r"^\t*header .*Strict-Transport-Security", site, re.M)) == 1


def test_only_the_edge_file_lets_caddy_name_the_visitor():
    """SEC-RATE-1: each listener overwrites X-Atlas-Client-Ip with `unknown` (siteapi: no visitor),
    whatever the request sent; the refresh's client-header file, imported after it, wins."""
    site = rendered_site()
    proxy = block(site, "\treverse_proxy 127.0.0.1:8090 127.0.0.1:8091")
    lines = [line.strip() for line in proxy.strip().splitlines()]
    assert lines[-2:] == [
        "header_up X-Atlas-Client-Ip unknown",
        "import /srv/atlas/caddy/edges/client-header*.caddy",
    ]
    assert len(re.findall(r"^\t+(header_up|import \S+client-header)", site, re.M)) == 2
    servers = block((TEMPLATES / "Caddyfile").read_text(), "\tservers")
    assert re.search(r"^\t+import @CADDY@/edges/cdn-edges\*\.caddy$", servers, re.M)
    assert re.search(r"^\t+trusted_proxies_strict$", servers, re.M)
    assert re.search(r"^\t+client_ip_headers X-Forwarded-For$", servers, re.M)
    assert not re.search(r"^\s*trusted_proxies ", servers, re.M)  # only the edge file trusts


def test_the_visitor_header_is_the_same_everywhere():
    """siteapi's --client-header default (api/cmd/siteapi/main_test.go holds it there)."""
    script = (TEMPLATES / "atlas-cdn-edges.py").read_text()
    header = re.search(r'^HEADER = "(\S+)"$', script, re.M).group(1)
    assert header == "X-Atlas-Client-Ip"
    assert f"header_up {header} unknown" in (TEMPLATES / "site.caddy").read_text()


def test_plan_writes_the_edge_refresh_and_never_enables_it(tmp_path):
    """The operator enables the timer (docs/operations/canada-server.md); a deploy never does."""
    out = plan(tmp_path, "production")
    home = tmp_path / "home"
    for dest, mode in (
        (home / ".config/systemd/user/atlas-cdn-edges.service", 644),
        (home / ".config/systemd/user/atlas-cdn-edges.timer", 644),
        (home / "atlas/bin/atlas-cdn-edges", 755),
    ):
        assert f"+ write {dest} (mode {mode})" in out
    assert "enable" not in "\n".join(line for line in out.splitlines() if "cdn-edges" in line)
    unit = (TEMPLATES / "atlas-cdn-edges.service").read_text()
    assert "ExecStart=@BIN@/atlas-cdn-edges --caddy @CADDY@" in unit
    timer = (TEMPLATES / "atlas-cdn-edges.timer").read_text()
    for line in ("OnCalendar=daily", "Persistent=true", "WantedBy=timers.target"):
        assert re.search(rf"^{line}$", timer, re.M), line


def test_caddy_is_pinned_with_its_sha256():
    text = SCRIPT.read_text()
    assert re.search(r"^CADDY_VERSION=\d+\.\d+\.\d+$", text, re.M)
    assert re.search(r"^CADDY_SHA256=[0-9a-f]{64}$", text, re.M)
    assert re.search(r'fetch "\S+/v\$CADDY_VERSION/\S+" "\$CADDY_SHA256"', text)


def test_plan_creates_the_private_log_folder(tmp_path):
    out = plan(tmp_path, "production")
    assert f"install -d -m 700 {tmp_path / 'home' / 'atlas' / 'caddy' / 'logs'}" in out


def stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


# A fake uv: `sync` installs src/atlas into $UV_PROJECT_ENVIRONMENT/pkg, which the venv's
# fake python names as the atlas package; FAKE_UV_STALE=1 installs nothing (uv's cached build).
FAKE_UV = r"""
[ "$1" = sync ] || { echo "uv 0.13.8"; exit 0; }
v=$UV_PROJECT_ENVIRONMENT
mkdir -p "$v/bin" "$v/pkg"
printf '#!/bin/sh\necho %s/pkg\n' "$v" >"$v/bin/python" && chmod 755 "$v/bin/python"
[ -n "$FAKE_UV_STALE" ] || cp -R src/atlas/. "$v/pkg/"
cat >"$v/bin/atlas" <<'ATLAS'
#!/bin/sh
[ -z "$FAKE_LOG" ] || echo "$*" >>"$FAKE_LOG.atlas"
[ -z "$FAKE_SEED_FAIL" ] || { echo "site.targets lacks or differs for social_web" >&2; exit 2; }
ATLAS
chmod 755 "$v/bin/atlas"
"""


def one_commit_repo(tmp_path: Path, files: dict[str, str] | None = None) -> tuple[str, Path]:
    """A bare repository whose main is one commit (a tiny api/ and src/atlas, plus `files`), and
    the server's tools stubbed under the temporary home; returns the commit and the repository."""
    work = tmp_path / "work"
    tree = {"api/go.mod": "module example\n\ngo 1.0\n", "src/atlas/__init__.py": "VERSION = 2\n"}
    for name, text in {**tree, **(files or {})}.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(text)
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
    stub(root / "bin" / "uv", FAKE_UV)
    stub(root / "bin" / "caddy", 'echo "v2.11.9 h1:x"')
    stub(root / "bin" / "psql", 'echo "psql (PostgreSQL) 17.0"')
    stub(root / f"go-{GO}" / "bin" / "go", "exit 0")
    return commit, bare


def first_install(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    """A fresh clone of a one-commit main, its tools stubbed, run for real up to the build step."""
    commit, bare = one_commit_repo(tmp_path)
    return dry_run(tmp_path, "production", "--ref", commit, ATLAS_REPO_URL=str(bare), **extra)


def test_first_install_checks_out_the_commit(tmp_path):
    """A fresh clone of main at the given commit is clean and installed: it reaches the build."""
    out = first_install(tmp_path)
    assert "has local changes" not in out.stderr
    assert "is not" not in out.stderr
    assert "api/go.mod names Go 1.0" in out.stderr
    assert out.returncode == 1


def test_stale_installed_code_stops_before_the_build(tmp_path):
    """The venv still holds an earlier commit's atlas (#1090): the run stops, naming the file."""
    pkg = tmp_path / "home" / "atlas" / "production" / "venv" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("VERSION = 1\n")
    out = first_install(tmp_path, FAKE_UV_STALE="1")
    assert out.returncode == 1
    assert "__init__.py" in out.stderr
    assert f"the installed atlas in {pkg} is not" in out.stderr
    assert "api/go.mod" not in out.stderr


def test_sync_step_installs_changed_code(tmp_path):
    """The script's own uv sync flags, run twice with real uv: a code-only change is installed."""
    line = re.search(r"run (uv sync .+)\)$", SCRIPT.read_text(), re.M).group(1)
    cmd = line.replace('"$PYTHON_VERSION"', sys.executable).split()
    project = tmp_path / "project"
    (project / "src" / "atlas").mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling>=1.27"]\nbuild-backend = "hatchling.build"\n'
        '[project]\nname = "atlas"\nversion = "0.0.1"\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["src/atlas"]\n'
    )
    source = project / "src" / "atlas" / "__init__.py"
    source.write_text("VERSION = 1\n")
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env["UV_PROJECT_ENVIRONMENT"] = str(tmp_path / "venv")
    uv = {"cwd": project, "env": env, "check": True, "timeout": 120}
    subprocess.run(["uv", "lock", "-q", "--python", sys.executable], **uv)
    subprocess.run(cmd, **uv)
    source.write_text("VERSION = 2\n")
    subprocess.run(cmd, **uv)
    (installed,) = (tmp_path / "venv").glob("lib/python*/site-packages/atlas/__init__.py")
    assert installed.read_text() == "VERSION = 2\n"


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


def test_switch_never_writes_the_site_or_reloads_caddy(tmp_path):
    """DEPLOY-SEAMLESS-1 (#1273): a reload closed the CDN's idle connections and dropped a fetch.
    A new commit starts green, waits for Caddy's health checks, stops blue, then self-checks."""
    caddy = serving(tmp_path, "f" * 40)
    (caddy / "sites" / "production.caddy").write_text(rendered_site(caddy=str(caddy)))
    (caddy / "sites" / "production.caddy").chmod(0o600)
    out = dry_run(tmp_path, "production", "--ref", COMMIT, "--dry-run", systemctl="exit 0")
    assert out.returncode == 0, out.stderr
    assert f"{caddy}/sites/production.caddy" not in out.stdout  # unchanged
    assert "reload atlas-caddy" not in out.stdout
    api = tmp_path / "home" / "atlas" / "production" / "api"
    steps = [
        f"+ touch {api}/hold-green",
        "+ systemctl --user restart atlas-api@green",
        "+ wait until http://127.0.0.1:8191/healthz is ok",
        f"+ rm -f {api}/hold-green",
        "+ sleep 15",
        "+ systemctl --user stop atlas-api@blue",
        "+ systemctl --user enable atlas-api@green",
        "+ self-check: ",
        "+ systemctl --user disable atlas-api@blue",
    ]
    at = [out.stdout.index(step) for step in steps]
    assert at == sorted(at)


def test_same_commit_retires_a_colour_left_running(tmp_path):
    serving(tmp_path, COMMIT)
    out = dry_run(tmp_path, "production", "--ref", COMMIT, "--dry-run", systemctl="exit 0")
    assert out.returncode == 0, out.stderr
    assert "+ systemctl --user restart" not in out.stdout
    assert "+ systemctl --user stop atlas-api@green" in out.stdout
    assert "+ systemctl --user disable atlas-api@green" in out.stdout


def test_health_probes_only_live_routes():
    """The deploy's probes are documented API routes; /v1/search-index was removed (#981)."""
    line = re.search(r"^\tfor path in (.+); do$", SCRIPT.read_text(), re.M).group(1)
    paths = [p.strip("'").split("?")[0].replace("/shifter", "/{slug}") for p in line.split()]
    readme = (ROOT / "api" / "README.md").read_text()
    assert "/v1/search-index" not in paths
    for path in paths:
        assert f"`{path}" in readme, path


def test_both_listeners_follow_the_healthy_colour():
    """Blue first, green while blue is down; a starting colour (503) or a stopped one fails the
    check, and a refused or broken GET is retried on the other colour, so a stop drops nothing."""
    proxy = block(rendered_site(), "\treverse_proxy 127.0.0.1:8090 127.0.0.1:8091")
    for line in (
        "lb_policy first",
        "lb_try_duration 10s",
        "lb_try_interval 250ms",
        "fail_duration 5s",
        "health_uri /healthz",
        "health_interval 2s",
        "health_passes 5",
        "health_fails 1",
    ):
        assert re.search(rf"^\t\t{line}$", proxy, re.M), line
    assert re.search(r"^\t\t\tmethod GET HEAD$", block(proxy, "\t\tlb_retry_match"), re.M)
    passes = re.search(r"health_passes (\d+)", proxy).group(1)
    interval = re.search(r"health_interval (\d+)s", proxy).group(1)
    handover = re.search(r"ATLAS_HANDOVER_SECONDS:-(\d+)", SCRIPT.read_text()).group(1)
    assert int(handover) > int(passes) * int(interval)  # Caddy sees the new colour first


def test_same_commit_and_site_change_nothing(tmp_path):
    """A second run with the same commit leaves the site file alone and does not reload Caddy."""
    caddy = serving(tmp_path, COMMIT)
    site = caddy / "sites" / "production.caddy"
    site.write_text(rendered_site(caddy=str(caddy)))
    site.chmod(0o600)
    out = dry_run(tmp_path, "production", "--ref", COMMIT, "--dry-run", systemctl="exit 0")
    assert out.returncode == 0, out.stderr
    assert str(site) not in out.stdout
    assert "reload atlas-caddy" not in out.stdout
    assert "Caddy site :8445 and plain HTTP :8080 forward to the healthy colour" in out.stdout


def test_slack_alerts_install_for_production_only(tmp_path):
    """Staging's run installs no alert units and removes none (the owner, 2026-11-14)."""
    assert "server-alerts.sh install production" in plan(tmp_path / "p", "production")
    assert "server-alerts" not in plan(tmp_path / "s", "staging")
