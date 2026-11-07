"""NETLIST-KEEP-1: only the current build and the kept drafts keep their network lists (K10).

Three builds published on three UTC days: after each publication only the current build holds
network lists, and the retained builds keep their group lists, heads and windows. An older
retained build published again gets its network lists back under the same list ids, row for row
the lists it first wrote.
"""

from datetime import UTC, datetime, timedelta

import pytest
from calculated_cases import lenient_policy
from test_calculated_hourly_reads import HOUR, daily_history, floor_hour
from test_calculated_netlists import ROWS

from proxy_index.calculated import netlists, store
from proxy_index.calculated.calculator import calculate
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

DAY = timedelta(days=1)
# A network list without created_at, the one column a list written again cannot keep.
LIST = (
    "SELECT list_id,product_id,cohort,window_hours,signature,content_hash,entries,figures,"
    "created_build FROM calculated_lists WHERE cohort='network' ORDER BY list_id"
)
# What a retained build keeps: its head, windows and group lists with their rows.
RETAINED = (
    "SELECT (SELECT payload::text FROM calculated_snapshots WHERE build_id=%(b)s),"
    "(SELECT string_agg(w::text,E'\\n' ORDER BY result_id) FROM calculated_windows w "
    "WHERE build_id=%(b)s),(SELECT string_agg(r::text,E'\\n' ORDER BY r.list_id,dimension,"
    "position) FROM calculated_list_rows r WHERE list_id IN (SELECT list_id FROM "
    "calculated_windows WHERE build_id=%(b)s))"
)


def network_lists(conn):
    """Every network list (without created_at) with its rows, by list id."""
    found = conn.execute(LIST).fetchall()
    return {r[0]: (r, conn.execute(ROWS, (r[0],)).fetchall()) for r in found}


def by_build(lists):
    return {row[8] for row, _ in lists.values()}


@pytest.mark.asyncio
async def test_only_the_current_build_keeps_its_network_lists(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await daily_history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    builds, written, kept = [], {}, {}
    for back in (2, 1, 0):  # three UTC days
        built = calculate(database, policy, as_of=end - back * DAY, publish=True)
        builds.append(build := int(built["snapshot_id"]))
        with connect(database) as conn:
            lists = network_lists(conn)
            assert lists and by_build(lists) == {build}
            written[build] = lists
            for old in builds[:-1]:  # retained: everything but their network lists unchanged
                assert conn.execute(RETAINED, {"b": old}).fetchone() == kept[old]
            kept[build] = conn.execute(RETAINED, {"b": build}).fetchone()
            assert all(kept[build])
            assert store.retained(conn)[0] == builds
    # A draft keeps its network lists next to the current build's.
    draft = int(calculate(database, policy, as_of=end - HOUR)["snapshot_id"])
    with connect(database) as conn:
        store.expire(conn)
        assert by_build(network_lists(conn)) == {builds[-1], draft}
        assert netlists.restore(conn) == 0  # the current build holds every list its head names
        # The middle build is current again (as after a rollback of current_calculation).
        conn.execute("DELETE FROM current_calculation")
    assert store.publish(database, str(builds[1]))["expired_builds"] == []
    with connect(database) as conn:
        lists = network_lists(conn)
        assert by_build(lists) == {builds[1], draft}
        assert {k: v for k, v in lists.items() if v[0][8] == builds[1]} == written[builds[1]]
        assert conn.execute(RETAINED, {"b": builds[1]}).fetchone() == kept[builds[1]]
        assert conn.execute(RETAINED, {"b": builds[2]}).fetchone() == kept[builds[2]]
    assert store.publish(database, str(builds[2]))["expired_builds"] == []
    with connect(database) as conn:
        lists = network_lists(conn)
        assert {k: v for k, v in lists.items() if v[0][8] == builds[2]} == written[builds[2]]
        assert by_build(lists) == {builds[2], draft}
