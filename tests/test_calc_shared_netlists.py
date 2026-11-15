"""CALC-SCALE-1 (#1125): a network window whose list would be the list of an earlier window of
the same network names that list: when every network of its kind has, at both labels, members
of the same contents (content hashes in list id order), the merged rows, counts and IP ranks are
the same. While the data is younger than a day the 24h, 7d and 30d network lists were the same
8.9 M rows, merged, ranked and written every hour. restore() writes a shared list once.
"""

import pytest
from test_calculated_netlists import US, group, group_list, product

from atlas.calculated import netlists, store
from atlas.storage import connect

pytest_plugins = ["test_storage", "calculated_template"]

ROWS = "SELECT dimension,position,ips,ips_rank,ips_peers FROM calculated_list_rows WHERE list_id=%s"
DAY = [("c", US, None, 50, 10, 9, 100)]
LATER = [("c", US, None, 70, 12, 11, 90)]


@pytest.fixture
def calculated(database):
    store.initialize(database)
    return database


def written(conn, build, contents):
    """write_network_lists() of two residential networks with one group each, whose members at
    each label have the rows contents[provider][label]: {(provider, label): list_id}."""
    groups, members = [], {}
    for provider, by_label in contents.items():
        p = product(conn, provider, "residential")
        groups.append(group(f"{provider}:r", provider, "residential", p, 1))
        for label, rows in by_label.items():
            members[f"{provider}:r", label] = group_list(conn, p, rows)
    found = netlists.write_network_lists(conn, build, {"products": groups}, members)
    return {(network.split(":")[0], label): f[0] for (network, label), f in found.items()}


def test_windows_with_the_same_members_in_every_network_of_the_kind_share_a_list(calculated):
    with connect(calculated) as conn:
        ids = written(
            conn,
            1,
            {
                "alpha": {"24h": DAY, "7d": DAY, "30d": LATER},
                "beta": {"24h": DAY, "7d": DAY, "30d": DAY},
            },
        )
        # 7d: both networks have their 24h members' contents, so both share their 24h lists.
        assert ids["alpha", "7d"] == ids["alpha", "24h"] and ids["beta", "7d"] == ids["beta", "24h"]
        # 30d: alpha's differ, so no network of the kind shares (the ranks are the kind's).
        assert len({ids[n, "30d"] for n in ("alpha", "beta")} | {ids["alpha", "24h"]}) == 3
        assert ids["beta", "30d"] != ids["beta", "24h"]
        # Each list holds its own rows and ranks.
        assert conn.execute(ROWS, (ids["alpha", "24h"],)).fetchall() == [("c", 0, 50, 1, 2)]
        assert conn.execute(ROWS, (ids["alpha", "30d"],)).fetchall() == [("c", 0, 70, 1, 2)]
        assert conn.execute(ROWS, (ids["beta", "30d"],)).fetchall() == [("c", 0, 50, 2, 2)]


def test_restore_writes_a_shared_list_once_and_ranks_it_once(calculated):
    with connect(calculated) as conn:
        contents = {"alpha": {"24h": DAY, "7d": DAY}, "beta": {"24h": LATER, "7d": LATER}}
        ids = written(conn, 1, contents)
        assert ids["alpha", "7d"] == ids["alpha", "24h"]
        before = {key: conn.execute(ROWS, (list_id,)).fetchall() for key, list_id in ids.items()}
        named = {(f"{p}:residential:network", label): i for (p, label), i in ids.items()}
        lost = list(ids.values())
        conn.execute("DELETE FROM calculated_list_rows WHERE list_id=ANY(%s)", (lost,))
        conn.execute("DELETE FROM calculated_lists WHERE list_id=ANY(%s)", (lost,))
        members, again = {}, []  # the build's groups, read back with their member lists
        for provider, by_label in contents.items():
            p = product(conn, f"{provider}-again", "residential")
            again.append(group(f"{provider}:r", provider, "residential", p, 1))
            for label, rows in by_label.items():
                members[f"{provider}:r", label] = group_list(conn, p, rows)
        restored = netlists.write_network_lists(conn, 1, {"products": again}, members, named)
        netlists.rank(conn, named)
        assert {key: found[0] for key, found in restored.items()} == named
        after = {key: conn.execute(ROWS, (list_id,)).fetchall() for key, list_id in ids.items()}
        assert after == before
