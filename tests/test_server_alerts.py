"""scripts/server-alerts.sh (#1007) against fake systemctl, curl, journalctl, psql, openssl, df
and hostname on PATH, with a fake clock (ATLAS_ALERTS_NOW) and a fake home (server_alerts_fake)."""

from __future__ import annotations

import http.server
import json
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest
from server_alerts_fake import HEAD, SCRIPT, STAGING, T0, TOKEN, WEBHOOK, Server, server

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="GNU tools (stat -c, flock)")
__all__ = ["server"]  # the fixture


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


def test_a_failed_hour_unit_posts_one_short_line_without_its_journal(server: Server) -> None:
    """PULSE-STATE-1: a pulse hour (or hourly test) unit's failure is one line, posted once,
    whether OnFailure= or the check sees it first; never its journal's JSON summary."""
    pulse = "atlas-pulse-20261115T11.service"
    server.run("failed", pulse)
    server.unit(pulse, active="failed", filestate="transient", result="exit-code")
    assert server.check() == []  # posted once
    [text] = server.posts()
    detail = f"the pulse hour stopped with an error (journalctl --user -u {pulse})"
    assert text == f"{HEAD}{pulse}: check failed: {detail}"
    hourly = "atlas-hourly-20261115T11.service"
    server.unit(hourly, active="failed", filestate="transient", result="exit-code")
    [text] = server.check()
    assert "```" not in text
    assert text.endswith(f"the hourly test failed (journalctl --user -u {hourly})")


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


def test_a_running_disabled_unit_is_watched(server: Server) -> None:
    server.unit("atlas-orchestrator.service", filestate="disabled", restarts=0)
    server.set("tls-8443", "")
    server.set("tls-8444", "")
    assert server.check() == []
    server.unit("atlas-orchestrator.service", filestate="disabled", restarts=1)
    [text] = server.check()
    assert text.startswith(f"{HEAD}atlas-orchestrator.service: restarted: ")


def test_a_starting_colour_that_is_not_serving_is_not_checked(server: Server) -> None:
    server.unit("atlas-api@blue.service")
    server.unit("atlas-api@green.service", filestate="disabled")  # a deploy's new colour
    server.set("healthz-8090", '{"status": "ok"}')
    server.set("healthz-8091", '{"status": "starting"}')
    assert server.check() == []


def test_journal_lines_with_invalid_utf8_still_post(server: Server) -> None:
    server.set("journal-bytes", "")
    server.unit("atlas-orchestrator.service", active="failed")
    [text] = server.check()
    assert "bad  byte" in text


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
    # A manager alert is its name, server and message: no journal (access-log) lines.
    assert all("```" not in t for t in texts[:2]) and "```" in texts[2]
    seen = server.home / "atlas" / "production" / "alerts-state" / "alert-id"
    assert seen.read_text().strip() == "7"
    query = [a for a in server.argv() if a[0] == "psql" and "FROM alerts" in a[-1]][-1]
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
    assert server.posts()[-1] == "Atlas production: alerts test from canada-test"


def test_only_the_first_install_on_a_machine_posts_and_test_posts_on_demand(
    tmp_path: Path,
) -> None:
    """Every deploy installs the alerts again (server-setup.sh): Slack hears of the first only."""
    server = Server(tmp_path)
    server.run("install", "production")
    assert server.posts() == ["Atlas production: alerts installed on canada-test"]
    for _ in range(2):  # later deploys and updates
        result = server.run("install", "production")
        assert "no Slack post" in result.stdout
    assert server.posts() == ["Atlas production: alerts installed on canada-test"]
    server.run("test", "production")
    assert server.posts()[1:] == ["Atlas production: alerts test from canada-test"]
    server.run("install", "staging")  # another environment's first install posts once too
    assert server.posts()[-1] == "Atlas staging: alerts installed on canada-test"


def test_a_failed_first_post_is_sent_by_the_next_install(tmp_path: Path) -> None:
    server = Server(tmp_path)
    server.set("slack-down", "")
    assert server.run("install", "production", check=False).returncode != 0
    server.set("slack-down", None)
    server.run("install", "production")
    assert server.posts() == ["Atlas production: alerts installed on canada-test"]


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
    assert json.loads(body) == {"text": "Atlas staging: alerts test from canada-test"}


def test_manager_alerts_are_read_with_the_managers_login(server: Server) -> None:
    """The alerts table is the manager's: the API's read-only login may not read it (#1013), so
    the alerts are read with database.toml; the database health check stays on the API's login."""
    server.database()
    server.set("alert-max", "5")
    server.check()
    server.set("alert-rows", "6\tdisk\tfull\n")
    texts = server.check()
    first_lines = [t.split("\n")[0] for t in texts]
    assert first_lines == [HEAD + "manager alert 6 (disk): check failed: full"]
    envs = [json.loads(line) for line in (server.fake / "psql-env.jsonl").read_text().splitlines()]
    alerts = [e for e in envs if "FROM alerts" in e["query"]]
    assert len(alerts) == 2 and all(e["PGUSER"] == "manager" for e in alerts)
    assert {e["PGUSER"] for e in envs if e["query"] == "SELECT 1"} == {"api"}


def test_no_manager_login_reads_no_alerts(server: Server) -> None:
    server.database()
    (server.home / ".config" / "atlas" / "production" / "database.toml").unlink()
    server.check()
    envs = [json.loads(line) for line in (server.fake / "psql-env.jsonl").read_text().splitlines()]
    assert envs and not any("alerts" in e["query"] for e in envs)


# --- the alert router (ALERT-POLICY-1, #1297) ---------------------------------------------------


def live(server: Server) -> None:
    (server.home / ".config" / "atlas" / "alert-router").write_text("live\n")


def manager(server: Server) -> None:
    server.database()
    server.unit("atlas-orchestrator.service", ActiveEnterTimestamp="@1")
    server.set("tls-8443", "")
    server.set("tls-8444", "")


def dry_run(server: Server) -> list[dict]:
    path = server.home / "atlas" / "production" / "alerts-state" / "router-dry-run.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_dry_run_keeps_the_old_posts_and_records_the_routers(server: Server) -> None:
    server.set("disk", "86")
    assert server.check() == [HEAD + "disk: check failed: the home file system is 86% full"]
    assert dry_run(server) == []  # within its grace
    server.now += 15 * 60
    assert server.check() == []
    [page] = dry_run(server)
    assert page["kind"] == "page" and page["text"].startswith(HEAD + "disk: check failed: ")
    server.set("disk", "50")
    assert server.check() == [HEAD + "disk: recovered: ok again"]
    assert dry_run(server)[1]["kind"] == "followup" and dry_run(server)[1]["thread"]


def test_live_a_problem_that_heals_within_its_grace_posts_nothing(server: Server) -> None:
    live(server)
    server.set("disk", "86")
    assert server.check() == []
    server.now += 10 * 60
    server.set("disk", "50")
    assert server.check() == []
    server.set("disk", "86")
    assert server.check() == []
    server.now += 15 * 60
    [text] = server.check()
    assert text.startswith(HEAD + "disk: check failed: the home file system is 86% full\n")
    server.set("disk", "50")
    [text] = server.check()
    assert text.startswith("Atlas production on canada-test: disk: recovered at ")


def test_live_login_refused_pages_at_once_and_a_budgeted_gap_never(server: Server) -> None:
    live(server)
    manager(server)
    rows = (
        f"proxy_login_refused:proxyempire\tproxy_login_refused\t2\t{T0 - 60}\t"
        "proxyempire refused our login (HTTP 407)\n"
        f"pulse_runs_open:20261116T22:proxy-cheap/lon1\tpulse_runs_open\t2\t{T0 - 3600}\t"
        "1 country pulse of proxy-cheap in London was not measured\n"
    )
    server.set("open-alerts", rows)
    [text] = server.check()
    assert text.startswith(HEAD + "manager alert: proxyempire refused our login (HTTP 407)")
    server.set("open-alerts", "")  # acknowledged: the network was resumed
    [text] = server.check()
    assert "manager alert: recovered at " in text


def test_live_a_network_down_for_30_minutes_pages_and_recovers(server: Server) -> None:
    live(server)
    manager(server)
    server.set("outages", f"proxy-cheap residential\t129\t{T0 - 20 * 60}\n")
    assert server.check() == []
    server.now += 10 * 60
    [text] = server.check()
    assert text.startswith(HEAD + "network proxy-cheap residential: network down: down in 129")
    server.set("outages", "")
    [text] = server.check()
    assert "network proxy-cheap residential: recovered at" in text


def test_live_a_broken_router_is_posted_once(server: Server) -> None:
    live(server)
    state = server.home / "atlas" / "production" / "alerts-state"
    (state / "router.json").write_text("not json")
    result = server.run("check", "production", check=False)
    assert result.returncode == 1 and "alert router" in result.stderr
    [text] = server.posts()
    assert text.startswith(HEAD + "the alert router failed, alerts may be missing: ")
    server.run("check", "production", check=False)
    assert len(server.posts()) == 1
    (state / "router.json").unlink()
    server.check()
    assert not (state / "router.down").exists()
