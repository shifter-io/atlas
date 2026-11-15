"""CALC-SCALE-1 (#1125): a build writes its network lists in one pass (netlists._write_merged():
every window merged into one temporary table, then each row written once, ranked), exactly what
writing them one list at a time and ranking them after (restore()'s way, RESTORE and rank())
writes: the same rows, ranks, content hashes, entry counts and place counts. Writing them, then
updating every row's rank, took 345 s of an 862 s production build at 8 M rows.
"""

import pytest
from calculated_cases import benchmark_scenario

from atlas.calculated import netlists, parts
from atlas.storage import connect

pytest_plugins = ["test_storage", "calculated_template"]

COLUMNS = (
    "dimension,position,place_id,asn_label_id,ips,observations,completed,successes,challenges,"
    "blocks,p50,p90,p99,ips_rank,ips_peers"
)


def stored(conn, lists):
    """{key: (product, hours, content_hash, entries, rows)} of the network lists {key: list_id}."""
    found = {}
    for key, list_id in lists.items():
        head = conn.execute(
            "SELECT product_id,window_hours,content_hash,entries FROM calculated_lists "
            "WHERE list_id=%s",
            (list_id,),
        ).fetchone()
        rows = conn.execute(
            f"SELECT {COLUMNS} FROM calculated_list_rows WHERE list_id=%s "
            "ORDER BY dimension,position",
            (list_id,),
        ).fetchall()
        found[key] = (*head, rows)
    return found


@pytest.mark.asyncio
async def test_one_pass_writes_what_one_list_at_a_time_then_ranked_writes(database, tmp_path):
    await benchmark_scenario(database, tmp_path)
    with connect(database) as conn:
        build = conn.execute("SELECT build_id FROM current_calculation").fetchone()[0]
        head = parts.head(conn, build)
        named = {
            (entry["id"], label): window["lists"]["id"]
            for entry in head["networks"]
            for label, window in entry["windows"].items()
            if window.get("lists")
        }
        groups = list(parts.stream(conn, build, "products"))
        members = {
            (g["id"], label): w["lists"]["id"]
            for g in groups
            for label, w in g["windows"].items()
            if w.get("lists")
        }
        payload = {"products": groups}
        built = stored(conn, named)  # the build's own lists, written in one pass
        again = netlists.write_network_lists(conn, build + 1, payload, members)
        assert stored(conn, {key: found[0] for key, found in again.items()}) == built
        lost = list(named.values())
        conn.execute("DELETE FROM calculated_list_rows WHERE list_id=ANY(%s)", (lost,))
        conn.execute("DELETE FROM calculated_lists WHERE list_id=ANY(%s)", (lost,))
        one_by_one = netlists.write_network_lists(conn, build, payload, members, named)
        netlists.rank(conn, named)
        assert stored(conn, named) == built
        assert {key: found[1:] for key, found in one_by_one.items()} == {
            key: found[1:] for key, found in again.items()
        }
        assert len(named) > 3 and any(rows for *_, rows in built.values())
