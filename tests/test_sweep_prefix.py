"""CI-90-1: a copied prefix of the hourly sweeps (sweep_prefix) is the database the sweep had
replayed: the same lists, memos and calculated rows, and the next build has the same hits, misses
and statements. And copies of one shared template can run at once.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from calculated_cases import lenient_policy
from helpers import drop_databases, scratch_database, untraced
from psycopg import sql
from psycopg.types.json import Jsonb
from storage_cases import admin_credentials, credentials_file
from sweep_prefix import name, prefix, replay
from test_calc_fleet import SWEEP
from test_calculated_hourly_reads import HOUR, floor_hour, statements
from test_calculated_incremental import chain, count_windows, without

from atlas.calculated.calculator import calculate
from atlas.storage import connect

pytest_plugins = ["test_storage"]

# What a build takes from the clock, not from its inputs: when it wrote a row, its payload's
# generated_at and so its snapshot's hash, and its slot memos' keys (hourly._digest: a run's stamp
# is when its ranges were counted).
CLOCK = {
    "created_at",
    "last_used",
    "calculated_snapshots.content_hash",
    "calculated_slot_figures.digest",
}
# Values stored in no particular order: fleetsql's cohort counts, pairs read back as a dict.
UNORDERED = {"calculated_distinct.counts"}
LATE_RUN = (
    "SELECT run_id,started_at,finished_at,summary_sha256,summary,imported_at FROM test_runs "
    "WHERE run_key=%s"
)
SNAPSHOT = "Composed([SQL('SET TRANSACTION SNAPSHOT ')"
TABLES = (
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
    "WHERE n.nspname='public' AND c.relkind IN ('r','p') AND NOT c.relispartition "
    "AND c.relname LIKE 'calculat%' ORDER BY 1"
)
COLUMNS = (
    "SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 "
    "AND NOT attisdropped ORDER BY attnum"
)


def clockless(value):
    """value without its JSON objects' generated_at (a build's clock)."""
    if isinstance(value, dict):
        return {k: clockless(v) for k, v in value.items() if k != "generated_at"}
    if isinstance(value, list):
        return [clockless(v) for v in value]
    return value


def calculated_rows(database):
    """Every row of the calculated tables (lists, memos, windows, builds, snapshots and the rest),
    sorted, without the columns and payload values a build sets from the clock."""

    def row(table, columns, values):
        values = [
            sorted(v, key=json.dumps) if f"{table}.{c}" in UNORDERED else clockless(v)
            for c, v in zip(columns, values, strict=True)
        ]
        return json.dumps(values, default=str)

    found = {}
    with connect(database) as conn:
        for (table,) in conn.execute(TABLES).fetchall():
            columns = [
                c
                for (c,) in conn.execute(COLUMNS, (table,))
                if c not in CLOCK and f"{table}.{c}" not in CLOCK
            ]
            query = sql.SQL("SELECT {} FROM {}").format(
                sql.SQL(",").join(map(sql.Identifier, columns)), sql.Identifier(table)
            )
            found[table] = sorted(row(table, columns, r) for r in conn.execute(query))
    return found


def same_late_run(source, target):
    """Give target's late run (its newest) source's: the measurement's id and clock."""
    with connect(source) as a, connect(target) as b:
        key = b.execute("SELECT max(run_key) FROM test_runs").fetchone()[0]
        row = a.execute(LATE_RUN, (key,)).fetchone()
        b.execute(
            "UPDATE test_runs SET run_id=%s,started_at=%s,finished_at=%s,summary_sha256=%s,"
            "summary=%s,imported_at=%s WHERE run_key=%s",
            (*row[:4], Jsonb(row[4]), row[5], key),
        )
        b.execute("UPDATE estimated_hours SET measured_at=%s WHERE run_key=%s", (row[1], key))


def statements_made(log):
    """The statements a build ran and their rows, in any order (fleetsql counts its slices on
    parallel connections), without the snapshot id the slice connections import."""
    return sorted((SNAPSHOT if query.startswith(SNAPSHOT) else query, rows) for query, rows in log)


# One start per chain: each day of the incremental sweep (day 1's built from another prefix, day
# 2's after its late run) and the fleet sweep. Their next cutoffs add no late run.
STARTS = [
    pytest.param(chain(0), 5, id="incremental-0-05"),
    pytest.param(chain(1), 7, id="incremental-1-07"),
    pytest.param(chain(2), 6, id="incremental-2-06"),
    pytest.param(SWEEP, 3, id="fleet-03"),
]


# No coverage (tests/conftest.py): other tests cover every line and branch it does (CI-90-5).
@pytest.mark.no_cover
@pytest.mark.asyncio
@pytest.mark.parametrize(("days", "start"), STARTS)
async def test_a_copied_prefix_equals_the_replayed_one(
    days, start, database_template, databases, tmp_path, monkeypatch
):
    """The day's prefix at start, copied, equals the day's cutoffs before start replayed on a
    copy of the history, the late run (a new measurement) given the copy's id and clock."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    policy = lenient_policy(tmp_path)
    copied = await prefix(days, start, end, database_template, tmp_path, policy, databases)
    # The old way: the day's cutoffs before start replayed on a copy of the history.
    replayed = await prefix(days, 0, end, database_template, tmp_path, policy, databases)
    late, add_late = days.late

    async def add():
        await add_late(replayed[1])
        same_late_run(copied[0], replayed[0])

    with untraced():  # the sweep's own builds cover these lines
        await replay(replayed[0], policy, end, days.hours[:start], (late, add))
    assert calculated_rows(copied[0]) == calculated_rows(replayed[0])
    counted, log = count_windows(monkeypatch), statements(monkeypatch)
    as_of, after = end - days.hours[start] * HOUR, []
    for database, _ in (copied, replayed):
        counted.update(hit=0, miss=0)
        first = len(log)
        payload = calculate(database, policy, as_of=as_of, mode="incremental")
        payload = without(json.loads(json.dumps(payload, default=str)))
        after.append((dict(counted), statements_made(log[first:]), clockless(payload)))
    monkeypatch.undo()
    assert after[0] == after[1]
    assert after[0][0]["hit"] > 0
    assert calculated_rows(copied[0]) == calculated_rows(replayed[0])


@pytest.mark.asyncio
async def test_copies_of_one_shared_template_run_at_once(database_template, databases, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    policy = lenient_policy(tmp_path)
    await prefix(SWEEP, 0, end, database_template, tmp_path, policy, databases)
    admin = admin_credentials()
    template = name(SWEEP, end)
    with ThreadPoolExecutor(8) as pool:
        copies = list(pool.map(lambda _: scratch_database(admin, template=template), range(8)))
    try:
        assert len(set(copies)) == 8
        with connect(credentials_file(admin, copies[0], tmp_path)) as conn:
            assert conn.execute("SELECT count(*) FROM test_runs").fetchone()[0] > 0
    finally:
        drop_databases(admin, copies)
