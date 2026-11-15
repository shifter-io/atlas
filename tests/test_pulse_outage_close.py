"""PULSE-OUTAGE-CLOSE-1: a per-country pulse outage ends with the network's first pulse in the
country that gets through, even below its baseline (detect.py), and the pulse evaluator closes
any outage the data says is over, at the real end (orchestrator/pulse_stale.py). Fixtures only;
the database is the test's own."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from calculated_pulse_cases import SETTINGS, T0, after, pulses
from census_pulse_cases import batch, batch_run, product
from orchestrator_cases import prepare, query

from atlas.detect import detect
from atlas.orchestrator import pulse, pulse_stale
from atlas.storage import connect, seed

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
        # Both targets down at pulse 6, then back but never near the 95 % baseline again.
        for target in ("generic_http", "generic_https"):
            pulses(conn, alpha, [95] * 6 + [0, 40, 45, 50], target=target)
        detect(conn, after(9), config=SETTINGS)
        assert _kinds(conn) == [
            ("down", "generic_http"),
            ("down", "generic_https"),
            ("recovered", "generic_http"),
            ("recovered", "generic_https"),
            ("success_drop", "generic_http"),  # judged afresh: degraded, no longer down
            ("success_drop", "generic_https"),
        ]
        rows = conn.execute("SELECT started_at,ended_at FROM outages").fetchall()
        # From the last healthy pulse (5) to the start of the first one through (7).
        assert rows == [(T0 + timedelta(minutes=151), T0 + timedelta(minutes=210))]


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


def _outage(conn, product_id, batch_id):
    """An open per-country pulse outage as detect.py opens it, at its batch's country."""
    return conn.execute(
        "INSERT INTO outages(product_id,country_code,region_id,source,batch_id,started_at) "
        "SELECT %s,country_code,region_id,'pulse',batch_id,%s FROM test_batches "
        "WHERE batch_id=%s RETURNING outage_id",
        (product_id, S0 - timedelta(minutes=50), batch_id),
    ).fetchone()[0]


async def test_the_evaluator_closes_an_outage_the_data_says_is_over_at_its_real_end(
    database, db
):
    prepare(database, ("DE", "GB"))
    with connect(database) as conn:
        alpha, beta = product(conn, "alpha"), product(conn, "beta")
        # alpha in DE: down at 0; slot 1 half back (https still 0); slots 2 and 3 through but
        # below its baseline, which the old rule left open. It ended at slot 2's first pulse.
        # beta in DE: down at 1 and still failing (https) in its last slot.
        down = _slot(conn, 0, {alpha: (0, 0), beta: (0, 0)})
        beta_down = _slot(conn, 1, {alpha: (40, 0), beta: (0, 0)})
        through = _slot(conn, 2, {alpha: (40, 50)})
        _slot(conn, 3, {alpha: (45, 45), beta: (90, 0)})
        over, failing = _outage(conn, alpha, down[0]), _outage(conn, beta, beta_down[0])
        # beta in GB: down at 3, through at 4, which has not settled yet at the first call.
        young = _outage(conn, beta, _slot(conn, 3, {beta: (0, 0)}, "GB")[0])
        _slot(conn, 4, {beta: (80, 80)}, "GB")
    assert (await pulse.evaluate(db, S0 + timedelta(hours=4, minutes=30)))["closed"] == 1
    assert query(database, "SELECT outage_id,ended_at FROM outages ORDER BY outage_id") == [
        (over, S0 + timedelta(hours=2)),
        (failing, None),
        (young, None),
    ]
    [(at, batch_id, data)] = query(
        database, "SELECT at,batch_id,data FROM test_events WHERE kind='recovered'"
    )
    assert (at, batch_id) == (S0 + timedelta(hours=2), through[0])
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
    assert len(query(database, "SELECT 1 FROM test_events WHERE kind='recovered'")) == 2


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
