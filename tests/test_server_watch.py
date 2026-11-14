"""The wider checks of scripts/server-alerts/checks.sh (MONITOR-1, #1094) on the fake Canada
server of server_alerts_fake: each failure is posted once, its recovery once, nothing between."""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest
from server_alerts_fake import HEAD, ROOT, STAGING, T0, Server, server

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="GNU tools (stat -c, flock)")
__all__ = ["server"]  # the fixture


def manager(server: Server) -> None:
    """The production manager on, with both logins."""
    server.unit("atlas-orchestrator.service")
    server.set("tls-8443", "")
    server.set("tls-8444", "")
    server.database()


def once(server: Server, service: str, detail: str, heal) -> None:
    """One message for the failure, none on the next check, one "recovered" after heal()."""
    assert server.check() == [f"{HEAD}{service}: check failed: {detail}"]
    server.now += 3600
    assert server.check() == []
    heal()
    assert server.check() == [f"{HEAD}{service}: recovered: ok again"]
    assert server.check() == []


def openssl_date(seconds: int) -> str:
    return time.strftime("%b %d %H:%M:%S %Y GMT", time.gmtime(seconds))


def test_a_healthy_server_posts_nothing(server: Server) -> None:
    manager(server)
    server.unit("atlas-api@blue.service")
    server.set("healthz-8090", '{"status": "ok"}')
    server.set("healthz-8080", '{"status": "ok"}')
    server.set("minis", "browser-1\t30\nbrowser-2\t45\n")
    assert server.check() == []
    queries = [json.loads(line)["query"] for line in server.fake.joinpath("psql-env.jsonl").open()]
    assert any("FROM job_runs" in q for q in queries) and any("FROM nodes" in q for q in queries)


def test_a_failed_health_check_is_posted_once_until_it_recovers(server: Server) -> None:
    server.set("disk", "99")
    once(server, "disk", "the home file system is 99% full", lambda: server.set("disk", "40"))


def test_memory_load_and_failed_system_units(server: Server) -> None:
    server.memory(4_000_000, 300_000)
    once(
        server,
        "memory",
        "only 7% of the memory is available",
        lambda: server.memory(4_000_000, 2_000_000),
    )
    server.load("8.01")
    once(server, "load", "the 15-minute load is 8.01 on 4 CPUs", lambda: server.load("8.00"))
    server.set("failed-system", "postgresql@16-main.service loaded failed failed PostgreSQL\n")
    once(
        server,
        "system units",
        "failed system units: postgresql@16-main.service",
        lambda: server.set("failed-system", None),
    )


def test_host_checks_run_once_for_both_environments(server: Server) -> None:
    server.memory(4_000_000, 100_000)
    server.run("check-all")
    assert [t.split(": ")[1] for t in server.posts()] == ["memory"]


def test_public_site_contract_and_certificate(server: Server) -> None:
    server.set("public-down-healthz", "")
    once(
        server,
        "public atlas.shifter.io/healthz",
        "https://atlas.shifter.io/healthz did not answer 200: no answer",
        lambda: server.set("public-down-healthz", None),
    )
    server.set("public-v1-version", '{"revision": "r"}')
    once(
        server,
        "public atlas.shifter.io/v1/version",
        "https://atlas.shifter.io/v1/version: missing builtAt, contract",
        lambda: server.set("public-v1-version", None),
    )
    server.set("cert-end", openssl_date(server.now + 10 * 86400 + 60))
    once(
        server,
        "certificate atlas.shifter.io",
        "the certificate of atlas.shifter.io expires in 10 day(s)",
        lambda: server.set("cert-end", openssl_date(server.now + 14 * 86400)),
    )


def test_staging_watches_its_own_public_name(server: Server) -> None:
    server.set("public-healthz", '{"status": "degraded"}')
    texts = server.check("staging")
    assert texts == [
        f"{STAGING}public staging.atlas.shifter.io/healthz: check failed: "
        "https://staging.atlas.shifter.io/healthz: status is degraded"
    ]


def test_no_serving_colour_and_caddy(server: Server) -> None:
    server.unit("atlas-api@blue.service", active="inactive")
    once(
        server,
        "atlas-api serving",
        "no API colour is running",
        lambda: (
            server.unit("atlas-api@blue.service"),
            server.set("healthz-8090", '{"status": "ok"}'),
        ),
    )
    server.set("healthz-8080", None)
    once(
        server,
        "Caddy port 8080 /healthz",
        "no answer on port 8080",
        lambda: server.set("healthz-8080", '{"status": "ok"}'),
    )


@pytest.mark.parametrize(
    ("rows", "detail"),
    [
        (
            "calculate\t60\tfailed\tTimeout: calculation took too long\n",
            "the last run failed: Timeout: calculation took too long",
        ),
        ("calculate\t601\tok\t\n", "late: last run 10 min ago"),
        ("", "no run in the last 2 days"),
    ],
)
def test_manager_jobs_failed_or_late(server: Server, rows: str, detail: str) -> None:
    manager(server)
    others = "".join(
        f"{j}\t60\tok\t\n"
        for j in ("estimates_release", "browser_plan", "raw_upload", "dbip_update")
    )
    server.set("jobs", rows + others)
    once(server, "manager job calculate", detail, lambda: server.set("jobs", None))


def test_jobs_are_not_read_while_the_manager_is_off(server: Server) -> None:
    server.database()
    server.set("jobs", "")
    assert server.check() == []
    queries = [json.loads(line)["query"] for line in server.fake.joinpath("psql-env.jsonl").open()]
    assert not any("job_runs" in q or "current_calculation" in q for q in queries)


def test_manager_jobs_read_with_the_managers_login(server: Server) -> None:
    manager(server)
    server.check()
    envs = [json.loads(line) for line in server.fake.joinpath("psql-env.jsonl").open()]
    users = {e["PGUSER"] for e in envs if "job_runs" in e["query"] or "FROM nodes" in e["query"]}
    assert users == {"manager"}
    assert {e["PGUSER"] for e in envs if "current_calculation" in e["query"]} == {"api"}
    assert all("default_transaction_read_only=on" in e["PGOPTIONS"] for e in envs)


def test_calculation_age_ingest_and_connections(server: Server) -> None:
    manager(server)
    server.set("calc-age", "5460")
    once(
        server,
        "calculation",
        "the published calculation is 91 min old",
        lambda: server.set("calc-age", "60"),
    )
    server.set("calc-age", "")
    once(
        server, "calculation", "no published calculation yet", lambda: server.set("calc-age", "60")
    )
    server.unit("atlas-hourly.timer", LastTriggerUSec=f"@{T0 + 86400}")  # on time all test
    server.set("ingest-age", "5460")
    once(
        server,
        "ingest",
        "the newest stored test run is 91 min old",
        lambda: server.set("ingest-age", "60"),
    )
    server.set("connections", "16\t-1")  # no role limit: 20
    once(
        server,
        "API connections",
        "the API login uses 16 of its 20 connections",
        lambda: server.set("connections", "15\t-1"),
    )


def test_hourly_and_pulse_timers_fire_on_time(server: Server) -> None:
    server.unit("atlas-pulse.timer", LastTriggerUSec=f"@{T0 - 3781}", ActiveEnterTimestamp="@1")
    # once() moves the clock by an hour before the recovery: the timer fired a minute before it
    once(
        server,
        "atlas-pulse.timer on time",
        "did not fire for 63 min",
        lambda: server.unit("atlas-pulse.timer", LastTriggerUSec=f"@{T0 + 3600 - 60}"),
    )
    since = f"@{server.now - 600}"
    server.unit("atlas-hourly.timer", LastTriggerUSec="", ActiveEnterTimestamp=since)
    assert server.check() == []  # just switched on, not fired yet
    server.unit("atlas-hourly.timer", active="inactive", LastTriggerUSec=f"@{T0 - 99999}")
    assert server.check() == []  # off: not watched


def test_a_mini_offline_for_30_minutes(server: Server) -> None:
    manager(server)
    server.set("minis", "browser-1\t60\nbrowser-2\t1801\n")
    once(
        server,
        "mini browser-2",
        "offline: last seen 30 min ago",
        lambda: server.set("minis", "browser-1\t60\nbrowser-2\t10\n"),
    )


def test_daily_all_good_heartbeat(server: Server) -> None:
    server.heartbeat_hour = "8"  # T0 is 08:00 UTC
    assert server.check() == ["Atlas production on canada-test: all good"]
    server.now += 1800
    assert server.check() == []  # once a day
    server.now += 86400
    server.set("disk", "99")
    texts = server.check()
    assert texts == [HEAD + "disk: check failed: the home file system is 99% full"]  # not all good


def test_install_copies_the_checks_beside_the_script(server: Server) -> None:
    server.run("install", "production")
    installed = server.home / "atlas" / "bin" / "server-alerts" / "checks.sh"
    assert installed.read_bytes() == (ROOT / "scripts" / "server-alerts" / "checks.sh").read_bytes()
    server.set("disk", "99")
    result = subprocess.run(
        ["bash", str(server.home / "atlas" / "bin" / "atlas-alerts"), "check", "production"],
        env=server.env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert server.posts()[-1] == HEAD + "disk: check failed: the home file system is 99% full"


def test_a_database_outage_is_one_message_not_one_per_query(server: Server) -> None:
    manager(server)
    server.unit("atlas-hourly.timer", LastTriggerUSec=f"@{T0}")
    server.set("db-down", "")
    assert server.check() == [HEAD + "database: check failed: SELECT 1 failed"]
    server.set("db-down", None)
    assert server.check() == [HEAD + "database: recovered: ok again"]


def test_silence_holds_every_message_until_it_ends(server: Server) -> None:
    result = server.run("silence", "production", "30")
    assert result.stdout.strip() == "Atlas production alerts silenced for 30 minute(s)"
    server.set("disk", "99")
    assert server.check() == []
    server.now += 30 * 60
    assert server.check() == [HEAD + "disk: check failed: the home file system is 99% full"]
    server.run("silence", "production", "10")
    server.run("silence", "production", "0")
    server.set("disk", "40")
    assert server.check() == [HEAD + "disk: recovered: ok again"]
    assert server.run("silence", "production", "soon", check=False).returncode == 1
