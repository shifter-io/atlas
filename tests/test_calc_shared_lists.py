"""CALC-SCALE-1 (#1125): an unsigned (census-pulse) window whose list would be the list of its
group's last window with a list of its own (the same IP inputs, geo rows, list figures and
first-seen bins) shares that list: computed and written once, not once a window. In production
the 24h, 7d and 30d census-pulse lists were the same 2.6 M rows written three times every hour
while the data was younger than a day (and the 7d and 30d ones for a week). A signed (daily)
window keeps the list its signature names. The build still equals the full calculation.
"""

from collections import defaultdict
from datetime import UTC, datetime

import pytest
from calculated_cases import lenient_policy
from test_calculated_hourly_reads import HOUR, floor_hour
from test_calculated_incremental import assert_equal, history

from atlas.storage import connect

pytest_plugins = ["test_storage"]

WINDOWS = (
    "SELECT p.provider,w.cohort,w.window_hours,w.list_id,l.signature IS NOT NULL "
    "FROM calculated_windows w JOIN calculated_lists l USING(list_id) "
    "JOIN products p ON p.product_id=w.product_id WHERE w.build_id=%s AND w.target='' "
    "ORDER BY 1,2,3"
)


@pytest.mark.asyncio
async def test_census_pulse_windows_with_one_list_share_it_and_daily_ones_never(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    assert_equal(database, policy, end)  # the incremental build, then the full one: equal
    with connect(database) as conn:
        build = conn.execute("SELECT min(build_id) FROM calculation_builds").fetchone()[0]
        groups = defaultdict(dict)
        signed = {}
        for provider, cohort, hours, list_id, is_signed in conn.execute(WINDOWS, (build,)):
            groups[provider, cohort][hours] = list_id
            signed[provider, cohort] = is_signed
    daily = [lists for key, lists in groups.items() if signed[key]]
    assert daily and all(len(set(lists.values())) == len(lists) for lists in daily)
    unsigned = {key: lists for key, lists in groups.items() if not signed[key]}
    # delta's census-pulse windows are all empty (its runs are older than 30 days): one list.
    delta = [lists for (provider, _), lists in unsigned.items() if provider == "delta"]
    assert delta and all(len(set(lists.values())) == 1 for lists in delta)
    # alpha's census is 30 hours old: its 24h list differs, its 7d and 30d lists are one.
    alpha = [lists for (provider, _), lists in unsigned.items() if provider == "alpha"]
    assert any(lists[168] == lists[720] != lists[24] for lists in alpha)
