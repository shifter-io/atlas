"""COUNTRY-MIN-IPS-1 in the calculation: with the methodology's country_verdicts, a network's
country counts only while its latest daily test found 10 unique IPs, so only what was measured
then reaches a figure; the incremental build still equals the full one (the oracle), also when
a verdict is imported after the hours it rules on were rolled up.
"""

from datetime import UTC, datetime

import pytest
from calculated_cases import lenient_policy
from census_pulse_cases import batch, batch_run, outage
from psycopg.rows import dict_row
from test_calculated_hourly_reads import HOUR, floor_hour
from test_calculated_incremental import assert_equal, history
from test_country_spans import COUNTING, at, example

from atlas.calculated import rollups, scheduled, store
from atlas.storage import connect, seed

pytest_plugins = ["test_storage"]
pytestmark = pytest.mark.country_verdicts

RULED = "SELECT counts FROM calculated_distinct WHERE digest=%s"


def verdict(database, provider, country, decided, ips, key=None):
    """A daily test's verdict on `country` for `provider`; key: its run key, else one newer than
    every stored run (a test imported now)."""
    with connect(database) as conn:
        product_id = conn.execute(
            "SELECT product_id FROM products WHERE provider=%s", (provider,)
        ).fetchone()[0]
        if key is None:
            key = conn.execute("SELECT max(run_key)+1000 FROM test_runs").fetchone()[0]
        conn.execute(
            "INSERT INTO country_verdicts(product_id,country_code,decided_at,run_key,unique_ips) "
            "VALUES (%s,%s,%s,%s,%s)",
            (product_id, country, decided, key, ips),
        )


def completed(payload, provider, country, label):
    """The completed requests of a provider's daily-test group of `country` in a window."""
    for p in payload["products"]:
        if p["provider"] == provider and (p["criteria"].get("country") or "").upper() == country:
            return p["windows"][label]["metrics"]["completed"]
    raise KeyError((provider, country))


def test_the_scheduled_inputs_keep_what_was_measured_while_it_counted(database):
    store.initialize(database)
    seed(database)
    alpha, _ = example(database)  # DE counts from day 1 10:00 to day 2 10:00 and from day 3 10:00
    with connect(database) as conn:
        for n in range(96):  # a pulse every hour of days 0 to 3
            pulse = batch(conn, "pulse", at(0, n))
            batch_run(conn, pulse, alpha, at(0, n) + HOUR / 60)
        outage(conn, alpha, at(1, 20), source="pulse")  # still open at the cutoff
    with connect(database) as conn:
        conn.row_factory = dict_row
        found = scheduled.scheduled_inputs(conn, at(4), rule=True)
        assert rollups.scheduled_inputs(conn, at(4), "generic_http", rule=True) == found
        every = scheduled.scheduled_inputs(conn, at(4))
    assert len(every["pulses"]) == len(every["tested"]) == 96 and every["counting"] is None
    # 24 hourly pulses from day 1 10:00 and 14 from day 3 10:00.
    assert len(found["pulses"]) == len(found["tested"]) == 24 + 14
    assert all(any(lo <= p["scheduled_for"] < hi for lo, hi in COUNTING) for p in found["pulses"])
    assert [(o["started_at"], o["ended_at"]) for o in found["outages"]] == [
        (at(1, 20), at(2, 10)),
        (at(3, 10), None),
    ]
    assert [(o["started_at"], o["ended_at"]) for o in every["outages"]] == [(at(1, 20), None)]
    assert [(c["lo"], c["hi"]) for c in found["counting"]] == COUNTING


@pytest.mark.asyncio
async def test_only_what_was_measured_while_the_country_counted_is_calculated(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    # alpha in the US: 100 IPs before its first daily test of the fixture (70 hours back), 6
    # before the ones 46 and 23.5 hours back, 12 before the one 22 hours back. alpha in DE: 50
    # IPs before all of its runs. beta, gamma and delta never had a daily test that counted.
    for n, (back, ips) in enumerate(((71, 100), (47, 6), (23, 12)), 1):
        verdict(database, "alpha", "US", end - back * HOUR, ips, -n)
    verdict(database, "alpha", "DE", end - 60 * HOUR, 50, -4)
    verdict(database, "beta", "US", end - 60 * HOUR, 9, -5)
    policy = lenient_policy(tmp_path)
    payload = assert_equal(database, policy, end)
    # 40 requests a run: alpha US counts its runs of 70 and 22 hours back, not 46 and 23.5.
    assert completed(payload, "alpha", "US", "7d") == 2 * 40
    assert completed(payload, "alpha", "US", "24h") == 40
    assert completed(payload, "alpha", "DE", "7d") == 3 * 40  # 50 hours back (split: 2x40), 3
    assert completed(payload, "beta", "US", "7d") == completed(payload, "delta", "US", "7d") == 0
    assert {r["provider"] for r in payload["runs"]} == {"alpha"} and len(payload["runs"]) == 4
    with connect(database) as conn:
        assert conn.execute(RULED, (rollups.RULE,)).fetchone()[0] == {"on": True}
    # A daily test of DE imported now found 3 IPs 31 hours back: alpha's DE census (30 hours
    # back), pulse and daily test no longer count. The rolled-up hours it rules on are counted
    # again, so the incremental build still equals the full one.
    verdict(database, "alpha", "DE", end - 31 * HOUR, 3)
    payload = assert_equal(database, policy, end)
    assert completed(payload, "alpha", "DE", "24h") == 0
    assert completed(payload, "alpha", "DE", "7d") == 2 * 40  # its run of 50 hours back


@pytest.mark.asyncio
async def test_the_rollups_are_counted_again_when_the_setting_changes(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    assert len(rollups.fill(database, end)) == 720  # without the setting, as before
    assert rollups.fill(database, end) == []
    assert len(rollups.fill(database, end, True)) == 720  # every hour again, once
    assert rollups.fill(database, end, True) == []
    with connect(database) as conn:
        assert conn.execute(RULED, (rollups.RULE,)).fetchone()[0] == {"on": True}
    assert len(rollups.fill(database, end)) == 720
