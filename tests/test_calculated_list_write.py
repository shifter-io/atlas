"""LISTS-WRITE-1: a group's lists written together, without the per-row foreign key.

calculated_list_rows.list_id has no foreign key since calculated v13: these tests show that no
row is ever left without its list (after a build, discard, expire and collect, and after a failed
build), that a v12 layer migrates without rewriting a row, and that the breakdown labels are
matched through the unique key of their name lists.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from calculated_cases import benchmark_scenario
from migration_cases import catalog
from psycopg.rows import tuple_row
from test_calculated_lists import own_lists
from test_retention import clone

from atlas import names
from atlas.calculated import incremental, list_write, netlists, store
from atlas.calculated.aggregate import BREAKDOWNS
from atlas.calculated.calculator import calculate
from atlas.storage import connect, migrate

pytest_plugins = ["test_storage", "calculated_template"]

# Rows without their list, and lists and rows in all.
ORPHAN_ROWS = (
    "SELECT (SELECT count(*) FROM calculated_list_rows r WHERE NOT EXISTS "
    "(SELECT FROM calculated_lists l WHERE l.list_id=r.list_id))"
)
TOTALS = (
    "SELECT (SELECT count(*) FROM calculated_lists),(SELECT count(*) FROM calculated_list_rows)"
)
FOREIGN_KEY = (
    "SELECT count(*) FROM pg_constraint WHERE conrelid='calculated_list_rows'::regclass "
    "AND confrelid='calculated_lists'::regclass"
)


def orphans(conn):
    return conn.execute(ORPHAN_ROWS).fetchone()[0]


@pytest.mark.asyncio
async def test_no_list_row_outlives_its_list(database, tmp_path):
    _, _, first = await benchmark_scenario(database, tmp_path)
    base = int(first["snapshot_id"])
    at = datetime.fromisoformat(first["as_of"])
    with connect(database) as conn:
        assert conn.execute(FOREIGN_KEY).fetchone() == (0,)
        assert orphans(conn) == 0 and conn.execute(TOTALS).fetchone()[1] > 0
        kept = clone(conn, base, at)  # shares the base build's lists
        older = clone(conn, base, at - timedelta(days=40))  # expires
        own = clone(conn, base, at - timedelta(days=41))  # expires, with lists of its own
        own_lists(conn, own)
        conn.execute("UPDATE current_calculation SET build_id=%s", (kept,))
        store.discard(conn, base)  # collects: its lists stay, the kept build references them
        assert orphans(conn) == 0
    assert store.retention(database)["expired_builds"] == [older, own]
    with connect(database) as conn:
        assert orphans(conn) == 0
        # The kept build goes too: collection deletes every list with its rows.
        conn.execute("DELETE FROM current_calculation")
        store.discard(conn, kept, gc=False)
        assert store.collect(conn) > 0
        assert orphans(conn) == 0 and conn.execute(TOTALS).fetchone() == (0, 0)


@pytest.mark.asyncio
async def test_a_failed_build_leaves_no_list_rows(database, tmp_path, monkeypatch):
    policy, last, _ = await benchmark_scenario(database, tmp_path)
    with connect(database) as conn:
        before = conn.execute(TOTALS).fetchone()
    written = []

    def failing(conn, *args):
        # The group lists are written (census-pulse lists are written by every build).
        written.append(conn.cursor(row_factory=tuple_row).execute(TOTALS).fetchone())
        raise RuntimeError("network lists failed")

    monkeypatch.setattr(netlists, "write_network_lists", failing)
    with pytest.raises(RuntimeError, match="network lists failed"):
        calculate(database, policy, as_of=last + timedelta(hours=1))
    assert written[0][0] > before[0] and written[0][1] > before[1]
    with connect(database) as conn:
        assert conn.execute(TOTALS).fetchone() == before and orphans(conn) == 0


@pytest.mark.asyncio
async def test_v12_layer_drops_the_list_rows_key_and_keeps_every_row(
    databases, database, calculated_template, tmp_path
):
    await benchmark_scenario(database, tmp_path)
    rows = "SELECT * FROM calculated_list_rows ORDER BY list_id,dimension,position"
    with connect(database) as conn:
        conn.execute(
            "ALTER TABLE calculated_list_rows ADD CONSTRAINT calculated_list_rows_list_id_fkey "
            "FOREIGN KEY(list_id) REFERENCES calculated_lists(list_id)"
        )
        conn.execute("UPDATE calculation_schema SET version=12,checksum=%s", (store.V12_CHECKSUM,))
        before = conn.execute(rows).fetchall()
    result = migrate(database)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        12,
        22,
    )
    verified = [*store.V12_CHECKS, *store.V13_CHECKS, *store.V14_CHECKS, *store.V15_CHECKS]
    verified += [*store.V16_CHECKS, *store.V17_CHECKS, *store.V18_CHECKS, *store.V19_CHECKS]
    verified += [*store.V20_CHECKS, *store.V21_CHECKS]
    assert result["verified"] == verified
    with connect(database) as conn:
        assert conn.execute(FOREIGN_KEY).fetchone() == (0,)
        assert conn.execute(rows).fetchall() == before
    assert catalog(database) == catalog(databases(template=calculated_template))


PLACES = {
    (None,) * 5,
    ("US", "United States", None, None, None),
    ("US", "United States", "CA", "California", None),
    ("US", "United States", "CA", "California", "San Jose"),
    ("US", None, None, None, "San Jose"),
}
NETWORKS = {(None, None, None), (64500, "Example", None), (64500, "Example", "hosting")}
PLACE_ID = (
    "SELECT place_id FROM place_names WHERE (country_code,country,region_code,region,city) "
    "IS NOT DISTINCT FROM (%s,%s,%s,%s,%s)"
)
LABEL_ID = (
    "SELECT asn_label_id FROM asn_labels JOIN asn_names USING(asn_name_id) "
    "WHERE (asn,name,usage_type) IS NOT DISTINCT FROM (%s::bigint,%s,%s)"
)


def test_labels_match_each_null_pattern_by_the_unique_key(calculated_database, monkeypatch):
    statements = []
    execute = psycopg.Cursor.execute

    def spy(self, query, params=None, **kwargs):
        statements.append((str(query), params))
        return execute(self, query, params, **kwargs)

    with connect(calculated_database) as conn:
        monkeypatch.setattr(psycopg.Cursor, "execute", spy)
        places, networks = names.labels(conn, PLACES, NETWORKS)
        monkeypatch.undo()
        assert names.labels(conn, PLACES, NETWORKS) == (places, networks)
        assert names.labels(conn, set(), set()) == ({}, {})
        assert set(places) == PLACES and set(networks) == NETWORKS
        for place, place_id in places.items():
            assert conn.execute(PLACE_ID, place).fetchone() == (place_id,)
        for network, label_id in networks.items():
            assert conn.execute(LABEL_ID, network).fetchone() == (label_id,)
        # Every branch of the place match compares the unique key's columns, so each can read an
        # index of place_names; a comparison of jsonb_build_array() values could not.
        query, params = next(
            (q, p) for q, p in statements if "JOIN place_names d" in q and "UNION ALL" in q
        )
        conn.execute("SET LOCAL enable_seqscan=off")
        plan = "\n".join(r[0] for r in conn.execute(f"EXPLAIN {query}", params))
        assert "Seq Scan on place_names" not in plan


def a_window(hours, ips=2):
    """A window with one country row and nothing else."""
    counters = {"ips": ips, "observations": 3, "completed": 4, "successes": 4}
    counters |= {"success_rate": 1.0, "challenges": 0, "blocks": 0, "p50": 1, "p90": 2, "p99": 3}
    rows = dict.fromkeys(BREAKDOWNS, ()) | {
        "country": [{"country_code": "XA", "country": "Example"} | counters]
    }
    return {"breakdowns": rows, "pool": {"unique_ips": ips}, "period": {"hours": hours}}


def test_a_group_writes_its_lists_in_one_insert_and_verifies_signed_ones(calculated_database):
    with connect(calculated_database) as conn:
        conn.execute("INSERT INTO providers VALUES ('example')")
        product = conn.execute(
            "INSERT INTO products(provider,name) VALUES ('example','residential') "
            "RETURNING product_id"
        ).fetchone()[0]
        group = {"product_id": product, "cohort": "c"}
        prepared = [list_write.prepare(group, a_window(h), f"s{h}") for h in (24, 168)]
        first = list_write.write_lists(conn, 1, prepared)
        assert first[0] < first[1]
        # A signed list whose twin is stored is reused, a new one is inserted.
        again = [*prepared, list_write.prepare(group, a_window(720), None)]
        assert list_write.write_lists(conn, 2, again)[:2] == first
        assert conn.execute(TOTALS).fetchone() == (3, 3) and orphans(conn) == 0
        with pytest.raises(ValueError, match="incomplete"):
            list_write.write_lists(conn, 3, [list_write.prepare(group, a_window(24, 5), "s24")])
        twins = [list_write.prepare(group, a_window(24), None)] * 2
        with pytest.raises(ValueError, match="differ in their window"):
            list_write.write_lists(conn, 3, twins)
    bad = a_window(24)
    bad["breakdowns"]["country"][0]["city"] = "Nowhere"
    with pytest.raises(ValueError, match="Unexpected breakdown label"):
        list_write.prepare(group, bad)


def test_the_incremental_store_refuses_a_list_left_unwritten():
    inputs = incremental.Inputs(source=SimpleNamespace(unwritten={("g", 1): []}), signed={})
    with pytest.raises(ValueError, match="was not written"):
        inputs.store(None, 1, {}, {})
