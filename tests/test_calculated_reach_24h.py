"""COUNTRIES-24H-SPAN-1: the 24h window reaches back to each network's country's
latest daily test when it counts and started in the hour before the window (verdicts.REACH): a
country tested at HH:02 yesterday and again at HH:02 today, whose test of today is not imported
yet at the HH:06 build, counts in 24h as it does in 7d and 30d.
"""

from datetime import UTC, datetime

import pytest
from calculated_cases import lenient_policy
from census_pulse_cases import product
from test_calculated_country_verdicts import completed, verdict
from test_calculated_hourly_reads import BUCKET, HOUR, floor_hour
from test_calculated_incremental import assert_equal, history
from test_country_spans import POLICY, at
from test_country_spans import verdict as stored

from atlas import verdicts
from atlas.calculated import target_scores as ts
from atlas.storage import connect

pytest_plugins = ["test_storage"]
pytestmark = pytest.mark.country_verdicts

PLACES = ("unique_ips", "countries", "regions", "cities", "asns")
MIN_IPS = POLICY["country_min_ips"]  # the shipped minimum


def test_the_reach_is_each_countrys_latest_counting_test_of_the_hour_before(database):
    end = at(3, 12)  # the window is [day 2 12:00, day 3 12:00)
    with connect(database) as conn:
        alpha, beta = product(conn, "alpha"), product(conn, "beta")
        stored(conn, alpha, "DE", at(1, 11) + 2 * BUCKET, 40, 1)  # tested at 11:10 the day before
        stored(conn, alpha, "DE", at(2, 11) + 2 * BUCKET, 40, 2)  # reached: today's not in yet
        stored(conn, alpha, "FR", at(2, 11), 40, 3)  # counts, but its latest found no IP
        stored(conn, alpha, "FR", at(3, 9), 0, 4)  # (never 50 before: not two days in a row)
        stored(conn, alpha, "IT", at(2, 11), 0, 5)  # its latest, no IP: it does not count
        stored(conn, alpha, "NL", at(2, 10) + 11 * BUCKET, 40, 6)  # 25 hours and 5 minutes back
        stored(conn, alpha, "PL", at(2, 12), 40, 7)  # inside the window already
        stored(conn, beta, "DE", at(2, 11) + 11 * BUCKET, 40, 8)  # 11:55: reached
        stored(conn, beta, "DE", end, 40, 9)  # at the cutoff: not before it, so still the latest
        assert verdicts.reach(conn, end, MIN_IPS) == {2, 8}
        # At 12:15 alpha's DE (11:10) is over 25 hours back and beta's DE has a newer test, while
        # PL's test of 12:00 is now before the window.
        assert verdicts.reach(conn, end + 3 * BUCKET, MIN_IPS) == {7}
        assert verdicts.reach(conn, end, 41) == set()  # under a minimum of 41 none counts


@pytest.mark.asyncio
async def test_a_country_tested_in_the_builds_own_hour_counts_in_24h(
    database, tmp_path, monkeypatch
):
    end = floor_hour(datetime.now(UTC)) + HOUR
    monkeypatch.setattr(ts, "MIN_REQUESTS", 1)  # a run has 40 requests
    h = await history(database, tmp_path, end)
    # beta tested DE and FR 20 minutes before the 24h window (yesterday's test of the cutoff's
    # hour; today's is not imported yet). FR's latest test, two hours back, found no IP.
    back = 24 * HOUR + 4 * BUCKET
    runs = {"DE": await h.daily("beta", "de", back), "FR": await h.daily("beta", "fr", back)}
    with connect(database) as conn:
        query = "SELECT run_key,started_at FROM test_runs WHERE run_key=ANY(%s)"
        started = dict(conn.execute(query, (list(runs.values()),)).fetchall())
    for country, key in runs.items():
        verdict(database, "beta", country, started[key], 30, key)
    verdict(database, "beta", "FR", end - 2 * HOUR, 0)
    policy = lenient_policy(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(verdicts, "reach", lambda conn, as_of, rule: set())  # the rule before
        before = assert_equal(database, policy, end)
    after = assert_equal(database, policy, end)  # the incremental build equals the full one
    for country in runs:
        assert pool(before, country, "24h")["unique_ips"] == 0
    # DE counts in 24h with its places and IPs, as in 7d; FR's latest test does not count.
    assert pool(after, "DE", "24h")["unique_ips"] > 0
    assert {k: pool(after, "DE", "24h")[k] for k in PLACES} == {
        k: pool(after, "DE", "7d")[k] for k in PLACES
    }
    assert entries(after, "DE", "24h") == entries(after, "DE", "7d")
    assert pool(after, "FR", "24h")["unique_ips"] == 0
    # Its requests count too (40, as in 7d); before, none did.
    assert completed(before, "beta", "DE", "24h") == 0
    assert completed(after, "beta", "DE", "24h") == completed(after, "beta", "DE", "7d") == 40
    assert completed(after, "beta", "FR", "24h") == 0
    beta = network(after, "24h")
    assert beta["metrics"]["completed"] == network(before, "24h")["metrics"]["completed"] + 40
    # 7d and 30d are as before, for every group and network.
    for part in ("products", "networks"):
        for old, new in zip(before[part], after[part], strict=True):
            assert {k: old["windows"][k] for k in ("7d", "30d")} == {
                k: new["windows"][k] for k in ("7d", "30d")
            }, (part, new["id"])


def group(payload, country):
    """beta's daily-test group of `country`."""
    for p in payload["products"]:
        if p["provider"] == "beta" and (p["criteria"].get("country") or "").upper() == country:
            return p
    raise KeyError(country)


def network(payload, label):
    """beta's network window."""
    return next(n for n in payload["networks"] if n["provider"] == "beta")["windows"][label]


def pool(payload, country, label):
    return group(payload, country)["windows"][label]["pool"]


def entries(payload, country, label):
    """The rows per dimension of the group's window list."""
    return group(payload, country)["windows"][label]["breakdown_entries"]
