"""AUDIT-DAILY-1: the live tape's audits for an hour's daily tests, against an isolated PostgreSQL.

A daily test has no batch, so detect() never audits it; the daily ingest writes its
new_hosting_asn and ip_overlap events with detect's checks once the hour's cells are stored.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import psycopg
from census_pulse_cases import HOSTING
from psycopg.rows import dict_row
from test_storage_v6 import census

from proxy_index.daily import audits, batches
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
