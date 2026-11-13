"""INGEST-BATCH-2: a daily campaign hour stored in batches, against an isolated PostgreSQL.

compute.estimate_runs stores what estimate_run stores one run at a time, in a fixed number of
round trips; ingest_campaign stores its countries through storage.ingest_many and
compute.estimate_runs, and a batch that fails is retried one country at a time.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import psycopg
import pytest
from daily_cases import daily_root
from estimates_cases import T1, formula, imported
from ingest_cases import Proxy, proxied
from test_onboarding import START

from atlas import storage
from atlas.cli import main
from atlas.daily import ingest as daily_ingest
from atlas.estimates import compute
from atlas.orchestrator.db import Database
from atlas.providers.credentials import read_credentials
from atlas.storage import IngestBatchError, connect, seed

pytest_plugins = ["test_storage", "calculated_template", "test_enrichment_cli", "estimates_cases"]


@pytest.fixture
def database(calculated_database):
    """The estimated hours need the current calculated schema, copied from its template."""
    return calculated_database


def estimated(database):
    """Every stored estimated hour, all columns but computed_at."""
    with connect(database) as conn:
        return conn.execute(
            "SELECT product_id,country_code,day,hour,run_key,measured_at,measured_ips,"
            "measured_requests,hour_factor,unique_ips,requests,day_unique_ips,curve,"
            "formula_version,released_at FROM estimated_hours ORDER BY 1,2,3,4"
        ).fetchall()


async def test_a_batch_of_runs_stores_what_one_at_a_time_stores(tmp_path, database):
    seed(database)
    first = await imported(tmp_path, database, T1)
    later = await imported(tmp_path, database, T1 + timedelta(hours=1))
    older = await imported(tmp_path, database, T1 - timedelta(hours=2))
    runs = [
        (first, "DE", formula(DE={})),
        (later, "DE", formula(2, DE={})),  # 22 of first's hours, newer
        (older, "DE", formula(DE={})),  # 2 hours no newer test covers
        (first, "FR", formula(FR={})),
    ]
    assert [compute.estimate_run(database, *run) for run in runs] == [23, 23, 2, 23]
    alone = estimated(database)
    with connect(database) as conn:
        conn.execute("DELETE FROM estimated_hours")
    # An hour two runs of the batch both cover counts for the run that keeps it.
    assert compute.estimate_runs(database, runs) == [1, 23, 2, 23]
    assert estimated(database) == alone and len(alone) == 23 + 1 + 2 + 23
    assert compute.estimate_runs(database, []) == []


async def test_a_batch_with_a_run_not_imported_stores_nothing(tmp_path, database):
    seed(database)
    first = await imported(tmp_path, database, T1)
    runs = [(first, "DE", formula(DE={})), (str(uuid.uuid4()), "DE", formula(DE={}))]
    with pytest.raises(ValueError, match="not imported"):
        compute.estimate_runs(database, runs)
    with pytest.raises(ValueError, match="FR is not in the formula"):  # fails the whole batch
        compute.estimate_runs(database, [runs[0], (first, "FR", formula(DE={}))])
    assert estimated(database) == []


async def test_a_batch_of_runs_takes_the_round_trips_of_one(tmp_path, database):
    seed(database)
    runs = [await imported(tmp_path, database, T1 + timedelta(hours=n)) for n in range(4)]
    values = read_credentials(database, "postgres")
    found = []
    with Proxy(values["host"], values["port"]) as proxy:
        credentials = proxied(values, proxy, tmp_path)
        for chosen in (runs[:1], runs[1:]):
            proxy.reset()
            compute.estimate_runs(credentials, [(run, "DE", formula(DE={})) for run in chosen])
            found.append((proxy.round_trips(), proxy.statements()))
    assert found[0] == found[1] and found[0][0] <= 3


def spy(monkeypatch, module, name, calls, fail=None):
    """Record each call's name and run count; `fail(argument)` may raise instead."""
    real = getattr(module, name)

    def wrapper(credentials_or_directory, *args, **options):
        batch = args[0] if name.endswith("_runs") else credentials_or_directory
        calls.append((name, len(batch) if isinstance(batch, list) else 1))
        if fail:
            fail(batch)
        return real(credentials_or_directory, *args, **options)

    monkeypatch.setattr(module, name, wrapper)


@pytest.fixture
async def hour(tmp_path, monkeypatch, database, fake_database):
    """A collected daily test (shifter: fra1 unreadable, tor1 with CA and PM), its formulas
    (both countries estimated), the DB-IP file and a pool; ingest(**options) imports it."""
    import atlas.runner as runner

    monkeypatch.setattr(runner.time, "time", lambda: START.timestamp() + 1)
    seed(database)
    root = await daily_root(tmp_path)
    folder = tmp_path / "formulas"
    folder.mkdir()
    (folder / "shifter.json").write_text(json.dumps(formula(CA={}, PM={})))
    mmdb, _ = fake_database
    db = Database(str(database), size=1)

    async def ingest(**options):
        options = {"database": db, "formulas": folder} | options
        return await daily_ingest.ingest_campaign(root, database, mmdb, **options)

    yield ingest
    await db.close()


async def test_a_campaign_hour_goes_in_batches(monkeypatch, hour):
    calls = []
    spy(monkeypatch, storage, "ingest_many", calls)
    spy(monkeypatch, storage, "ingest", calls)
    spy(monkeypatch, compute, "estimate_runs", calls)
    report = await hour()
    assert calls == [("ingest_many", 2), ("estimate_runs", 2)]
    tor1, fra1 = report["cells"]["shifter/tor1"], report["cells"]["shifter/fra1"]
    assert (tor1["status"], tor1["ingested"]) == ("done", ["CA", "PM"])
    assert tor1["estimated"] == ["CA", "PM"] and tor1["estimate_issues"] == []
    # fra1 comes first in the same batch, before shifter's product exists: as cell by cell,
    # its events wait for the rerun; tor1's own import lets it have its pair.
    assert (fra1["events"], tor1["events"]) == (False, True)
    calls.clear()
    # A batch per country, one connection: the rerun replays both, one after the other.
    again = await hour(batch=1, connections=1)
    assert calls == [("ingest_many", 1), ("estimate_runs", 1)] * 2
    assert again["cells"]["shifter/tor1"]["estimated"] == ["CA", "PM"]
    assert again["cells"]["shifter/fra1"]["events"] is True
    with pytest.raises(ValueError, match="at least one"):
        await hour(batch=0)
    with pytest.raises(ValueError, match="at least one connection"):
        await hour(connections=0)
    with pytest.raises(ValueError, match="at least one process"):
        await hour(processes=0)
    with pytest.raises(TypeError, match="unexpected sizes"):
        await hour(batches=3)


async def test_a_failed_batch_is_retried_one_country_at_a_time(monkeypatch, hour, database):
    calls = []

    def refuse(batch):
        raise IngestBatchError([path.name for path in batch])

    def offline(directory):
        if calls[-1] == ("ingest", 1) and calls.count(("ingest", 1)) == 2:  # PM, after CA
            raise psycopg.OperationalError("connection lost")

    spy(monkeypatch, storage, "ingest_many", calls, refuse)
    spy(monkeypatch, storage, "ingest", calls, offline)
    report = await hour()
    assert calls == [("ingest_many", 2), ("ingest", 1), ("ingest", 1)]
    tor1 = report["cells"]["shifter/tor1"]
    assert (tor1["status"], tor1["ingested"], tor1["estimated"]) == ("failed", ["CA"], ["CA"])
    assert tor1["issues"] == ["PM: not imported (OperationalError)"]
    assert tor1["events"] is True  # CA's import made the product row
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM test_runs").fetchone()[0] == 1


async def test_a_product_lookup_error_leaves_the_events_for_a_rerun(monkeypatch, hour):
    async def broken(db, manifest):
        raise psycopg.OperationalError("connection lost")

    monkeypatch.setattr(daily_ingest, "_product_id", broken)
    report = await hour()
    tor1 = report["cells"]["shifter/tor1"]
    assert tor1["ingested"] == ["CA", "PM"] and tor1["events"] is False
    assert tor1["issues"] == ["events not emitted (OperationalError)"]


def test_the_cli_passes_the_batch_size_and_connections(tmp_path, monkeypatch, capsys):
    import atlas.campaign.collect as collect

    seen = []

    async def fake_ingest(root, credentials, mmdb, **options):
        seen.append((options.get("batch"), options.get("connections"), options.get("processes")))
        return {"campaign_id": "c", "complete": True}

    monkeypatch.setattr(daily_ingest, "ingest_campaign", fake_ingest)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / collect.REPORT).write_text(json.dumps({"clean": True}))
    args = ["daily", "ingest", "--inventory", str(tmp_path / "inv"), "--campaign-id", "c"]
    args += ["--dest", str(dest), "--database-file", str(tmp_path / "db.toml")]
    sizes = ["--batch", "5", "--connections", "3", "--processes", "2"]
    assert main([*args, *sizes]) == 0 and main(args) == 0
    assert seen == [(5, 3, 2), (None, None, None)]
