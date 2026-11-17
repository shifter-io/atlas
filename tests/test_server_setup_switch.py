"""scripts/server-setup.sh's blue/green switch, run for real (DEPLOY-SEAMLESS-1, #1273).

A staging install serves blue; the run deploys a new commit to green. systemctl, curl, jq and
sleep are fakes that log their arguments (systemctl and sleep in one log, in order, each
systemctl call with the hold files that exist then), so nothing is started; the self-check's
answers come from the fake curl (FAKE_BAD names a path that answers 502), and the fake jq
fails the health check of the commit FAKE_DOWN names.
"""

import re
from pathlib import Path

import test_server_setup as setup

OLD = "f" * 40
LOGGING = 'echo "$*" >>"$FAKE_LOG.{name}"'
FAKE_SYSTEMCTL = r"""
held=""
for f in "$FAKE_API"/hold-*; do [ -e "$f" ] && held="$held [${f##*/}]"; done
echo "$*$held" >>"$FAKE_LOG.systemctl"
"""
FAKE_CURL = r"""
echo "$*" >>"$FAKE_LOG.curl"
for a; do url=$a; done
case "$*" in
*%\{http_code\}*)
	if [ -n "$FAKE_BAD" ] && [ "${url#*"$FAKE_BAD"}" != "$url" ]; then printf 502
	else printf 200; fi ;;
*/healthz*) echo '{}' ;;
esac
"""


def staging(tmp_path: Path, files: dict[str, str] | None = None) -> tuple[str, Path, Path]:
    """Staging serving blue on OLD, its site and Caddyfile current, the new commit built; returns
    the new commit, the repository and the API folder."""
    commit, bare = setup.one_commit_repo(tmp_path, files)
    home = tmp_path / "home"
    api = home / "atlas" / "staging" / "api"
    (api / commit).mkdir(parents=True)
    setup.stub(api / commit / "siteapi", "exit 0")
    (api / "serving").write_text("blue\n")
    (api / "blue").symlink_to(OLD)
    wheels = home / "atlas" / "staging" / "wheels"
    wheels.mkdir(parents=True)
    (wheels / "current.json").write_text(f'{{"commit": "{commit}"}}\n')
    caddy = home / "atlas" / "caddy"
    (caddy / "sites").mkdir(parents=True)
    tpl = (setup.TEMPLATES / "Caddyfile").read_text().rstrip("\n") + "\n"
    (caddy / "Caddyfile").write_text(tpl.replace("@CADDY@", str(caddy)))
    key = "k" * 64
    site = caddy / "sites" / "staging.caddy"
    site.write_text(setup.rendered_site(key=key, env="staging", caddy=str(caddy)))
    site.chmod(0o600)
    setup.private_files(tmp_path, "staging", key)
    fakes = tmp_path / "fakebin"
    setup.stub(fakes / "curl", FAKE_CURL)
    down = 'case "$*" in *"${FAKE_DOWN:-none}"*) exit 1 ;; esac'
    jq = LOGGING.format(name="jq") + "\ncat >/dev/null\n" + down
    setup.stub(fakes / "jq", jq)
    setup.stub(fakes / "sleep", 'echo "sleep $*" >>"$FAKE_LOG.systemctl"')
    return commit, bare, api


def deploy(tmp_path: Path, commit: str, bare: Path, **extra: str):
    return setup.dry_run(
        tmp_path,
        "staging",
        "--ref",
        commit,
        systemctl=FAKE_SYSTEMCTL,
        ATLAS_REPO_URL=str(bare),
        FAKE_API=str(tmp_path / "home" / "atlas" / "staging" / "api"),
        FAKE_LOG=str(tmp_path / "log"),
        **extra,
    )


def logged(tmp_path: Path, name: str) -> list[str]:
    f = tmp_path / f"log.{name}"
    return f.read_text().splitlines() if f.exists() else []


def unit_steps(tmp_path: Path, held: bool = False) -> list[str]:
    """The start, stop, enable and disable calls on the API colours and the handover waits, in
    order; with `held`, each call names the hold files that existed then."""
    steps = []
    for line in logged(tmp_path, "systemctl"):
        if line == "sleep 15":
            steps.append(line)
        elif re.match(r"--user (start|restart|stop|enable|disable) atlas-staging-api@", line):
            line = line.removeprefix("--user ")
            steps.append(line if held else re.sub(r" \[hold-\w+\]", "", line))
    return steps


def self_check_urls(tmp_path: Path) -> list[str]:
    return [line.split()[-1] for line in logged(tmp_path, "curl") if "%{http_code}" in line]


def test_switch_stops_the_old_colour_without_touching_caddy(tmp_path):
    commit, bare, api = staging(tmp_path)
    out = deploy(tmp_path, commit, bare)
    assert out.returncode == 0, out.stderr
    assert unit_steps(tmp_path, held=True) == [
        "restart atlas-staging-api@green [hold-green]",  # held: Caddy sends it nothing yet
        "sleep 15",  # released after its checks, then Caddy's passing checks
        "stop atlas-staging-api@blue",
        "enable atlas-staging-api@green",
        "disable atlas-staging-api@blue",
    ]
    caddy = [line for line in logged(tmp_path, "systemctl") if "caddy" in line]
    assert caddy == ["--user is-active --quiet atlas-caddy"]  # never reloaded or restarted
    urls = self_check_urls(tmp_path)
    assert len(urls) == 40  # 20 rounds of the version and the biggest answer, then /healthz
    assert set(urls) == {
        "http://127.0.0.1:9080/v1/version",
        "http://127.0.0.1:9080/v1/home?range=7d",
    }
    assert (api / "serving").read_text() == "green\n"
    assert "http://127.0.0.1:9080/healthz" in logged(tmp_path, "curl")[-1]
    assert f"-e --arg c {commit} .revision == $c" in logged(tmp_path, "jq")
    assert (api / "green").readlink() == Path(commit)
    assert f"{commit} self-check 41 requests, 0 failed" in (api / "deploys.log").read_text()
    assert f"{commit} self-check 41 requests, 0 failed" in out.stdout
    assert not list(api.glob("hold-*"))


def test_self_check_asks_the_public_url(tmp_path):
    commit, bare, _ = staging(tmp_path)
    priv = tmp_path / "home" / ".config" / "atlas" / "staging"
    (priv / "public-url").write_text("https://api.example.invalid/\n")
    out = deploy(tmp_path, commit, bare)
    assert out.returncode == 0, out.stderr
    assert {u.split("/v1/")[0] for u in self_check_urls(tmp_path)} == {
        "https://api.example.invalid"
    }


def test_a_failed_self_check_rolls_back_to_the_old_colour(tmp_path):
    commit, bare, api = staging(tmp_path)
    out = deploy(tmp_path, commit, bare, FAKE_BAD="/v1/home")
    assert out.returncode == 1
    assert f"rolled back to atlas-staging-api@blue ({OLD})" in out.stderr
    assert out.stderr.count("self-check: /v1/home?range=7d answered 502") == 20
    assert unit_steps(tmp_path, held=True) == [
        "restart atlas-staging-api@green [hold-green]",
        "sleep 15",
        "stop atlas-staging-api@blue",
        "enable atlas-staging-api@green",
        "start atlas-staging-api@blue [hold-blue]",
        "sleep 15",  # Caddy has blue up again before green stops: no gap (review of #1275)
        "enable atlas-staging-api@blue",
        "stop atlas-staging-api@green",
        "disable atlas-staging-api@green",
    ]
    assert any(f"--arg c {OLD}" in line for line in logged(tmp_path, "jq"))  # blue's own commit
    assert (api / "serving").read_text() == "blue\n"
    log = (api / "deploys.log").read_text()
    assert f"{commit} self-check 41 requests, 20 failed" in log
    assert f"{commit} rolled back to {OLD}" in log


def test_a_bad_public_url_stops_before_anything_starts(tmp_path):
    commit, bare, _ = staging(tmp_path)
    priv = tmp_path / "home" / ".config" / "atlas" / "staging"
    (priv / "public-url").write_text("api.example.invalid\n")
    out = deploy(tmp_path, commit, bare)
    assert out.returncode == 1
    assert "public-url must hold one URL" in out.stderr
    assert unit_steps(tmp_path) == []


def test_an_older_one_colour_site_template_is_refused(tmp_path):
    """The templates come from the deployed commit: one from before #1273 names one colour."""
    tpl = {f"scripts/server-setup/{f.name}": f.read_text() for f in setup.TEMPLATES.iterdir()}
    old = "reverse_proxy 127.0.0.1:@BLUE_PORT@ 127.0.0.1:@GREEN_PORT@"
    tpl["scripts/server-setup/site.caddy"] = tpl["scripts/server-setup/site.caddy"].replace(
        old, "reverse_proxy 127.0.0.1:@UPSTREAM@"
    )
    commit, bare, _ = staging(tmp_path, tpl)
    site = tmp_path / "home" / "atlas" / "caddy" / "sites" / "staging.caddy"
    before = site.read_text()
    out = deploy(tmp_path, commit, bare)
    assert out.returncode == 1
    assert "forwards to one colour only (older than #1273); nothing was changed" in out.stderr
    assert site.read_text() == before
    assert unit_steps(tmp_path) == []


def test_a_rollback_whose_old_colour_does_not_come_back_keeps_the_new_one(tmp_path):
    commit, bare, api = staging(tmp_path)
    bad = {"FAKE_BAD": "/v1/version", "FAKE_DOWN": OLD, "ATLAS_HEALTH_TIMEOUT": "0"}
    out = deploy(tmp_path, commit, bare, **bad)
    assert out.returncode == 1
    blue, green = "atlas-staging-api@blue", "atlas-staging-api@green"
    assert f"{blue} did not come back on {OLD}; {green} keeps serving" in out.stderr
    assert unit_steps(tmp_path)[-1] == "start atlas-staging-api@blue"
    assert (api / "hold-blue").exists()  # never released: Caddy keeps green
    assert (api / "serving").read_text() == "green\n"


def test_a_new_colour_whose_checks_fail_is_never_released(tmp_path):
    """When blue is the new colour, Caddy prefers it as soon as its public /healthz answers 200;
    the hold file keeps that at 503 until the script's own checks pass (review of #1275)."""
    commit, bare, api = staging(tmp_path)
    (api / "serving").write_text("green\n")
    (api / "green").symlink_to(OLD)
    (api / "blue").unlink()
    out = deploy(tmp_path, commit, bare, FAKE_DOWN=commit, ATLAS_HEALTH_TIMEOUT="0")
    assert out.returncode == 1
    assert "nothing was switched" in out.stderr
    assert unit_steps(tmp_path, held=True) == [
        "restart atlas-staging-api@blue [hold-blue]",
        "stop atlas-staging-api@blue [hold-blue]",
    ]
    assert (api / "serving").read_text() == "green\n"
    assert self_check_urls(tmp_path) == []


def test_the_catalog_is_seeded_before_the_new_colour_starts(tmp_path):
    """SEED-ON-DEPLOY-1 (#1284): the deploy seeds the targets and site.targets itself, with the
    manager's login, then starts the new colour; a second run seeds again (it changes nothing)."""
    commit, bare, _ = staging(tmp_path)
    out = deploy(tmp_path, commit, bare)
    assert out.returncode == 0, out.stderr
    db = tmp_path / "home" / ".config" / "atlas" / "staging" / "database.toml"
    assert logged(tmp_path, "atlas") == [
        f"db-seed --only targets --require-catalog --database-file {db}"
    ]
    assert unit_steps(tmp_path)[0] == "restart atlas-staging-api@green"


def test_an_incomplete_catalog_stops_before_anything_starts(tmp_path):
    """The staging deploy of 16 Nov 2026 served social_web's sources without social_web: now the
    seed's check fails, the run names the fix and nothing is started or switched."""
    commit, bare, api = staging(tmp_path)
    out = deploy(tmp_path, commit, bare, FAKE_SEED_FAIL="1")
    assert out.returncode == 1
    assert "site.targets lacks or differs for social_web" in out.stderr
    assert "the target catalog is incomplete (above); nothing was switched" in out.stderr
    assert unit_steps(tmp_path) == []
    assert (api / "serving").read_text() == "blue\n"
    assert not (api / "green").exists()


def test_a_missing_manager_login_stops_before_anything_starts(tmp_path):
    commit, bare, _ = staging(tmp_path)
    db = tmp_path / "home" / ".config" / "atlas" / "staging" / "database.toml"
    db.unlink()
    out = deploy(tmp_path, commit, bare)
    assert out.returncode == 1
    assert f"missing private file {db}" in out.stderr
    assert unit_steps(tmp_path) == [] and logged(tmp_path, "atlas") == []
