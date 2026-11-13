"""OVERLAP-7D-1: every build has the 7d and 30d overlap pairs, the first after a deploy included.

The daily windows' overlaps are counted on the day's first build and carried by the later builds
of that UTC day (network_scores.carried()). A build counts them again when the previous head was
written by another implementation (a deploy or a code change), or has no pair in the window.
"""

import hashlib
from datetime import UTC, datetime

import pytest
from calculated_cases import lenient_policy
from calculated_network_scores_cases import assert_set_rule, place, run
from test_calculated_hourly_reads import BUCKET, DAY, HOUR, PRODUCT_IPS, History, floor_hour

from atlas.calculated import calculator, store
from atlas.calculated.calculator import calculate
from atlas.storage import connect

pytest_plugins = ["test_storage"]

DAILY = ("7d", "30d")


def addresses(conn, count):
    """count new documentation IPv6 addresses' ip ids."""
    return [
        conn.execute(
            "INSERT INTO ip_addresses(ip) VALUES (%s) RETURNING ip_id", (f"2001:db8:7::{n:x}",)
        ).fetchone()[0]
        for n in range(1, count + 1)
    ]


def build(database, policy, as_of):
    """A published build at as_of: its networks by provider and its as_of."""
    found = calculate(database, policy, as_of=as_of, publish=True)
    payload = store.read(database, found["snapshot_id"])
    return {n["provider"]: n for n in payload["networks"]}, payload["as_of"]


def pairs(nets, label):
    return sum(len(n["windows"][label]["overlap"]["peers"]) for n in nets.values())


@pytest.mark.asyncio
async def test_every_build_has_the_daily_pairs_after_a_deploy(database, tmp_path, monkeypatch):
    """A first build, a second the same UTC day (carried) and a third after a simulated deploy
    (a new implementation id) and a gamma run that saw one of beta's IPs: all three have 7d and
    30d pairs equal to a fresh count; the third's counted at its own cutoff."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    h = History(database, tmp_path, end)
    alpha = await run(h, "alpha", 3 * HOUR, 120)
    beta = await run(h, "beta", 3 * HOUR, 120)
    gamma = await run(h, "gamma", 3 * HOUR, 120)
    with connect(database) as conn:
        ips = addresses(conn, 260)
        place(conn, alpha, {end - 3 * HOUR: ips[:100]})
        place(conn, beta, {end - 3 * HOUR: ips[90:190]})
        place(conn, gamma, {end - 3 * HOUR: ips[200:250]})
        conn.execute(PRODUCT_IPS)
    policy = lenient_policy(tmp_path)
    first, counted = build(database, policy, end)
    assert_set_rule(database, first, DAILY, counted)
    second, _ = build(database, policy, end + BUCKET)
    assert_set_rule(database, second, DAILY, counted)
    seen = await run(h, "gamma", 2 * HOUR, 40)
    with connect(database) as conn:
        place(conn, seen, {end - 2 * HOUR: ips[150:151]})
        conn.execute(PRODUCT_IPS)
    monkeypatch.setattr(calculator, "IMPLEMENTATION_ID", hashlib.sha256(b"deploy").hexdigest())
    third, later = build(database, policy, end + 2 * BUCKET)
    assert_set_rule(database, third, DAILY, later)
    for nets in (first, second, third):
        assert all(pairs(nets, label) for label in DAILY)
    assert [p["provider"] for p in third["gamma"]["windows"]["7d"]["overlap"]["peers"]] == ["beta"]


@pytest.mark.asyncio
async def test_a_window_without_pairs_is_counted_again_the_same_day(database, tmp_path):
    """Alpha and beta share IPs 10 days back only: the first build's 7d window has no pair, its
    30d window has. After a beta run saw one of alpha's IPs, the next build of the same UTC day
    and implementation counts the 7d pairs again and carries the 30d ones."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    h = History(database, tmp_path, end)
    old = {p: await run(h, p, 10 * DAY, 120) for p in ("alpha", "beta")}
    new = {p: await run(h, p, 3 * HOUR, 120) for p in ("alpha", "beta")}
    with connect(database) as conn:
        ips = addresses(conn, 260)
        place(conn, old["alpha"], {end - 10 * DAY: ips[:50]})
        place(conn, old["beta"], {end - 10 * DAY: ips[40:90]})
        place(conn, new["alpha"], {end - 3 * HOUR: ips[100:150]})
        place(conn, new["beta"], {end - 3 * HOUR: ips[200:250]})
        conn.execute(PRODUCT_IPS)
    policy = lenient_policy(tmp_path)
    first, counted = build(database, policy, end)
    assert_set_rule(database, first, DAILY, counted)
    assert (pairs(first, "7d"), pairs(first, "30d")) == (0, 2)
    seen = await run(h, "beta", 2 * HOUR, 40)
    with connect(database) as conn:
        place(conn, seen, {end - 2 * HOUR: ips[120:121]})
        conn.execute(PRODUCT_IPS)
    second, later = build(database, policy, end + BUCKET)
    assert_set_rule(database, second, ("7d",), later)
    assert pairs(second, "7d") == 2
    for provider, n in second.items():
        assert n["windows"]["30d"]["overlap"] == first[provider]["windows"]["30d"]["overlap"]
