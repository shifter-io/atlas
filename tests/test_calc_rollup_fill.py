"""GEO-ROLLUP-2: the incremental build fills the geo rollups before its transaction, one range of
at most a day per statement and per transaction (rollups.fill()), on the 3-day fixture of
test_calc_rollups. A build that fails after the fill keeps its rollups and marks, and the next
build counts no hour again; a run ingested between the fill and the build's snapshot is counted
inside the build. Every build still equals the full mode's.
"""

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from calculated_cases import lenient_policy
from census_pulse_cases import batch, batch_run, product
from psycopg.rows import dict_row
from test_calc_rollups import bounds, fixture, seed_geo
from test_calculated_hourly_reads import DAY, HOUR, floor_hour
from test_calculated_incremental import assert_equal
from test_storage import measurement

from atlas.calculated import calculator, incremental, rollup_days, rollups, scheduled
from atlas.storage import connect, ingest

pytest_plugins = ["test_storage"]


@pytest.fixture
def executed(monkeypatch):
    """Record (statement, parameters) of every statement a cursor runs."""
    log = []
    original = psycopg.Cursor.execute

    def execute(self, query, params=None, **kwargs):
        log.append((query if isinstance(query, str) else repr(query), params))
        return original(self, query, params, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", execute)
    return log


def writes(log):
    return [params for query, params in log if query == rollups.WRITE]


def test_ranges_hold_a_day_at_most():
    end = datetime(2026, 11, 6, 12, tzinfo=UTC)
    hours = [end + n * HOUR for n in range(72)] + [end + 80 * HOUR]
    assert rollups._ranges(hours) == [
        [end, end + DAY],
        [end + DAY, end + 2 * DAY],
        [end + 2 * DAY, end + 3 * DAY],
        [end + 80 * HOUR, end + 81 * HOUR],
    ]


@pytest.mark.asyncio
async def test_fill_counts_a_day_per_statement_and_transaction(database, tmp_path, executed):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await fixture(database, tmp_path, end)
    start = len(executed)
    stale = rollups.fill(database, end)
    assert len(stale) == 720  # nothing rolled up yet
    ranges = writes(executed[start:])
    assert len(ranges) >= 3 and all(p["hi"] - p["lo"] <= DAY for p in ranges), ranges
    marks = [q for q, _ in executed[start:] if q == rollups.MARK]
    locks = [q for q, _ in executed[start:] if "pg_advisory_xact_lock(817452902)" in q]
    # One transaction per range, then one that sums the whole days (CALC-SCALE-2), each
    # day in one statement.
    assert len(marks) == len(locks) - 1 == len(ranges)
    days = [p["day"] for q, p in executed[start:] if q == rollup_days.WRITE]
    assert days and days == rollup_days.inside((end - 720 * HOUR, end))
    with connect(database) as conn:
        conn.row_factory = dict_row
        hours, _ = rollups.spans(tuple(bounds(end).values()))
        assert rollups.refresh(conn, hours) == []
        full = scheduled.scheduled_inputs(conn, end, "generic_http")
        assert rollups.scheduled_inputs(conn, end, "generic_http") == full


@pytest.mark.asyncio
async def test_a_failed_build_keeps_the_fill(database, tmp_path, monkeypatch, executed):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await fixture(database, tmp_path, end)
    policy = lenient_policy(tmp_path)

    def killed(*args):
        raise RuntimeError("killed")

    with monkeypatch.context() as patch:
        patch.setattr(incremental, "read", killed)
        with pytest.raises(RuntimeError, match="killed"):
            calculator.calculate(database, policy, as_of=end, mode="incremental")
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM calculated_geo_rollups").fetchone() == (720,)
        assert conn.execute("SELECT count(*) FROM calculated_geo_hours").fetchone()[0] > 0
    start = len(executed)
    assert_equal(database, policy, end)
    assert writes(executed[start:]) == []  # neither the fill nor the build counts again


@pytest.mark.asyncio
async def test_a_run_ingested_after_the_fill_is_counted_in_the_build(
    database, tmp_path, monkeypatch
):
    end = floor_hour(datetime.now(UTC)) + HOUR
    h, rng, _ = await fixture(database, tmp_path, end)
    with connect(database) as conn:
        batch_id = batch(conn, "pulse", end - 40 * HOUR - timedelta(minutes=1))
    directory = await measurement(tmp_path, requests=40, provider="alpha", requested_country="de")
    fill, refresh, counted = rollups.fill, rollups.refresh, []

    def late_fill(database, as_of, rule=False):
        filled = fill(database, as_of, rule)
        key = h.settle(ingest(directory, database, batch_id=batch_id)["run_key"], 40 * HOUR)
        with connect(database) as conn:
            finished = conn.execute(
                "SELECT finished_at FROM test_runs WHERE run_key=%s", (key,)
            ).fetchone()[0]
            batch_run(conn, batch_id, product(conn, "alpha"), finished, run_key=key, requests=40)
            seed_geo(conn, rng, key)
        return filled

    def spied(conn, hours, rule=False):
        counted.append(refresh(conn, hours, rule))
        return counted[-1]

    monkeypatch.setattr(rollups, "fill", late_fill)
    monkeypatch.setattr(rollups, "refresh", spied)
    assert_equal(database, lenient_policy(tmp_path), end)
    assert counted == [[end - 40 * HOUR]]
