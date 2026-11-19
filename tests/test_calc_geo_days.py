"""CALC-SCALE-2: the per-day geo rollups (calculated/rollup_days.py). A whole UTC day inside one of
a build's geo ranges is read from its day row, the sum of its hour rollups, and every input still
equals the full mode's; a late run counts its hour again, which forgets its day until fill()
sums it again; the build reads only days fill() committed.
"""

from datetime import UTC, datetime, timedelta

import pytest
from psycopg.rows import dict_row
from test_calc_rollups import bounds, compare, fixture, land_late
from test_calculated_hourly_reads import HOUR, floor_hour

from atlas.calculated import rollup_days, rollups
from atlas.storage import connect

pytest_plugins = ["test_storage"]

MIDNIGHT = datetime(2026, 11, 16, tzinfo=UTC)
DAY = timedelta(days=1)
# A day's rows against its hours summed again (rollup_days.WRITE): rows on one side only.
DIFFERENCE = (
    "SELECT count(*) FROM ((SELECT * FROM calculated_geo_days WHERE day=%(day)s EXCEPT ALL "
    "SELECT * FROM summed) UNION ALL (SELECT * FROM summed EXCEPT ALL "
    "SELECT * FROM calculated_geo_days WHERE day=%(day)s)) d"
)


def test_a_build_reads_the_days_wholly_inside_one_of_its_ranges():
    cuts = (MIDNIGHT - 2 * DAY - 5 * HOUR, MIDNIGHT - DAY, MIDNIGHT + 3 * HOUR)
    assert rollup_days.inside(cuts) == [MIDNIGHT - 2 * DAY, MIDNIGHT - DAY]
    assert rollup_days.inside((MIDNIGHT - DAY + HOUR, MIDNIGHT + DAY - HOUR)) == []
    assert rollup_days.inside((MIDNIGHT, MIDNIGHT + DAY)) == [MIDNIGHT]


def days(database):
    with connect(database) as conn:
        return [r[0] for r in conn.execute("SELECT day FROM calculated_geo_day_marks ORDER BY 1")]


def assert_summed(database):
    """Every marked day holds exactly its hour rollups summed."""
    with connect(database) as conn:
        for day in days(database):
            conn.execute(
                "CREATE TEMP TABLE summed ON COMMIT DROP AS SELECT * FROM calculated_geo_days "
                "WHERE false"
            )
            conn.execute(
                rollup_days.WRITE.replace("calculated_geo_days", "summed", 1),
                {"day": day, "next": day + DAY},
            )
            assert conn.execute(DIFFERENCE, {"day": day}).fetchone()[0] == 0, day
            conn.execute("DROP TABLE summed")


def read_days(database, as_of):
    """The days the build at as_of reads from the day rollups."""
    with connect(database) as conn:
        conn.row_factory = dict_row
        cuts = tuple(bounds(as_of).values())
        return rollup_days.split(conn, cuts, rollups.spans(cuts)[0])[0]


@pytest.mark.asyncio
async def test_whole_days_come_from_their_rollups_and_equal_the_full_mode(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    h, rng, _ = await fixture(database, tmp_path, end)
    compare(database, end)  # the hour rollups and the batch-run copies, without days
    assert days(database) == [] and rollups.fill(database, end) == []
    written = days(database)
    assert written and read_days(database, end)  # whole days of the 3-day fixture are read
    assert_summed(database)
    assert compare(database, end)[0] == []  # read from the days: the full mode's inputs
    # A late pulse 60 hours back: the build counts its hour again, which forgets its day, and
    # reads that day's hours; the next fill() sums the day again from them.
    await land_late(database, h, rng)
    late = rollups._floor(end - 60 * HOUR)
    day = rollup_days._floor(late)
    with connect(database) as conn:
        conn.row_factory = dict_row
        cuts = tuple(bounds(end).values())
        hours = rollups.spans(cuts)[0]
        assert rollups.refresh(conn, hours) == [late]
        found, rest = rollup_days.split(conn, cuts, hours)
        assert day not in found and set(range(24)) <= {(t - day) // HOUR for t in rest}
        conn.rollback()
    assert compare(database, end)[0] == [late]
    assert day not in days(database)
    assert rollups.fill(database, end) == []
    assert days(database) == written
    assert_summed(database)
    assert compare(database, end)[0] == []


@pytest.mark.asyncio
async def test_old_days_are_collected_and_a_new_rule_sums_them_again(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await fixture(database, tmp_path, end)
    rollups.fill(database, end)
    written = days(database)
    assert written
    # A build a month later collects the days over a day older than its window.
    later = end + 31 * DAY
    with connect(database) as conn:
        conn.row_factory = dict_row
        rollups.refresh(conn, rollups.spans(tuple(bounds(later).values()))[0])
    assert days(database) == []
    # Another country rule empties every rollup and day; fill() counts and sums them again.
    rollups.fill(database, end)
    assert days(database) == written
    rollups.fill(database, end, 1)
    assert days(database) == written
    assert_summed(database)
