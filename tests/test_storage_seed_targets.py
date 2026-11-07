"""Browser targets missing from `targets` (a database seeded before they were added): the
browser planner's alert, and `db-seed --only targets`, which adds them and nothing else."""

import json
from datetime import timedelta

import pytest
from orchestrator_browser_cases import (
    HOUR,
    HOURLY,
    LEAD,
    SLOW,
    browser_batches,
    browser_nodes,
    configure,
    settings_file,
)
from orchestrator_cases import prepare, query

from proxy_index.browser.contracts import RUN_TARGETS
from proxy_index.cli import main
from proxy_index.orchestrator import browser
from proxy_index.orchestrator.browser import TARGETS_ALERT, browser_plan
from proxy_index.orchestrator.db import Database
from proxy_index.regions import load_targets
from proxy_index.storage import connect, seed

pytest_plugins = ["test_storage"]

BROWSER = [row["target_id"] for row in load_targets() if row["engine"] == "browser"]
TARGETS = "SELECT * FROM targets ORDER BY target_id"
COUNTRIES = "SELECT * FROM countries ORDER BY country_code"
REGIONS = "SELECT * FROM runner_regions ORDER BY region_id"


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()


@pytest.fixture(autouse=True)
def fresh_cache():
    browser._LOADED.clear()
    yield
    browser._LOADED.clear()


def unseed(database, targets):
    with connect(database) as conn:
        conn.execute("DELETE FROM targets WHERE target_id = ANY(%s)", (list(targets),))


def seed_targets(database):
    return main(["db-seed", "--database-file", str(database), "--only", "targets"])


async def test_missing_browser_targets_alert_and_the_others_are_planned(database, db, tmp_path):
    prepare(database)
    browser_nodes(database)
    await configure(db, settings_file(tmp_path))
    unseed(database, SLOW)

    for minute in range(2):
        result = await browser_plan(db, HOUR + timedelta(minutes=minute))
        assert result["missing"] == list(SLOW)
    assert result == {"opened": [], "created": 0, "networks": 2, "missing": list(SLOW)}
    # The present targets keep their usual place in the slot; the missing ones leave a gap.
    assert [(r[1], r[5]) for r in browser_batches(database)] == [
        (target, HOUR + LEAD + timedelta(minutes=60) * i / 8) for i, target in enumerate(HOURLY)
    ]
    alerts = query(database, "SELECT kind,severity,occurrences,data,acknowledged_at FROM alerts")
    assert alerts == [(TARGETS_ALERT, "major", 2, {"missing": list(SLOW)}, None)]
    (message,) = query(database, "SELECT message FROM alerts")[0]
    assert all(target in message for target in SLOW)
    assert "run `proxy-index db-seed --only targets`" in message

    # Seeded: the missing targets are planned and the alert acknowledged.
    assert seed_targets(database) == 0
    after = await browser_plan(db, HOUR + timedelta(minutes=2))
    assert after == {"opened": list(SLOW), "created": 2, "networks": 2}
    assert [r[1] for r in browser_batches(database)] == list(RUN_TARGETS)
    assert query(database, "SELECT acknowledged_at IS NOT NULL FROM alerts") == [(True,)]


def test_only_targets_adds_the_targets_and_leaves_the_rest(database, capsys):
    seed(database)
    unseed(database, BROWSER)
    with connect(database) as conn:
        conn.execute(
            "UPDATE targets SET pulse_interval_minutes=15,pulse_enabled=false "
            "WHERE target_id='generic_https'"
        )
        conn.execute("UPDATE countries SET census_time='12:34',name='N' WHERE country_code='NO'")
        conn.execute("UPDATE runner_regions SET pattern='manual' WHERE slug='ams3'")
    countries, regions = query(database, COUNTRIES), query(database, REGIONS)
    capsys.readouterr()

    assert seed_targets(database) == 0
    assert json.loads(capsys.readouterr().out) == {
        "targets": [row["target_id"] for row in load_targets()]
    }
    assert len(BROWSER) == 9
    assert query(database, "SELECT target_id FROM targets WHERE engine='browser'") == [
        (target,) for target in BROWSER
    ]
    assert query(
        database,
        "SELECT pulse_interval_minutes,pulse_enabled FROM targets WHERE target_id='generic_https'",
    ) == [(15, False)]
    assert query(database, COUNTRIES) == countries and query(database, REGIONS) == regions
    # A second run changes nothing.
    targets = query(database, TARGETS)
    assert seed_targets(database) == 0
    assert query(database, TARGETS) == targets


def test_only_targets_refuses_reset_times(database, capsys):
    seed_args = ["db-seed", "--database-file", str(database), "--only", "targets"]
    assert main([*seed_args, "--reset-times"]) == 2
    assert "cannot go with --only targets" in capsys.readouterr().err
    assert query(database, "SELECT count(*) FROM targets") == [(0,)]
