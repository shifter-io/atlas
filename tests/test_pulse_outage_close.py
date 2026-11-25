"""PULSE-OUTAGE-CLOSE-1: a per-country pulse outage is a network with no success at all in a
country (every target); it ends with the network's first pulse there that gets through on any
target, even below its baseline (detect.py), and the pulse evaluator closes any outage the data
says is over, at the real end (orchestrator/pulse_stale.py). Fixtures only; the database is the
test's own."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from calculated_pulse_cases import SETTINGS, T0, after, pulses
from census_pulse_cases import batch, batch_run, product
from orchestrator_cases import prepare, query

from atlas.detect import detect
from atlas.orchestrator import pulse, pulse_stale
from atlas.storage import connect, partition, seed

pytest_plugins = ["test_storage", "orchestrator_cases"]

S0 = datetime(2026, 10, 5, 12, tzinfo=UTC)  # an hourly pulse slot


def _kinds(conn):
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT kind,target_id FROM test_events WHERE kind IN ('down','recovered',"
            "'success_drop') ORDER BY event_id"
        )
    ]


def test_a_down_ends_with_the_first_pulse_through_even_below_its_baseline(database):
    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        # Both targets down at pulses 6 and 7 (confirmed at 7, PULSE-NODE-DNS-2), then back but
        # never near the 95 % baseline again.
        for target in ("generic_http", "generic_https"):
            pulses(conn, alpha, [95] * 6 + [0, 0, 40, 45, 50], target=target)
        detect(conn, after(10), config=SETTINGS)
        assert _kinds(conn) == [
            ("down", "generic_http"),
            ("down", "generic_https"),
            ("recovered", "generic_http"),
            ("recovered", "generic_https"),
            ("success_drop", "generic_http"),  # judged afresh: degraded, no longer down
            ("success_drop", "generic_https"),
        ]
        rows = conn.execute("SELECT started_at,ended_at FROM outages").fetchall()
        # From the last healthy pulse (5) to the start of the first one through (8).
        assert rows == [(T0 + timedelta(minutes=151), T0 + timedelta(minutes=240))]


def test_one_target_failing_alone_is_its_down_on_the_tape_not_an_outage(database):
    """HTTP works, HTTPS gets nothing (a provider without HTTPS in a country): the HTTPS series
    is 'down' on the tape, but the product is not out in the country, so no outage opens."""
    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        pulses(conn, alpha, [95] * 10)
        pulses(conn, alpha, [95] * 6 + [0] * 4, target="generic_https")
        detect(conn, after(9), config=SETTINGS)
        assert _kinds(conn) == [("down", "generic_https")]
        assert conn.execute("SELECT count(*) FROM outages").fetchone()[0] == 0


def test_an_outage_closes_with_one_target_back_while_the_other_keeps_failing(database):
    """Both targets get nothing at pulses 6 and 7: an outage. HTTP is back at 8, HTTPS never: the
    outage ends at pulse 8 (after one healthy pulse), the HTTPS series stays 'down' on the tape."""
    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        pulses(conn, alpha, [95] * 6 + [0, 0, 95, 95])
        pulses(conn, alpha, [95] * 6 + [0] * 4, target="generic_https")
        detect(conn, after(9), config=SETTINGS)
        assert _kinds(conn) == [
            ("down", "generic_http"),
            ("down", "generic_https"),
            ("recovered", "generic_http"),
        ]
        rows = conn.execute("SELECT started_at,ended_at FROM outages").fetchall()
        assert rows == [(T0 + timedelta(minutes=151), T0 + timedelta(minutes=240))]


def _batch(conn, target, i):
    """The batch of pulse i (pulses()) on the target."""
    return conn.execute(
        "SELECT batch_id FROM test_batches WHERE target_id=%s AND scheduled_for=%s",
        (target, T0 + timedelta(minutes=30 * i)),
    ).fetchone()[0]


def test_a_target_already_down_alone_still_opens_the_outage_the_other_left_to_it(database):
    """HTTPS is already 'down' on its own (HTTP fine to pulse 6; HTTPS's pulse 7 failed on our
    side, never judged). HTTP gets nothing at 7 (a first one: no 'down') and at 8, judged first
    while the HTTPS batch is still running, so it leaves the outage to HTTPS; when HTTPS ends
    with nothing (no new 'down': its series is down already) the outage opens, from HTTP's last
    healthy pulse."""
    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        pulses(conn, alpha, [95] * 7 + [0, 0])
        pulses(conn, alpha, [95] * 5 + [0] * 4, target="generic_https")
        failed = _batch(conn, "generic_https", 7)
        conn.execute("UPDATE batch_runs SET status='failed' WHERE batch_id=%s", (failed,))
        https8 = _batch(conn, "generic_https", 8)
        conn.execute("UPDATE batch_runs SET status='running' WHERE batch_id=%s", (https8,))
        conn.execute(
            "UPDATE test_batches SET finished_at=finished_at+interval '9 minutes',status='running' "
            "WHERE batch_id=%s",
            (https8,),
        )
        detect(conn, after(8), config=SETTINGS)  # HTTP 8 judged; HTTPS 8 still running
        assert conn.execute("SELECT count(*) FROM outages").fetchone()[0] == 0
        conn.execute("UPDATE batch_runs SET status='done' WHERE batch_id=%s", (https8,))
        conn.execute("UPDATE test_batches SET status='done' WHERE batch_id=%s", (https8,))
        detect(conn, after(8) + timedelta(minutes=15), config=SETTINGS)
        assert _kinds(conn) == [("down", "generic_https"), ("down", "generic_http")]
        rows = conn.execute("SELECT started_at,ended_at FROM outages").fetchall()
        assert rows == [(T0 + timedelta(minutes=30 * 6 + 1), None)]


def _slot(conn, k, results, country="DE"):
    """Slot k (hourly) in `country`: one batch per target (6 s apart, as compact pulses run),
    one run per network, {product: (http successes, https successes)}; the batch ids."""
    at = S0 + timedelta(hours=k)
    ids = []
    for i, target in enumerate(("generic_http", "generic_https")):
        b = batch(conn, "pulse", at + timedelta(seconds=6 * i), country=country, target=target)
        conn.execute("UPDATE test_batches SET scheduled_for=%s WHERE batch_id=%s", (at, b))
        for product_id, successes in results.items():
            batch_run(conn, b, product_id, at + timedelta(minutes=1), successes=successes[i])
        ids.append(b)
    return ids


def _outage(conn, product_id, batch_ids):
    """An open per-country pulse outage as detect.py opens it, with the 'down' events of these
    batches (each target's series), at the first batch's country."""
    since = S0 - timedelta(minutes=50)
    partition(conn, "test_events", since)
    for batch_id in batch_ids:
        conn.execute(
            "INSERT INTO test_events(at,kind,batch_id,product_id,country_code,target_id,"
            "region_id,signal,data) SELECT %s,'down',batch_id,%s,country_code,target_id,"
            "region_id,-1,'{}' FROM test_batches WHERE batch_id=%s",
            (since, product_id, batch_id),
        )
    return conn.execute(
        "INSERT INTO outages(product_id,country_code,region_id,source,batch_id,started_at) "
        "SELECT %s,country_code,region_id,'pulse',batch_id,%s FROM test_batches "
        "WHERE batch_id=%s RETURNING outage_id",
        (product_id, since, batch_ids[0]),
    ).fetchone()[0]


async def test_the_evaluator_closes_an_outage_the_data_says_is_over_at_its_real_end(database, db):
    prepare(database, ("DE", "GB"))
    with connect(database) as conn:
        alpha, beta = product(conn, "alpha"), product(conn, "beta")
        # alpha in DE: down at 0; at 1 http is back (below its baseline, https still 0), which
        # the old rules left open: it ended at slot 1's http pulse. beta in DE: down at 1 and
        # with no success at all in its last slot either.
        down = _slot(conn, 0, {alpha: (0, 0), beta: (0, 0)})
        through = beta_down = _slot(conn, 1, {alpha: (40, 0), beta: (0, 0)})
        _slot(conn, 2, {alpha: (40, 50)})
        _slot(conn, 3, {alpha: (45, 45), beta: (0, 0)})
        over, failing = _outage(conn, alpha, down), _outage(conn, beta, beta_down)
        # beta in GB: down at 3 (http only on the tape), through at 4, not settled at first.
        young = _outage(conn, beta, _slot(conn, 3, {beta: (0, 0)}, "GB")[:1])
        gb = _slot(conn, 4, {beta: (80, 80)}, "GB")
    assert (await pulse.evaluate(db, S0 + timedelta(hours=4, minutes=30)))["closed"] == 1
    assert query(database, "SELECT outage_id,ended_at FROM outages ORDER BY outage_id") == [
        (over, S0 + timedelta(hours=1)),
        (failing, None),
        (young, None),
    ]
    events = "SELECT at,batch_id,data FROM test_events WHERE kind='recovered' ORDER BY event_id"
    # A 'recovered' at the outage's end for the target that got through (http) only: https,
    # still failing, keeps its 'down' on the tape.
    [(at, batch_id, data)] = query(database, events)
    assert (at, batch_id) == (S0 + timedelta(hours=1), through[0])
    assert data == {
        "incident": "down",
        "source": "pulse",
        "down_since": (S0 - timedelta(minutes=50)).isoformat(),
        "closed_by": "pulse_evaluate",
    }
    # Once slot 4 has settled the GB outage closes at its start; then nothing more (idempotent).
    later = S0 + timedelta(hours=5, minutes=5)
    assert (await pulse.evaluate(db, later))["closed"] == 1
    assert query(database, "SELECT ended_at FROM outages WHERE outage_id=%s", (young,)) == [
        (S0 + timedelta(hours=4),)
    ]
    assert (await pulse.evaluate(db, later))["closed"] == 0
    # GB: only the http series was down on the tape, so only it gets a 'recovered'.
    assert [row[1] for row in query(database, events)][1:] == gb[:1]


async def test_a_network_outage_is_never_closed_by_the_stale_rule(database, db):
    prepare(database, ("DE",))
    with connect(database) as conn:
        alpha = product(conn, "alpha")
        _slot(conn, 1, {alpha: (90, 90)})
        conn.execute(
            "INSERT INTO outages(product_id,country_code,region_id,source,started_at) "
            "SELECT %s,country_code,region_id,'pulse',%s FROM countries",
            (alpha, S0),
        )
    async with db.transaction() as conn:
        assert await pulse_stale.stale_ends(conn, S0 + timedelta(hours=5)) == []
