"""CALC-SCALE-3 (#1307): the latest build counts each network window's overlap (its IPs, the
shared ones and each peer's) from the IP state's vectors (ipstate.network_counts()) instead of a
pass over the window's observations, which the 7d and 30d overlaps made at the first build of
every UTC day; the incremental build still equals the full one, which counts them in SQL.
"""

from datetime import UTC, datetime

import pytest
from calculated_cases import lenient_policy
from test_calculated_hourly_reads import HOUR, floor_hour
from test_calculated_incremental import assert_equal, history

from atlas.calculated import incremental, ipstate, network_scores

pytest_plugins = ["test_storage"]


def test_network_counts_follow_the_vectors_scopes_by_kind():
    # Scopes 2 and 3 are network a's, 4 network b's (both residential), 6 network c's (mobile).
    vectors = ipstate.Vectors([((2,), 5), ((2, 4), 3), ((3, 4, 6), 2), ((6,), 1), ((8,), 9)])
    members = [
        ({"kind": "residential"}, {"_runs": [10, 11]}),
        ({"kind": "residential"}, {"_runs": [12]}),
        ({"kind": "mobile"}, {"_runs": [13]}),
    ]
    scope_of = {10: 2, 11: 3, 12: 4, 13: 6, 99: 8}  # run 99 (scope 8) is in no member
    own, pairs = ipstate.network_counts(vectors, members, scope_of)
    assert own == {0: (10, 5), 1: (5, 5), 2: (3, 0)}  # c alone in its kind shares nothing
    assert pairs == {0: {1: 5}, 1: {0: 5}}


@pytest.mark.asyncio
async def test_the_latest_build_counts_the_overlap_from_the_ip_state(
    database, tmp_path, monkeypatch
):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    used, counted = [], incremental.Incremental.overlaps

    def spy(source, members):
        found = counted(source, members)
        used.append((members[0][1]["period"]["hours"], found is not None))
        return found

    monkeypatch.setattr(incremental.Incremental, "overlaps", spy)
    queried = []
    sql = network_scores._counted
    monkeypatch.setattr(
        network_scores, "_counted", lambda conn, m: queried.append(1) or sql(conn, m)
    )
    payload = assert_equal(database, lenient_policy(tmp_path), end)  # the full build: SQL
    assert sorted(used) == [(24, True), (168, True), (720, True)]
    assert len(queried) == 3  # the full build's three windows only
    shared = [w["overlap"] for n in payload["networks"] for w in n["windows"].values()]
    assert any(o and o.get("peers") for o in shared)  # networks that share IPs
