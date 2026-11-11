"""AUDIT-DAILY-1: the live tape's audits for an hour's daily tests, against an isolated PostgreSQL.

A daily test has no batch, so detect() never audits it; the daily ingest writes its
new_hosting_asn and ip_overlap events with detect's checks once the hour's cells are stored.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from census_pulse_cases import HOSTING
from psycopg.rows import dict_row
from test_storage_v6 import census

from proxy_index import detect, production_guard, storage
from proxy_index.cli import main
from proxy_index.daily import DAILY, MARKER, audits, batches
from proxy_index.daily import ingest as daily_ingest
from proxy_index.orchestrator.db import Database
from proxy_index.storage import connect, seed

pytest_plugins = ["test_storage"]

HOUR = datetime(2027, 1, 1, 13, tzinfo=UTC)
AUDITS = ("new_hosting_asn", "ip_overlap")


def cell(network, region, directories):
    """A stored-to-be cell as prepare_cell leaves it: one completed country per run directory."""
    countries = [batches.Country(code, {"unique_ips": 1}) for code in directories]
    for country in countries:
        country.directory = directories[country.code]
    report = {"network": network, "region": region, "status": "failed", "unique_ips": 0}
    report |= {"countries": list(directories), "requests": 4}
    report |= {"started_at": HOUR.isoformat(), "finished_at": HOUR.isoformat()}
    report |= {name: [] for name in ("ingested", "issues", "estimated", "estimate_issues")}
    report["spool_issues"] = []
    manifest = {"provider": network, "product": "residential"}
    return daily_ingest.Cell(f"{network}/{region}", network, manifest, report, countries, True)


def hour_of(db, database, tmp_path, previous):
    path = tmp_path / daily_ingest.REPORT
    return daily_ingest.CampaignHour(db, database, None, path, previous, {"cells": {}}, HOUR)


def audit_events(database):
    with connect(database) as conn:
        return (
            conn.cursor(row_factory=dict_row)
            .execute(
                "SELECT e.kind,p.provider,e.batch_id,e.country_code,e.region_id,e.target_id,"
                "e.signal,e.data FROM test_events e JOIN products p USING(product_id) "
                "WHERE e.kind=ANY(%s) ORDER BY e.event_id",
                (list(AUDITS),),
            )
            .fetchall()
        )


async def test_an_hours_daily_tests_get_their_audits_once(tmp_path, database):
    seed(database)  # the regions, countries and targets the events reference
    alpha_ca, _ = await census(tmp_path, provider="alpha", network=HOSTING)
    alpha_de, _ = await census(tmp_path, provider="alpha", network=HOSTING)
    beta_ca, _ = await census(tmp_path, provider="beta")  # residential, same exit address

    def cells():
        return [
            cell("alpha", "tor1", {"CA": alpha_ca}),
            cell("alpha", "fra1", {"DE": alpha_de}),
            cell("beta", "tor1", {"CA": beta_ca}),
        ]

    db = Database(str(database), size=2)
    try:
        hour = hour_of(db, database, tmp_path, {"cells": {}})
        await daily_ingest.store_cells(hour, cells(), 20)
        assert hour.report["audits"] == 3 and "audit_issue" not in hour.report
        first = audit_events(database)
        # The same runs stored again (the hour's rerun) repeat no audit.
        again = hour_of(db, database, tmp_path, hour.report)
        await daily_ingest.store_cells(again, cells(), 20)
        assert again.report["audits"] == 0 and audit_events(database) == first
    finally:
        await db.close()
    hosting = [e for e in first if e["kind"] == "new_hosting_asn"]
    # Once per network and ASN, although both of alpha's cells landed in it.
    assert [(e["provider"], e["data"]["asn"], e["signal"]) for e in hosting] == [
        ("alpha", 24940, -1)
    ]
    assert hosting[0]["batch_id"] is None and hosting[0]["target_id"] == "generic_http"
    assert hosting[0]["data"]["name"] == "Hetzner Online GmbH"
    overlap = {e["provider"]: e for e in first if e["kind"] == "ip_overlap"}
    assert sorted(overlap) == ["alpha", "beta"] and len(first) == 3  # once per network
    assert overlap["alpha"]["data"] == {
        "value": 1.0,
        "shared_ips": 1,
        "unique_ips": 1,
        "peers": {"beta": 1},
        "hour": "2027-01-01T13:00:00.000000+00:00",
        "runs": 2,
    }
    assert overlap["beta"]["data"]["peers"] == {"alpha": 1} and overlap["beta"]["signal"] == 0
    assert overlap["beta"]["batch_id"] is None and overlap["beta"]["country_code"] is None


async def test_runs_not_stored_get_no_audit(database):
    with connect(database) as conn:
        assert audits.audit(conn, {str(uuid.uuid4()): ("CA", "tor1")}, HOUR, HOUR) == 0


async def test_an_audit_the_database_refuses_is_an_issue_of_the_hour(tmp_path, monkeypatch):
    def refused(*_):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(audits, "stored", refused)
    hour = hour_of(None, tmp_path / "credentials.toml", tmp_path, {"cells": {}})
    await daily_ingest._audit(hour, [cell("alpha", "tor1", {"CA": tmp_path / str(uuid.uuid4())})])
    assert hour.report == {"cells": {}, "audit_issue": "audits not written (OperationalError)"}


async def stored_without_audits(tmp_path, database, monkeypatch):
    """The test above's runs stored as daily runs of the hour they were measured in (the
    marker, a country, a start at the hour) while the ingest's audit step wrote nothing, as on
    the real two days (AUDIT-DAILY-2); returns that hour. The hosting check reads the runs'
    buckets between their start and end, so the hour is the real one, not HOUR."""
    seed(database)
    alpha_ca, _ = await census(tmp_path, provider="alpha", network=HOSTING, requested_country="CA")
    alpha_de, _ = await census(tmp_path, provider="alpha", network=HOSTING, requested_country="DE")
    beta_ca, _ = await census(tmp_path, provider="beta", requested_country="CA")
    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    cells = [
        cell("alpha", "tor1", {"CA": alpha_ca}),
        cell("alpha", "fra1", {"DE": alpha_de}),
        cell("beta", "tor1", {"CA": beta_ca}),
    ]
    for each in cells:  # the cells' daily_test_started events, in the hour
        each.report |= {"started_at": hour.isoformat(), "finished_at": hour.isoformat()}
    db = Database(str(database), size=2)
    try:
        with monkeypatch.context() as patched:
            patched.setattr(audits, "stored", lambda *_: 0)
            path = tmp_path / daily_ingest.REPORT
            stored = daily_ingest.CampaignHour(
                db, database, None, path, {"cells": {}}, {"cells": {}}, hour
            )
            await daily_ingest.store_cells(stored, cells, 20)
    finally:
        await db.close()
    with connect(database) as conn:
        conn.execute(
            "UPDATE test_runs SET summary=summary||%s::jsonb,started_at=%s",
            (json.dumps({MARKER: DAILY}), hour),
        )
    return hour


def backfill(database, hour, *options):
    span = ["--from", (hour - timedelta(hours=1)).isoformat()]
    span += ["--to", (hour + timedelta(hours=1)).isoformat()]
    return main(["daily", "audits", "--database-file", str(database), *span, *options])


async def test_stored_hours_get_the_ingests_audits_once(tmp_path, database, monkeypatch, capsys):
    hour = await stored_without_audits(tmp_path, database, monkeypatch)
    assert backfill(database, hour, "--dry-run") == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry | {"seconds": 0} == {
        "hour": hour.isoformat(),
        "runs": 3,
        "unmapped": 0,
        "events": 3,
        "seconds": 0,
    }
    assert audit_events(database) == []  # the dry run wrote nothing
    assert backfill(database, hour) == 0
    assert json.loads(capsys.readouterr().out)["events"] == 3
    written = audit_events(database)
    overlap = {e["provider"]: e["data"] for e in written if e["kind"] == "ip_overlap"}
    assert overlap["alpha"]["hour"] == detect.timestamp(hour)  # the ingest's stamp
    assert overlap["alpha"]["peers"] == {"beta": 1} and overlap["alpha"]["runs"] == 2
    assert [e["kind"] for e in written].count("new_hosting_asn") == 1
    # Again, or after the ingest's own call for the hour: nothing more.
    assert backfill(database, hour) == 0
    assert json.loads(capsys.readouterr().out)["events"] == 0
    assert audit_events(database) == written


async def test_a_run_without_its_cells_region_is_left_out(tmp_path, database, monkeypatch):
    hour = await stored_without_audits(tmp_path, database, monkeypatch)
    later = hour + timedelta(hours=1)
    with connect(database) as conn:
        conn.execute(
            "DELETE FROM test_events WHERE kind='daily_test_started' AND data->>'region'='fra1'"
        )
        [(found, tested, unmapped)] = audits.stored_hours(conn, hour, later)
        assert found == hour and len(unmapped) == 1
        assert sorted(tested.values()) == [("CA", "tor1"), ("CA", "tor1")]
        assert audits.stored_hours(conn, later, later + timedelta(hours=1)) == []


class Identity:
    """A connection that only answers current_database() and current_user."""

    def __init__(self, database):
        self.database = database

    def execute(self, query):
        assert query == "SELECT current_database(),current_user"
        return self

    def fetchone(self):
        return self.database, "proxy_index"


def test_production_needs_the_three_of_the_guard(monkeypatch, capsys):
    go = f"owner-go PRODUCTION-AS-STAGING {datetime.now(UTC).date()}"
    assert audits.target_refusal(Identity("proxy_reviews_staging"), None, False) is None
    monkeypatch.delenv(production_guard.ALLOW, raising=False)
    found = audits.target_refusal(Identity("proxy_reviews"), go, True)
    assert found.endswith(f"{production_guard.ALLOW} is not 1")
    assert "--i-know-this-is-production is missing" in audits.target_refusal(
        Identity("proxy_reviews"), go, False
    )
    monkeypatch.setenv(production_guard.ALLOW, "1")
    assert "is not 'owner-go" in audits.target_refusal(Identity("proxy_reviews"), "go", True)
    assert audits.target_refusal(Identity("proxy_reviews"), go, True) is None
    assert "*** PRODUCTION DATABASE 'proxy_reviews'" in capsys.readouterr().err


def test_production_refused_exits_2(tmp_path, monkeypatch, capsys):
    production = contextlib.nullcontext(Identity("proxy_reviews"))
    monkeypatch.setattr(storage, "connect", lambda _: production)
    assert backfill(tmp_path / "database.toml", HOUR) == 2
    assert capsys.readouterr().err.startswith("refused: 'proxy_reviews' is production's")


def test_times_need_their_offset(capsys):
    with pytest.raises(SystemExit):
        main(["daily", "audits", "--database-file", "x", "--from", "2027-01-01T13:00"])
    assert "--from" in capsys.readouterr().err
