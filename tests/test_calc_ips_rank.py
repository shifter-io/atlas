"""COVERAGE-IPS-RANK-1: each network list row's IP rank among the networks at the same place.

netlists.rank() (calculated v15) ranks the rows with IPs of a build's network lists among the
lists of the same kind and window at the same dimension, place and ASN label: ips_rank is SQL
rank() by ips (ties share the better rank, as the API's rankOf), ips_peers the number of those
networks. Hand-written group lists check the rule; a 3-day build checks that every network row
with IPs is ranked and no group row is, that restore() ranks a list it derives again, and that a
v14 layer migrates without changing a row.
"""

import pytest
from calculated_cases import benchmark_scenario
from migration_cases import catalog
from test_calculated_netlists import CITY, US, group, group_list, product

from atlas.calculated import netlists, store
from atlas.storage import connect, migrate

pytest_plugins = ["test_storage", "calculated_template"]

ASN = (64501, "Other Net", "hosting")
RANKS = (
    "SELECT dimension::text,ips,ips_rank,ips_peers FROM calculated_list_rows WHERE list_id=%s "
    "ORDER BY dimension,position"
)
UNRANKED = (
    "SELECT l.cohort,count(*) FILTER (WHERE r.ips_rank IS NULL OR r.ips_peers IS NULL),"
    "count(*) FILTER (WHERE r.ips_rank IS NOT NULL OR r.ips_peers IS NOT NULL),count(*) "
    "FROM calculated_list_rows r JOIN calculated_lists l USING(list_id) WHERE r.ips>0 GROUP BY 1"
)

# The current build's network list rows and their ranks.
CURRENT = (
    "SELECT list_id,dimension,position,ips_rank,ips_peers FROM calculated_list_rows "
    "JOIN calculated_lists USING(list_id) WHERE cohort='network' "
    "AND created_build=(SELECT build_id FROM current_calculation) ORDER BY 1,2,3"
)


@pytest.fixture
def calculated(database):
    store.initialize(database)
    return database


def write(conn, networks, label="24h"):
    """Network lists of one group each: networks is [(provider, kind, rows)]."""
    groups, members = [], {}
    for provider, kind, rows in networks:
        p = product(conn, provider, kind)
        groups.append(group(f"{provider}:{kind}", provider, kind, p, 1))
        members[f"{provider}:{kind}", label] = group_list(conn, p, rows)
    written = netlists.write_network_lists(conn, 1, {"products": groups}, members)
    return {network: found[0] for (network, _), found in written.items()}


def ranks(conn, list_id):
    return conn.execute(RANKS, (list_id,)).fetchall()


def test_ties_share_the_better_rank_among_the_networks_of_one_kind(calculated):
    with connect(calculated) as conn:
        lists = write(
            conn,
            [
                ("alpha", "residential", [("c", US, None, 500, 1, 1, 1)]),
                ("beta", "residential", [("c", US, None, 500, 1, 1, 1)]),
                ("gamma", "residential", [("c", US, None, 100, 1, 1, 1)]),
                # Another kind at the same place is not a peer: 1 of 1.
                ("alpha", "datacenter", [("c", US, None, 900, 1, 1, 1)]),
            ],
        )
        assert ranks(conn, lists["alpha:residential:network"]) == [("c", 500, 1, 3)]
        assert ranks(conn, lists["beta:residential:network"]) == [("c", 500, 1, 3)]
        assert ranks(conn, lists["gamma:residential:network"]) == [("c", 100, 3, 3)]
        assert ranks(conn, lists["alpha:datacenter:network"]) == [("c", 900, 1, 1)]


def test_each_dimension_place_and_asn_is_ranked_apart(calculated):
    with connect(calculated) as conn:
        lists = write(
            conn,
            [
                (
                    "alpha",
                    "residential",
                    [
                        ("c", US, None, 5, 1, 1, 1),
                        ("g", CITY, None, 4, 1, 1, 1),
                        ("a", None, ASN, 3, 1, 1, 1),
                        ("y", CITY, ASN, 2, 1, 1, 1),
                        ("y", US, ASN, 0, 1, 1, 1),  # requests without IPs: not ranked
                    ],
                ),
                (
                    "beta",
                    "residential",
                    [("c", US, None, 9, 1, 1, 1), ("y", CITY, ASN, 1, 1, 1, 1)],
                ),
            ],
        )
        alpha = ranks(conn, lists["alpha:residential:network"])
        assert alpha == [
            ("a", 3, 1, 1),  # a place seen by this network alone: 1 of 1
            ("c", 5, 2, 2),
            ("g", 4, 1, 1),
            ("y", 2, 1, 2),
            ("y", 0, None, None),
        ]
        assert ranks(conn, lists["beta:residential:network"]) == [("c", 9, 1, 2), ("y", 1, 2, 2)]


def test_a_place_is_the_apis_key_not_one_label(calculated):
    isp, other = (64501, "Other Net", "isp"), (64501, "Other Net Inc", "isp")
    usa = ("US", "USA", None, None, None)  # another name of the US: the same country
    with connect(calculated) as conn:
        lists = write(
            conn,
            [
                (
                    "alpha",
                    "residential",
                    [
                        ("a", None, ASN, 400, 2, 1, 1),
                        ("a", None, isp, 200, 1, 1, 1),
                        ("c", US, None, 3, 2, 1, 1),
                        ("c", usa, None, 3, 1, 1, 1),
                        ("c", (" ", "None", None, None, None), None, 9, 0, 1, 1),  # no code
                    ],
                ),
                (
                    "beta",
                    "residential",
                    [("a", None, other, 500, 1, 1, 1), ("c", US, None, 5, 1, 1, 1)],
                ),
            ],
        )
        # Alpha's two labels of AS64501 are one place of 600 IPs: first of two, on both rows.
        assert ranks(conn, lists["alpha:residential:network"]) == [
            ("a", 400, 1, 2),
            ("a", 200, 1, 2),
            ("c", 3, 1, 2),
            ("c", 3, 1, 2),
            ("c", 9, None, None),
        ]
        assert ranks(conn, lists["beta:residential:network"]) == [("a", 500, 2, 2), ("c", 5, 2, 2)]


def test_windows_are_ranked_apart(calculated):
    with connect(calculated) as conn:
        day = write(conn, [("alpha", "residential", [("c", US, None, 5, 1, 1, 1)])])
        week = write(conn, [("beta", "residential", [("c", US, None, 9, 1, 1, 1)])], "7d")
        assert ranks(conn, day["alpha:residential:network"]) == [("c", 5, 1, 1)]
        assert ranks(conn, week["beta:residential:network"]) == [("c", 9, 1, 1)]


@pytest.mark.asyncio
async def test_a_build_ranks_every_network_row_with_ips_and_restore_ranks_again(database, tmp_path):
    await benchmark_scenario(database, tmp_path)
    with connect(database) as conn:
        counts = {cohort: rest for cohort, *rest in conn.execute(UNRANKED)}
        unranked, ranked, total = counts["network"]
        assert unranked == 0 and ranked == total > 0
        assert all(ranked == 0 for cohort, (_, ranked, _) in counts.items() if cohort != "network")
        before = conn.execute(CURRENT).fetchall()
        lost = before[0][0]
        conn.execute("DELETE FROM calculated_list_rows WHERE list_id=%s", (lost,))
        conn.execute("DELETE FROM calculated_lists WHERE list_id=%s", (lost,))
        conn.execute("UPDATE calculated_list_rows SET ips_rank=NULL,ips_peers=NULL")
        assert {r[3] for r in conn.execute(CURRENT)} == {None}
        assert netlists.restore(conn) == 1
        assert conn.execute(CURRENT).fetchall() == before


@pytest.mark.asyncio
async def test_v14_layer_gains_the_ip_ranks_and_keeps_every_row(
    databases, database, calculated_template, tmp_path
):
    await benchmark_scenario(database, tmp_path)
    rows = (
        "SELECT list_id,dimension,position,place_id,asn_label_id,ips,observations,completed,"
        "successes,challenges,blocks,p50,p90,p99 FROM calculated_list_rows ORDER BY 1,2,3"
    )
    with connect(database) as conn:
        conn.execute("ALTER TABLE calculated_list_rows DROP COLUMN ips_rank, DROP COLUMN ips_peers")
        conn.execute("UPDATE calculation_schema SET version=14,checksum=%s", (store.V14_CHECKSUM,))
        before = conn.execute(rows).fetchall()
    result = migrate(database)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        14,
        15,
    )
    assert result["verified"] == list(store.V14_CHECKS)
    with connect(database) as conn:
        assert conn.execute(rows).fetchall() == before and before
        stored = "SELECT count(ips_rank),count(ips_peers) FROM calculated_list_rows"
        assert conn.execute(stored).fetchone() == (0, 0)  # a stored list is not ranked
        # The check counts a list the migration would have changed.
        conn.execute(store.V14_SNAPSHOT)
        conn.execute("DELETE FROM calculated_list_rows WHERE list_id=%s", (before[0][0],))
        check = store.V14_CHECKS["list rows kept with the IP ranks"]
        assert conn.execute(check).fetchone() == (1,)
        conn.rollback()
    assert catalog(database) == catalog(databases(template=calculated_template))
