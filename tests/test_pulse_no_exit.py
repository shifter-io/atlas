"""PULSE-NOEXIT-OUTAGE-1 (#1204): a pulse whose answers are the network's own "no exit" statuses
(its grammar's no_inventory_statuses: Evomi 409 and 500, IPRoyal 503) opens no outage. detect
(step and fast replay) records the country's first one of a streak as a pulse_finished with
reason "no_exits" and leaves them out of later baselines; the pulse evaluator leaves them out of
its windows. Timeouts, other networks' statuses, login refusals, a network-wide problem and a
normally big country's first hours stay visible."""

import json
from datetime import timedelta

import pytest
from calculated_pulse_cases import T0, after, pulses
from census_pulse_cases import product
from orchestrator_cases import prepare, query
from pulse_cases import evaluated, products, run, tape, window
from test_pulse_country_verdicts import verdict
from test_storage import measurement

from atlas import pulse_no_exit
from atlas.daily import tape as daily_tape
from atlas.storage import connect, ingest, seed

pytest_plugins = ["test_storage", "orchestrator_cases"]

JUDGED = (
    "SELECT product_id,kind,data->>'reason' FROM test_events WHERE kind IN ('down',"
    "'success_drop','recovered','pulse_finished') ORDER BY event_id"
)
OUTAGES = "SELECT product_id,country_code,ended_at IS NULL FROM outages ORDER BY outage_id"
NO_EXIT = {"409": 100}  # every answer Evomi's "no exit"


async def stored(tmp_path, database, answers):
    """A stored 100-request run per entry, whose summary has these answers: statuses (digits)
    and errors (the rest)."""
    keys = []
    for i, found in enumerate(answers):
        key = ingest(await measurement(tmp_path / f"run-{i}"), database)["run_key"]
        statuses = {code: n for code, n in found.items() if code.isdigit()}
        errors = {code: n for code, n in found.items() if not code.isdigit()}
        with connect(database) as conn:
            conn.execute(
                "UPDATE test_runs SET summary=summary||jsonb_build_object('completed',100,"
                "'status_codes',%s::jsonb,'errors',%s::jsonb) WHERE run_key=%s",
                (json.dumps(statuses), json.dumps(errors), key),
            )
        keys.append(key)
    return keys


def judged(conn, until):
    """The judged pulse events and the outages of a fast replay up to pulse `until`."""
    daily_tape.replay(conn, T0, after(until), fast=True)
    return conn.execute(JUDGED).fetchall(), conn.execute(OUTAGES).fetchall()


def test_the_rule_needs_nine_answers_in_ten():
    assert pulse_no_exit.SHARE == 0.9 and pulse_no_exit.HOLD == timedelta(hours=2)
    assert pulse_no_exit.guard(3, 2) and pulse_no_exit.guard(5, 3)
    assert not pulse_no_exit.guard(2, 2) and not pulse_no_exit.guard(4, 2)


@pytest.mark.parametrize("fast", [False, True])
async def test_a_country_answering_only_no_exit_opens_no_outage(database, tmp_path, fast):
    """Evomi answers 409 to every request of pulses 6 and 7 on both targets: no 'down', no
    outage, one record for the country's streak, and pulse 8 is compared with pulse 5."""
    seed(database)
    keys = await stored(tmp_path, database, [NO_EXIT] * 4)
    with connect(database) as conn:
        evomi = product(conn, "evomi")
        for target, (six, seven) in (("generic_http", keys[0:2]), ("generic_https", keys[2:4])):
            pulses(conn, evomi, [95] * 6 + [0, 0, 95], run_keys={6: six, 7: seven}, target=target)
    with connect(database) as conn:
        daily_tape.replay(conn, T0, after(9), fast=fast)
        assert conn.execute(JUDGED).fetchall() == [(evomi, "pulse_finished", "no_exits")]
        assert conn.execute(OUTAGES).fetchall() == []
        found = "SELECT data FROM test_events WHERE kind='pulse_finished'"
        (data,) = conn.execute(found).fetchone()
        assert (data["requests"], data["successes"], data["region"]) == (100, 0, "fra1")


@pytest.mark.parametrize(
    ("network", "answers"),
    [
        ("evomi", {"409": 60, "timeout": 40}),  # mixed with timeouts: under the share
        ("evomi", {"409": 85, "503": 15}),  # 503 is not Evomi's "no exit"
        ("iproyal", {"500": 100}),  # 500 is Evomi's, not IPRoyal's
        ("alpha", {"409": 100}),  # no grammar: no "no exit" answer at all
    ],
)
async def test_other_failures_stay_a_normal_down(database, tmp_path, network, answers):
    seed(database)
    (key,) = await stored(tmp_path, database, [answers])
    with connect(database) as conn:
        found = product(conn, network)
        pulses(conn, found, [95] * 6 + [0], run_keys={6: key})
    with connect(database) as conn:
        assert judged(conn, 7) == ([(found, "down", None)], [(found, "DE", True)])


async def test_a_login_refusal_stays_proxy_auth_alert_s(database, tmp_path):
    """Evomi refuses our login (407): PROXY-AUTH-ALERT-1 leaves it unjudged and unrecorded."""
    seed(database)
    (key,) = await stored(tmp_path, database, [{"407": 100, "proxy_auth": 100}])
    with connect(database) as conn:
        evomi = product(conn, "evomi")
        pulses(conn, evomi, [95] * 6 + [0], run_keys={6: key})
    with connect(database) as conn:
        assert judged(conn, 7) == ([], [])


async def test_a_network_answering_no_exit_everywhere_is_down(database, tmp_path):
    """The network guard: Evomi answers 409 in all three countries it was pulsed in."""
    seed(database)
    keys = await stored(tmp_path, database, [NO_EXIT] * 3)
    with connect(database) as conn:
        evomi = product(conn, "evomi")
        for country, key in zip(("DE", "FR", "PL"), keys, strict=True):
            pulses(conn, evomi, [95] * 6 + [0], run_keys={6: key}, country=country)
    with connect(database) as conn:
        events, outages = judged(conn, 7)
        assert events == [(evomi, "down", None)] * 3
        assert sorted(outages) == [(evomi, code, True) for code in ("DE", "FR", "PL")]


async def test_a_normally_big_country_stays_visible_for_two_hours(database, tmp_path):
    """DE had 80 IPs yesterday: its first "no exit" pulse opens the outage as before, and only
    the pulse two hours into the streak (pulse 10) is hidden and recorded."""
    seed(database)
    keys = await stored(tmp_path, database, [NO_EXIT] * 5)
    with connect(database) as conn:
        evomi = product(conn, "evomi")
        verdict(conn, evomi, "DE", T0 - timedelta(days=1), 80)
        pulses(conn, evomi, [95] * 6 + [0] * 5, run_keys=dict(zip(range(6, 11), keys, strict=True)))
    with connect(database) as conn:
        events, outages = judged(conn, 10)
        assert events == [(evomi, "down", None), (evomi, "pulse_finished", "no_exits")]
        assert outages == [(evomi, "DE", True)]  # an open outage is not touched
        slots = (
            "SELECT b.scheduled_for FROM test_events e JOIN test_batches b USING(batch_id) "
            "WHERE e.kind IN ('down','pulse_finished') ORDER BY e.event_id"
        )
        found = conn.execute(slots)
        assert [row[0] - T0 for row in found] == [timedelta(hours=3), timedelta(hours=5)]


REGIONS = ("CA", "DE", "GB", "NL", "US")  # tor1, fra1, lon1, ams3, nyc3


async def pulse_run(tmp_path, database, network, country, no_exit, timeouts=0):
    """A 100-request pulse run of the network in window 0: `no_exit` requests answered 503
    (IPRoyal's "no exit", a proxy error) and `timeouts` timed out."""
    rows = [(window(0), 100, no_exit + timeouts, 300)]
    key = await run(tmp_path, database, "pulse", window(0), network, country, rows)
    with connect(database) as conn:
        conn.execute(
            "UPDATE run_buckets SET status_codes=jsonb_build_object('503',%s::int),"
            "proxy_errors=%s,timeouts=%s WHERE run_key=%s",
            (no_exit, no_exit, timeouts, key),
        )
        conn.execute(
            "UPDATE test_runs SET summary=summary||jsonb_build_object('completed',100,"
            "'status_codes',jsonb_build_object('503',%s::int)) WHERE run_key=%s",
            (no_exit, key),
        )


async def test_the_evaluator_leaves_no_exit_countries_out_of_its_window(database, db, tmp_path):
    """IPRoyal answers 503 to every pulse in DE and GB, and NL times out: with DE and GB the
    window would be down (280 of 500 requests, 3 regions); without them it is 80 of 300."""
    prepare(database, REGIONS)
    (iproyal,) = products(database, "iproyal")
    for country in ("DE", "GB"):
        await pulse_run(tmp_path, database, iproyal, country, no_exit=100)
    for country, timeouts in (("NL", 60), ("CA", 10), ("US", 10)):
        await pulse_run(tmp_path, database, iproyal, country, no_exit=0, timeouts=timeouts)
    assert (await evaluated(db, 0))["opened"] == 0
    assert tape(database, "pulse_down") == []
    assert query(database, "SELECT count(*) FROM outages") == [(0,)]


async def test_the_evaluator_keeps_a_network_answering_no_exit_everywhere(database, db, tmp_path):
    prepare(database, REGIONS)
    (iproyal,) = products(database, "iproyal")
    for country in ("DE", "GB", "NL"):
        await pulse_run(tmp_path, database, iproyal, country, no_exit=100)
    assert (await evaluated(db, 0))["opened"] == 1
    assert len(tape(database, "pulse_down")) == 1
