"""COUNTRY-MIN-IPS-1 on the tape and in the outages: with the methodology's country_verdicts, a
network's country that does not count (its latest daily test found under 10 unique IPs, or it
has had none) gets no tape event and no outage; a series' baseline holds only its pulses of the
time its country counted; the fast replay still gives the step replay's tape; the network-wide
outage opens only in the countries that count; and the pulse evaluator closes the open outages
of a country that no longer counts, at the verdict, with end_reason 'too_few_ips'.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from calculated_pulse_cases import SETTINGS, T0, after, pulses
from census_pulse_cases import HOSTING, batch, batch_run, product
from orchestrator_cases import prepare, query
from pulse_cases import W0, evaluated, products, run, window
from test_storage_v6 import census
from test_detect_replay import COUNTRIES, END, THIRD, copy, replayed, stored
from test_detect_replay import T0 as REPLAY_T0
from test_pulse_outage_close import S0

from atlas.daily import audits
from atlas.detect import country_rule, detect
from atlas.orchestrator import batch_events, pulse
from atlas.orchestrator.events import PulseWindow
from atlas.storage import connect, ingest, seed

pytest_plugins = ["test_storage", "orchestrator_cases"]
pytestmark = pytest.mark.country_verdicts

TAPE = "SELECT kind,target_id FROM test_events ORDER BY event_id"
OUTAGES = "SELECT country_code,started_at,ended_at,end_reason FROM outages ORDER BY outage_id"


def verdict(conn, product_id, country, decided, ips):
    """A daily test's verdict on `country` for `product_id` (its run key: a fresh negative one)."""
    conn.execute(
        "INSERT INTO country_verdicts(product_id,country_code,decided_at,run_key,unique_ips) "
        "SELECT %s,%s,%s,coalesce(min(run_key),0)-1,%s FROM country_verdicts",
        (product_id, country, decided, ips),
    )


def down_both(conn, alpha):
    """Pulses of both targets in DE, healthy 0 to 5 and with no success 6 to 8."""
    for target in ("generic_http", "generic_https"):
        pulses(conn, alpha, [95] * 6 + [0, 0, 0], target=target)


def test_the_shipped_methodology_turns_the_rule_on():
    assert country_rule() is True


def test_a_country_without_a_counting_daily_test_gets_no_event_and_no_outage(database):
    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        down_both(conn, alpha)
        verdict(conn, alpha, "DE", T0 - timedelta(hours=1), 9)  # 9 IPs: does not count
        detect(conn, after(8), config=SETTINGS)
        assert conn.execute(TAPE).fetchall() == []
        assert conn.execute(OUTAGES).fetchall() == []


def test_a_counting_country_is_judged_and_one_that_stops_counting_is_not(database):
    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        down_both(conn, alpha)
        verdict(conn, alpha, "DE", T0 - timedelta(hours=1), 100)
    with connect(database) as conn:
        detect(conn, after(8), rule=False, config=SETTINGS)  # without the rule: as before
        every = conn.execute(TAPE).fetchall()
        conn.rollback()
    with connect(database) as conn:
        detect(conn, after(8), config=SETTINGS)
        assert conn.execute(TAPE).fetchall() == every  # the country counts: the same tape
        assert ("down", "generic_http") in every
        assert [row[3] for row in conn.execute(OUTAGES)] == [None]
        conn.rollback()
    with connect(database) as conn:
        # A daily test found 6 IPs just before pulse 6: the failing pulses are not judged.
        verdict(conn, alpha, "DE", T0 + timedelta(minutes=170), 6)
        detect(conn, after(8), config=SETTINGS)
        assert {kind for kind, _ in conn.execute(TAPE)} <= {"pulse_finished"}
        assert conn.execute(OUTAGES).fetchall() == []


def test_a_baseline_holds_only_the_pulses_of_the_time_the_country_counted(database):
    """The pulses before the country counted had no success; counted from pulse 3, the series'
    baseline is pulses 3 to 5 (95 %) and pulse 6 without a success is a 'down'."""
    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        for target in ("generic_http", "generic_https"):
            pulses(conn, alpha, [0, 0, 0, 95, 95, 95, 0], target=target)
        verdict(conn, alpha, "DE", T0 + timedelta(minutes=80), 50)
        detect(conn, after(6), config=dict(SETTINGS, min_baseline_batches=3))
        assert ("down", "generic_http") in conn.execute(TAPE).fetchall()
        assert len(conn.execute(OUTAGES).fetchall()) == 1


@pytest.mark.asyncio
async def test_the_fast_replay_gives_the_step_replays_tape_under_the_rule(
    database, databases, tmp_path
):
    ids = await stored(database, tmp_path)
    with connect(database) as conn:
        # Every network counts everywhere but beta in PL (no verdict: its outage never opens)
        # and alpha in DE from slot 11 (6 IPs: no 'recovered' at its comeback).
        for name, pid in ids.items():
            for country in COUNTRIES:
                if (name, country) != ("beta", "PL"):
                    verdict(conn, pid, country, REPLAY_T0 - timedelta(hours=4), 40)
        verdict(conn, ids["alpha"], "DE", REPLAY_T0 + timedelta(hours=11), 6)
    fast, mixed = copy(databases, database), copy(databases, database)
    step = replayed(database, (REPLAY_T0, END, False))
    assert replayed(fast, (REPLAY_T0, END, True)) == step
    assert replayed(mixed, (REPLAY_T0, THIRD, False), (THIRD, END, True)) == step
    events, outages = step
    found = {(kind, product, country.strip()) for kind, _, product, country, *_ in events}
    assert ("down", ids["alpha"], "DE") in found  # slot 10, before the 6-IP verdict
    assert ("recovered", ids["alpha"], "DE") not in found
    assert not any(product == ids["beta"] and country == "PL" for _, product, country in found)
    assert [row[1].strip() for row in outages if row[0] == ids["beta"]] == []


@pytest.mark.asyncio
async def test_a_network_outage_opens_only_where_the_country_counts(database, db):
    prepare(database, ("DE", "FR", "GB"))
    with connect(database) as conn:
        alpha = product(conn, "alpha")
        verdict(conn, alpha, "DE", S0 - timedelta(hours=2), 30)
        verdict(conn, alpha, "FR", S0 - timedelta(hours=2), 30)
        verdict(conn, alpha, "FR", S0 - timedelta(hours=1), 2)  # FR stopped counting; GB none
    window = PulseWindow(
        product_id=alpha,
        window_start=S0,
        window_minutes=5,
        errors=90,
        requests=100,
        regions=2,
        threshold=0.5,
    )
    await db.ensure_partition("test_events", S0)  # pulse_down's
    async with db.transaction() as conn:
        await pulse._open_outage(conn, S0, alpha, window, True)
    assert [row[0] for row in query(database, OUTAGES)] == ["DE"]
    async with db.transaction() as conn:  # without the rule: every enabled country, as before
        await conn.execute("DELETE FROM outages")
        await pulse._open_outage(conn, S0, alpha, window, False)
    assert sorted(row[0] for row in query(database, OUTAGES)) == ["DE", "FR", "GB"]


@pytest.mark.asyncio
async def test_the_evaluator_closes_the_outages_of_a_country_that_stops_counting(database, db):
    prepare(database, ("DE", "FR", "GB"))
    since = S0 - timedelta(hours=3)
    with connect(database) as conn:
        alpha = product(conn, "alpha")
        verdict(conn, alpha, "DE", S0 - timedelta(hours=4), 30)  # counts: stays open
        verdict(conn, alpha, "FR", S0 - timedelta(hours=4), 30)
        verdict(conn, alpha, "FR", S0 - timedelta(hours=1), 2)  # stopped counting an hour ago
        for country in ("DE", "FR", "GB"):  # GB: never a daily test
            conn.execute(
                "INSERT INTO outages(product_id,country_code,region_id,source,started_at) "
                "SELECT %s,country_code,region_id,'pulse',%s FROM countries WHERE country_code=%s",
                (alpha, since, country),
            )
    now = S0 + timedelta(minutes=30)
    assert (await pulse.evaluate(db, now))["closed"] == 2
    assert query(database, OUTAGES) == [
        ("DE", since, None, None),
        ("FR", since, S0 - timedelta(hours=1), "too_few_ips"),
        ("GB", since, since, "too_few_ips"),
    ]
    assert (await pulse.evaluate(db, now))["closed"] == 0  # idempotent
    assert query(database, "SELECT count(*) FROM test_events")[0][0] == 0


@pytest.mark.asyncio
async def test_the_network_window_and_latency_pairs_count_only_the_countries_that_count(
    database, db, tmp_path
):
    """alpha fails at 80 % in DE, GB and NL and is healthy in the US (enough for a network
    outage, test_pulse); only the US counts, so the window is healthy and opens nothing."""
    prepare(database, ("DE", "GB", "NL", "US"))
    (alpha,) = products(database, "alpha")
    for country in ("DE", "GB", "NL", "US"):
        errors = 0 if country == "US" else 80
        rows = [(window(0), 100, errors, 300), (window(1), 100, 0, 300)]
        await run(tmp_path, database, "pulse", W0, alpha, country, rows)
    with connect(database) as conn:
        verdict(conn, alpha, "US", W0 - timedelta(hours=1), 30)
    async with db.transaction() as conn:
        cfg = await pulse._settings(conn)
        assert cfg["verdicts"] is True
        pairs = await (await conn.execute(pulse.PAIRS_SQL, pulse._range(window(0), cfg))).fetchall()
        stats = await pulse.window_stats(conn, window(0), 0.5, cfg)
    assert [p["country_code"] for p in pairs] == ["US"]
    assert stats[alpha] == {"requests": 100, "errors": 0, "regions": 1, "over": 0}
    assert (await evaluated(db, 0))["opened"] == 0
    assert query(database, OUTAGES) == []


@pytest.mark.asyncio
async def test_a_stalled_census_opens_an_outage_only_where_its_country_counts(database, db):
    prepare(database, ("DE", "FR"))
    with connect(database) as conn:
        alpha = product(conn, "alpha")
        verdict(conn, alpha, "DE", S0 - timedelta(hours=2), 9)  # does not count
        verdict(conn, alpha, "FR", S0 - timedelta(hours=2), 30)
        stalled = {}
        for country in ("DE", "FR"):
            stalled[country] = batch(conn, "census", S0, country=country, minutes=20)
            batch_run(conn, stalled[country], alpha, S0 + timedelta(minutes=20))
            conn.execute(
                "UPDATE batch_runs SET stop_reason='stalled' WHERE batch_id=%s",
                (stalled[country],),
            )
    summary = {"outage_started_at": (S0 + timedelta(minutes=5)).isoformat()}
    await db.ensure_partition("test_events", S0)
    async with db.transaction() as conn:
        assert not await batch_events.census_down(conn, stalled["DE"], alpha, summary)
        assert await batch_events.census_down(conn, stalled["FR"], alpha, summary)
    assert [row[0] for row in query(database, OUTAGES)] == ["FR"]
    assert query(database, "SELECT kind,country_code FROM test_events") == [("down", "FR")]


@pytest.mark.asyncio
async def test_the_daily_audits_skip_a_daily_test_that_does_not_count(tmp_path, database):
    """Two networks' daily tests of one hour share their exit address: only the one whose own
    verdict counts is audited, so no overlap is left to report and only its hosting ASN is."""
    seed(database)
    alpha, _ = await census(tmp_path, provider="alpha", network=HOSTING, requested_country="ca")
    beta, _ = await census(tmp_path, provider="beta", network=HOSTING, requested_country="ca")
    for directory in (alpha, beta):
        path = directory / "summary.json"
        path.write_text(json.dumps(json.loads(path.read_text()) | {"test_kind": "daily"}))
    keys = {name: ingest(d, database)["run_key"] for name, d in (("alpha", alpha), ("beta", beta))}
    with connect(database) as conn:
        conn.execute("UPDATE country_verdicts SET unique_ips=40 WHERE run_key=%s", (keys["alpha"],))
        conn.execute("UPDATE country_verdicts SET unique_ips=3 WHERE run_key=%s", (keys["beta"],))
        tested = {
            run_id: ("CA", "tor1")
            for (run_id,) in conn.execute("SELECT run_id::text FROM test_runs ORDER BY run_key")
        }
        hour = conn.execute("SELECT date_trunc('hour',min(started_at)) FROM test_runs").fetchone()[0]
        assert audits.audit(conn, tested, hour, hour + timedelta(hours=1)) == 1
        found = conn.execute(
            "SELECT e.kind,p.provider FROM test_events e JOIN products p USING(product_id)"
        ).fetchall()
    assert found == [("new_hosting_asn", "alpha")]
