"""COUNTRY-MIN-IPS-1 in the calculation: with the methodology's country_verdicts, a network's
country counts only while its latest daily test found the methodology's country_min_ips unique
IPs, so only what was measured then reaches a figure; the incremental build still equals the
full one (the oracle), also when a verdict is imported after the hours it rules on were rolled
up, and when the minimum changes. The examples read under test_country_spans.RULE (10).
"""

import re
from datetime import UTC, datetime

import pytest
from calculated_cases import lenient_policy
from census_pulse_cases import batch, batch_run, outage
from psycopg.rows import dict_row
from test_calculated_hourly_reads import HOUR, floor_hour
from test_calculated_incremental import assert_equal, history
from test_country_spans import COUNTING, DAY, RULE, at, example

from atlas import verdicts
from atlas.calculated import rollups, scheduled, store
from atlas.calculated import target_scores as ts
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


def ruled_policy(tmp_path, minimum=RULE):
    """lenient_policy() with the methodology's country_min_ips set to `minimum`."""
    policy = lenient_policy(tmp_path)
    line = f"country_min_ips = {minimum}"
    text = re.sub(r"(?m)^country_min_ips = \d+$", line, policy.read_text())
    assert f"country_min_ips = {minimum}" in text
    policy.write_text(text)
    return policy


def completed(payload, provider, country, label):
    """The completed requests of a provider's daily-test group of `country` in a window."""
    for p in payload["products"]:
        if p["provider"] == provider and (p["criteria"].get("country") or "").upper() == country:
            return p["windows"][label]["metrics"]["completed"]
    raise KeyError((provider, country))


def statuses(payload, provider):
    """{country: (status, unique IPs)} of a network's requested countries (criteria)."""
    for n in payload["networks"]:
        if n["provider"] == provider:
            found = n["criteria"]["country_status"]
            return {c["country"]: (c["status"], c["unique_ips"]) for c in found}
    raise KeyError(provider)


def test_the_scheduled_inputs_keep_what_was_measured_while_it_counted(database):
    store.initialize(database)
    seed(database)
    alpha, _ = example(database)  # DE counts from day 1 10:00 to day 2 10:00 and from day 3 10:00
    with connect(database) as conn:
        for n in range(96):  # a pulse every hour of days 0 to 3
            pulse = batch(conn, "pulse", at(0, n))
            batch_run(conn, pulse, alpha, at(0, n) + HOUR / 60)
        outage(conn, alpha, at(1, 20), source="pulse")  # still open at the cutoff
        # FR counts since 50 days back: its outage of 40 days back keeps its start.
        conn.execute(
            "INSERT INTO country_verdicts(product_id,country_code,decided_at,run_key,unique_ips) "
            "VALUES (%s,%s,%s,%s,%s)",
            (alpha, "FR", at(0) - 50 * DAY, -20, 30),
        )
        outage(conn, alpha, at(0) - 40 * DAY, country="FR", source="pulse")
    with connect(database) as conn:
        conn.row_factory = dict_row
        found = scheduled.scheduled_inputs(conn, at(4), rule=RULE)
        assert rollups.scheduled_inputs(conn, at(4), "generic_http", rule=RULE) == found
        every = scheduled.scheduled_inputs(conn, at(4))
    assert len(every["pulses"]) == len(every["tested"]) == 96 and every["counting"] is None
    # 24 hourly pulses from day 1 10:00 and 14 from day 3 10:00.
    assert len(found["pulses"]) == len(found["tested"]) == 24 + 14
    assert all(any(lo <= p["scheduled_for"] < hi for lo, hi in COUNTING) for p in found["pulses"])
    assert [(o["country"], o["started_at"], o["ended_at"]) for o in found["outages"]] == [
        ("FR", at(0) - 40 * DAY, None),
        ("DE", at(1, 20), at(2, 10)),
        ("DE", at(3, 10), None),
    ]
    assert [(o["started_at"], o["ended_at"]) for o in every["outages"]] == [
        (at(0) - 40 * DAY, None),
        (at(1, 20), None),
    ]
    assert [(c["lo"], c["hi"]) for c in found["counting"] if c["country"] == "DE"] == COUNTING


@pytest.mark.asyncio
async def test_only_what_was_measured_while_the_country_counted_is_calculated(
    database, tmp_path, monkeypatch
):
    end = floor_hour(datetime.now(UTC)) + HOUR
    monkeypatch.setattr(ts, "MIN_REQUESTS", 1)  # a run has 40 requests
    await history(database, tmp_path, end)
    # alpha in the US: 40 IPs before its first daily test of the fixture (70 hours back), 6
    # before the ones 46 and 23.5 hours back, 12 before the one 22 hours back. alpha in DE: 45
    # IPs before all of its runs. beta, gamma and delta never had a daily test that counted.
    # Neither 40 nor 45 is normally big, so two days in a row never applies.
    for n, (back, ips) in enumerate(((71, 40), (47, 6), (23, 12)), 1):
        verdict(database, "alpha", "US", end - back * HOUR, ips, -n)
    verdict(database, "alpha", "DE", end - 60 * HOUR, 45, -4)
    verdict(database, "beta", "US", end - 60 * HOUR, 9, -5)
    policy = ruled_policy(tmp_path)
    payload = assert_equal(database, policy, end)
    # 40 requests a run: alpha US counts its runs of 70 and 22 hours back, not 46 and 23.5.
    assert completed(payload, "alpha", "US", "7d") == 2 * 40
    assert completed(payload, "alpha", "US", "24h") == 40
    assert completed(payload, "alpha", "DE", "7d") == 3 * 40  # 50 hours back (split: 2x40), 3
    assert completed(payload, "beta", "US", "7d") == completed(payload, "delta", "US", "7d") == 0
    assert {r["provider"] for r in payload["runs"]} == {"alpha"} and len(payload["runs"]) == 4
    # Each network's requested countries with their latest daily test's verdict.
    assert statuses(payload, "alpha") == {"DE": ("counts", 45), "US": ("counts", 12)}
    assert statuses(payload, "beta") == {"US": ("too_few_ips", 9)}
    assert statuses(payload, "delta") == {"US": ("no_daily_test", None)}
    # SUCCESS-DAILY-DISTANCE-1: only alpha has a daily test that counted when it started, so only
    # alpha has a success score; beta's (9 IPs) and delta's (no verdict) are left out.
    overall = {n["provider"]: n["windows"]["7d"]["success_overall"] for n in payload["networks"]}
    assert overall["alpha"]["targets"] == {"generic_http": 10}
    assert (overall["alpha"]["rank"], overall["alpha"]["ranked_of"]) == (1, 1)
    assert {"beta", "delta"} <= set(overall)
    for provider in set(overall) - {"alpha"}:
        assert overall[provider]["status"] == ts.NO_RANKED_TARGETS, provider
    with connect(database) as conn:
        assert conn.execute(RULED, (rollups.RULE,)).fetchone()[0] == verdicts.params(RULE)
    # A daily test of DE imported now found 3 IPs 31 hours back: alpha's DE census (30 hours
    # back), pulse and daily test no longer count. The rolled-up hours it rules on are counted
    # again, so the incremental build still equals the full one.
    verdict(database, "alpha", "DE", end - 31 * HOUR, 3)
    payload = assert_equal(database, policy, end)
    assert completed(payload, "alpha", "DE", "24h") == 0
    assert statuses(payload, "alpha")["DE"] == ("too_few_ips", 3)
    assert completed(payload, "alpha", "DE", "7d") == 2 * 40  # its run of 50 hours back
    # The same stored verdicts under a minimum of 1 (COUNTRY-MIN-IPS-1, #1253): alpha's US tests
    # of 6 IPs and its DE test of 3 count now, and beta's of 9; everything measured then is in
    # every figure, and the incremental build, its memos counted again, equals the full one.
    payload = assert_equal(database, ruled_policy(tmp_path, 1), end)
    assert completed(payload, "alpha", "US", "7d") == 4 * 40
    assert completed(payload, "alpha", "DE", "24h") > 0
    assert statuses(payload, "alpha")["DE"] == ("counts", 3)
    assert statuses(payload, "beta") == {"US": ("counts", 9)}
    with connect(database) as conn:
        assert conn.execute(RULED, (rollups.RULE,)).fetchone()[0] == verdicts.params(1)


@pytest.mark.asyncio
async def test_the_rollups_are_counted_again_when_the_setting_changes(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    assert len(rollups.fill(database, end)) == 720  # without the setting, as before
    assert rollups.fill(database, end) == []
    assert len(rollups.fill(database, end, RULE)) == 720  # every hour again, once
    assert rollups.fill(database, end, RULE) == []
    with connect(database) as conn:
        assert conn.execute(RULED, (rollups.RULE,)).fetchone()[0] == verdicts.params(RULE)
    assert len(rollups.fill(database, end, 2)) == 720  # another minimum: again, once
    assert rollups.fill(database, end, 2) == []
    with connect(database) as conn:  # a memo of #1137's (no minimum recorded) is another rule
        conn.execute(
            "UPDATE calculated_distinct SET counts='{\"on\": true}' WHERE digest=%s",
            (rollups.RULE,),
        )
    assert len(rollups.fill(database, end, 2)) == 720
    assert len(rollups.fill(database, end)) == 720
