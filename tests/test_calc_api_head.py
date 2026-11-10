"""API-LOAD-STATEMENTS-1: what the API reads at each load, stored when a build is written.

calculated_snapshots.api_head (calculated v14, calculated/api_head.sql) is the v4 head without
"parts" and "overlap", with only the every-group fleet rows: what the API built from the head on
every load before. These tests show that both ways of publishing write it, that it is that
projection of the stored head, that a head before v4 gets none, and that a v13 layer migrates
without changing a stored build. api/internal/store reads one build both ways.

The API reads a network list's row counts from calculated_lists.entries (NOT NULL since
calculated v9), which list_write.py and netlists.py write with the rows: they count the stored
rows, and the API's names of its dimensions (api/internal/store/netlists.go) are the framework's.
"""

import json
import re
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import pytest
from calculated_cases import benchmark_scenario
from migration_cases import catalog

from proxy_index.calculated import netlists, parts, store
from proxy_index.calculated.aggregate import BREAKDOWNS
from proxy_index.calculated.calculator import calculate
from proxy_index.storage import connect, migrate

pytest_plugins = ["test_storage", "calculated_template"]

HEADS = "SELECT build_id,payload,api_head FROM calculated_snapshots ORDER BY build_id"
KEPT = "build_id,content_hash,payload"
NETLISTS_GO = Path(__file__).parents[1] / "api/internal/store/netlists.go"
COUNTED = (
    "SELECT l.list_id,l.cohort,l.entries,r.dimension::text,count(r.position) "
    "FROM calculated_lists l LEFT JOIN calculated_list_rows r USING(list_id) GROUP BY 1,2,3,4"
)


def api_dimensions():
    """The API's breakdown name of each network list dimension code (netCodes, netEntries)."""
    text = NETLISTS_GO.read_text()
    codes = re.search(r'const netCodes = "(\w+)"', text).group(1)
    names = re.search(r"var netEntries = \[len\(netCodes\)\]string\{([^}]*)\}", text).group(1)
    names = re.findall(r'"(\w+)"', names)
    assert len(codes) == len(names)
    return dict(zip(codes, names, strict=True))


def stored_lists(database):
    """{list_id: (cohort, entries, {dimension: rows counted})} of every stored list."""
    lists = defaultdict(lambda: [None, None, {}])
    with connect(database) as conn:
        for list_id, cohort, entries, dimension, rows in conn.execute(COUNTED):
            lists[list_id][:2] = cohort, entries
            if dimension:
                lists[list_id][2][dimension] = rows
    return lists


def expected(head):
    """The API head of a v4 head, built in Python."""
    out = {k: v for k, v in head.items() if k not in ("parts", "overlap", "fleet")}
    if "fleet" in head:
        out["fleet"] = [f for f in head["fleet"] if f.get("cohort") == "*"]
    return out


def heads(database):
    with connect(database) as conn:
        return {build: (head, api) for build, head, api in conn.execute(HEADS).fetchall()}


@pytest.mark.asyncio
async def test_publishing_writes_the_head_the_api_reads(database, tmp_path):
    policy, last, first = await benchmark_scenario(database, tmp_path)
    draft = calculate(database, policy, as_of=last + timedelta(hours=1))
    stored = heads(database)
    head, api = stored[int(first["snapshot_id"])]
    assert head["schema_version"] == 4 and head["networks"] and "parts" in head
    assert {f["cohort"] for f in head["fleet"]} > {"*"}  # per-cohort rows are left out
    assert json.loads(api) == expected(head) and "overlap" in head
    assert stored[int(draft["snapshot_id"])][1] is None  # a draft gets it when published
    store.publish(database, draft["snapshot_id"])
    head, api = heads(database)[int(draft["snapshot_id"])]
    assert json.loads(api) == expected(head)
    with connect(database) as conn:
        assert parts.write_api_head(conn) is False  # written once


@pytest.mark.asyncio
async def test_a_head_before_v4_gets_no_api_head(database, tmp_path):
    _, _, first = await benchmark_scenario(database, tmp_path)
    build = int(first["snapshot_id"])
    with connect(database) as conn:
        conn.execute(
            "UPDATE calculated_snapshots SET api_head=NULL,payload=payload-'schema_version' "
            "WHERE build_id=%s",
            (build,),
        )
        assert parts.write_api_head(conn) is False
        conn.execute(
            "UPDATE calculated_snapshots SET payload=payload-'networks'||'{\"schema_version\":4}' "
            "WHERE build_id=%s",
            (build,),
        )
        assert parts.write_api_head(conn) is False
    assert heads(database)[build][1] is None


@pytest.mark.asyncio
async def test_v13_layer_gains_the_api_head_and_keeps_every_build(
    databases, database, calculated_template, tmp_path
):
    await benchmark_scenario(database, tmp_path)
    with connect(database) as conn:
        conn.execute("ALTER TABLE calculated_snapshots DROP COLUMN api_head")
        conn.execute("UPDATE calculation_schema SET version=13,checksum=%s", (store.V13_CHECKSUM,))
        before = conn.execute(f"SELECT {KEPT} FROM calculated_snapshots ORDER BY 1").fetchall()
    result = migrate(database)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        13,
        14,
    )
    assert result["verified"] == list(store.V13_CHECKS)
    with connect(database) as conn:
        after = conn.execute(f"SELECT {KEPT} FROM calculated_snapshots ORDER BY 1").fetchall()
        assert after == before and before
        assert conn.execute("SELECT count(api_head) FROM calculated_snapshots").fetchone() == (0,)
        # The check counts a build the migration would have changed.
        conn.execute(store.V13_SNAPSHOT)
        query = "UPDATE calculated_snapshots SET content_hash='x' WHERE build_id=%s"
        conn.execute(query, (before[0][0],))
        check = store.V13_CHECKS["snapshots kept with the API head"]
        assert conn.execute(check).fetchone() == (1,)
        conn.rollback()
    assert catalog(database) == catalog(databases(template=calculated_template))


@pytest.mark.asyncio
async def test_list_entries_count_the_stored_rows(database, tmp_path):
    await benchmark_scenario(database, tmp_path)
    lists = stored_lists(database)
    cohorts = {cohort == netlists.COHORT for cohort, _, _ in lists.values()}
    assert cohorts == {True, False}  # group lists and network lists (the MERGE)
    for cohort, entries, counted in lists.values():
        names = netlists.DIMENSIONS if cohort == netlists.COHORT else BREAKDOWNS
        assert set(entries) == set(names) and set(counted) <= set(names.values())
        assert entries == {name: counted.get(code, 0) for name, code in names.items()}


@pytest.mark.asyncio
async def test_the_api_names_each_dimension_as_the_framework_counts_it(database, tmp_path):
    api = api_dimensions()
    assert api == {code: name for name, code in netlists.DIMENSIONS.items()}
    await benchmark_scenario(database, tmp_path)
    network = [v for v in stored_lists(database).values() if v[0] == netlists.COHORT]
    assert network
    for _, entries, counted in network:
        assert {code: entries[name] for code, name in api.items()} == {
            code: counted.get(code, 0) for code in api
        }
