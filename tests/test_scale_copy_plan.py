"""REPLAY-FIX-2: copy_plan.py copies production's scheduling inputs into staging. Two test
databases: one stands in for production, the other for staging (target.STAGING is its name)."""

import importlib
import json
import sys
from pathlib import Path

import pytest
from orchestrator_cases import prepare, query

from proxy_index.orchestrator.db import DEFAULTS
from proxy_index.storage import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scale"))
copy_plan = importlib.import_module("copy_plan")
target = importlib.import_module("target")

pytest_plugins = ["test_storage"]

URL = "https://example.invalid/"
ADD = "INSERT INTO targets(target_id,url,pulse_enabled) VALUES (%s,%s,%s)"
SCHEDULED = "UPDATE targets SET pulse_interval_minutes=15,pulse_requests=50 WHERE target_id=({})"
READ_ONLY, LEVEL = "transaction_read_only", "transaction_isolation"
PULSE = "SELECT min(target_id) FROM targets WHERE pulse_enabled"
OTHER = next(key for key in DEFAULTS if key.split(".")[0] not in copy_plan.GROUPS)


def execute(database, *statements):
    with connect(database) as conn:
        for statement, params in statements:
            conn.execute(statement, params)


def read(database):
    with connect(database) as conn:
        return copy_plan.read(conn)


@pytest.fixture
def pair(databases, database_template, monkeypatch):
    """(production, staging): seeded, with US, DE and FR enabled in production, US in staging."""
    production, staging = databases(database_template), databases(database_template)
    prepare(production, ("US", "DE", "FR"))
    prepare(staging, ("US",))
    monkeypatch.setattr(target, "STAGING", query(staging, "SELECT current_database()")[0][0])
    return production, staging


def test_staging_plans_as_production_and_a_second_run_changes_nothing(pair, capsys):
    production, staging = pair
    region = "(SELECT min(region_id) FROM runner_regions WHERE region_id<>c.region_id)"
    put = "INSERT INTO settings(key,value) VALUES (%s,%s::jsonb)"
    execute(
        production,
        ("UPDATE countries SET census_time='03:30' WHERE country_code='DE'", None),
        (f"UPDATE countries c SET region_id={region} WHERE country_code='FR'", None),
        (SCHEDULED.format(PULSE), None),
        (ADD, ("production_only", URL, False)),
        (put, ("scheduler.census_enabled", "false")),
        (put, ("pulse.lead_seconds", "20")),
        (put, ("census.every_minutes", "null")),
        (put, (OTHER, json.dumps(DEFAULTS[OTHER]))),
    )
    execute(
        staging,
        (ADD, ("staging_only", URL, True)),
        (put, ("pulse.timeout_ms", "5000")),
    )
    source = read(production)
    assert copy_plan.main(["--from", str(production), "--to", str(staging)]) == 0
    output = capsys.readouterr()
    changed = [json.loads(line) for line in output.out.splitlines()]
    assert output.err == f"{len(changed)} rows changed\n"
    keys = {(change["table"], change["key"]) for change in changed}
    pulse = query(production, PULSE)[0][0]
    assert keys == {
        ("countries", "DE"),
        ("countries", "FR"),
        ("targets", pulse),
        ("targets", "staging_only"),
        ("settings", "scheduler.census_enabled"),
        ("settings", "pulse.lead_seconds"),
        ("settings", "census.every_minutes"),
        ("settings", "pulse.timeout_ms"),
    }
    by_key = {change["key"]: change for change in changed}
    assert by_key["pulse.timeout_ms"] == {
        "table": "settings",
        "key": "pulse.timeout_ms",
        "before": {"value": 5000},
        "after": None,
    }
    assert by_key["census.every_minutes"]["after"] == {"value": None}
    assert by_key["DE"]["after"]["census_time"] == "03:30:00"
    found = read(staging)
    assert found["countries"] == source["countries"] and found["settings"] == source["settings"]
    assert {k: v for k, v in found["targets"].items() if k != "staging_only"} == {
        k: v for k, v in source["targets"].items() if k != "production_only"
    }
    assert found["targets"]["staging_only"]["pulse_enabled"] is False
    assert query(staging, "SELECT count(*) FROM settings WHERE key=%s", (OTHER,)) == [(0,)]
    assert read(production) == source
    assert copy_plan.main(["--from", str(production), "--to", str(staging)]) == 0
    assert capsys.readouterr() == ("", "0 rows changed\n")


def test_only_staging_is_written_and_only_when_it_has_what_production_schedules(pair, capsys):
    production, staging = pair
    before = read(production), read(staging)
    assert copy_plan.main(["--from", str(staging), "--to", str(production)]) == 2
    assert "writes only to" in capsys.readouterr().err
    execute(production, (ADD, ("scheduled", URL, True)))
    assert copy_plan.main(["--from", str(production), "--to", str(staging)]) == 2
    assert "staging lacks what production schedules: targets scheduled" in capsys.readouterr().err
    assert read(staging) == before[1]
    execute(production, ("DELETE FROM targets WHERE target_id='scheduled'", None))
    assert read(production) == before[0]


def test_production_is_read_in_one_read_only_transaction(pair, monkeypatch):
    production, _ = pair
    real, seen = copy_plan.read, []

    def read_once(conn):
        mode = conn.execute(f"SELECT current_setting('{READ_ONLY}'),current_setting('{LEVEL}')")
        seen.append((mode.fetchone(), conn.info.transaction_status.name))
        return real(conn)

    monkeypatch.setattr(copy_plan, "read", read_once)
    assert copy_plan.production(production)["countries"]["US"]["enabled"] is True
    assert seen == [(("on", "repeatable read"), "INTRANS")]
