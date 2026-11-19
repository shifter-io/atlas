"""CALC-PERF-4: list rows without the foreign keys to their places and ASN labels.

Since calculated v23, calculated_list_rows.place_id and asn_label_id have no foreign key, whose
per-row checks were most of a build's list-row writes. These tests show that a build's rows all
have their place and label (store_steps.LABEL_ORPHANS finds none), that the query and the nightly
`db-server analyze` line count a row whose place is missing, and that a v22 layer migrates
without rewriting a row.
"""

import pytest
from calculated_cases import benchmark_scenario
from migration_cases import catalog
from test_calculated_list_write import TOTALS

from atlas.calculated import store
from atlas.calculated.calculator import calculate
from atlas.storage import connect, migrate

pytest_plugins = ["test_storage", "calculated_template"]

ROWS = "SELECT * FROM calculated_list_rows ORDER BY list_id,dimension,position"
# The rows that name a place or an ASN label (the ones a missing label would leave dangling).
LABELLED = (
    "SELECT count(*) FILTER (WHERE place_id IS NOT NULL),"
    "count(*) FILTER (WHERE asn_label_id IS NOT NULL) FROM calculated_list_rows"
)
NETWORK_LISTS = "SELECT count(*) FROM calculated_lists WHERE cohort='network'"
# A country row whose place is not in place_names.
ORPHAN = (
    "INSERT INTO calculated_list_rows(list_id,dimension,position,place_id,ips,observations) "
    "SELECT -1,'c',0,max(place_id)+1000,1,1 FROM place_names"
)


@pytest.mark.asyncio
async def test_every_list_row_of_a_build_has_its_place_and_label(database, tmp_path):
    policy, _, first = await benchmark_scenario(database, tmp_path)
    second = calculate(database, policy, as_of=first["as_of"], mode="recount", publish=True)
    assert second["snapshot_id"] != first["snapshot_id"]
    with connect(database) as conn:
        assert conn.execute(store.V22_CHECKS["list rows without label keys"]).fetchone() == (0,)
        places, labels = conn.execute(LABELLED).fetchone()
        assert places > 0 and labels > 0  # group and network rows name both
        assert conn.execute(store.LABEL_ORPHANS).fetchone() == (0,)
        assert conn.execute(NETWORK_LISTS).fetchone()[0] > 0
        conn.execute(ORPHAN)  # no key refuses it any more: the query must count it
        assert conn.execute(store.LABEL_ORPHANS).fetchone() == (1,)
        conn.execute(
            "UPDATE calculated_list_rows SET dimension='a',place_id=NULL,asn_label_id=-7 "
            "WHERE list_id=-1"
        )  # an ASN row whose label is missing
        assert conn.execute(store.LABEL_ORPHANS).fetchone() == (1,)
        conn.rollback()


@pytest.mark.asyncio
async def test_v22_layer_drops_the_label_keys_and_keeps_every_row(
    databases, database, calculated_template, tmp_path
):
    await benchmark_scenario(database, tmp_path)
    with connect(database) as conn:
        conn.execute(
            "ALTER TABLE calculated_list_rows ADD CONSTRAINT calculated_list_rows_place_id_fkey "
            "FOREIGN KEY(place_id) REFERENCES place_names(place_id), ADD CONSTRAINT "
            "calculated_list_rows_asn_label_id_fkey FOREIGN KEY(asn_label_id) "
            "REFERENCES asn_labels(asn_label_id)"
        )
        conn.execute("UPDATE calculation_schema SET version=22,checksum=%s", (store.V22_CHECKSUM,))
        before = conn.execute(ROWS).fetchall(), conn.execute(TOTALS).fetchone()
    result = migrate(database)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        22,
        23,
    )
    assert result["verified"] == list(store.V22_CHECKS)
    with connect(database) as conn:
        assert conn.execute(store.V22_CHECKS["list rows without label keys"]).fetchone() == (0,)
        assert (conn.execute(ROWS).fetchall(), conn.execute(TOTALS).fetchone()) == before
    assert catalog(database) == catalog(databases(template=calculated_template))
