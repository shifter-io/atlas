"""Schema v9: one shared list of place and ASN names for enrichment locations and networks."""

import pytest
from test_storage import enrichment, measurement
from test_storage_v6 import BERLIN, NETWORK, census

from atlas import names
from atlas.storage import connect, ingest

pytest_plugins = ["test_storage"]

BERLIN_NAMES = (
    "SELECT (SELECT count(*) FROM place_names WHERE city='Berlin'),"
    "(SELECT count(*) FROM enrichment_location_fields WHERE city='Berlin'),"
    "(SELECT count(*) FROM asn_names WHERE asn=3320),"
    "(SELECT count(*) FROM enrichment_network_fields WHERE asn=3320)"
)


@pytest.mark.asyncio
async def test_one_name_row_per_place_and_asn_across_coordinates_and_sinks(database, tmp_path):
    first, _ = await census(tmp_path)
    ingest(first, database)
    moved = BERLIN | {"latitude": 52.53}
    second, _ = await census(tmp_path, location=moved, network=NETWORK | {"isp": "Other ISP"})
    ingest(second, database)
    with connect(database) as conn:
        assert conn.execute(BERLIN_NAMES).fetchone() == (1, 2, 1, 2)
    # The IP enrichment path, at a third coordinate, reuses the same names: three locations from
    # both sinks refer to one Berlin row and two networks to one ASN row.
    third = await measurement(tmp_path)
    enrichment(third, attributes={"matched": True} | BERLIN | {"latitude": 52.54} | NETWORK)
    ingest(third, database)
    with connect(database) as conn:
        assert conn.execute(BERLIN_NAMES).fetchone() == (1, 3, 1, 2)


@pytest.mark.asyncio
async def test_unknown_place_and_asn_are_the_all_null_rows(database, tmp_path):
    for _ in range(2):
        directory = await measurement(tmp_path)
        enrichment(directory, attributes={"matched": False})
        ingest(directory, database)
    with connect(database) as conn:
        assert conn.execute(
            "SELECT country_code,country,region_code,region,city FROM place_names"
        ).fetchall() == [(None,) * 5]
        assert conn.execute("SELECT asn,name FROM asn_names").fetchall() == [(None, None)]
        assert conn.execute(
            "SELECT count(*) FROM enrichment_locations JOIN place_names USING(place_id)"
        ).fetchone() == (1,)


def test_fill_is_idempotent_and_lookup_is_null_safe(database):
    source = "(VALUES ('DE','Berlin'),('DE',NULL),(NULL,NULL),('DE','Berlin')) s(code,city)"
    values = ["s.code", "NULL", "NULL", "NULL", "s.city"]
    with connect(database) as conn:
        for _ in range(2):
            names.fill(conn, "place_names", values, source)
        found = conn.execute(
            f"SELECT s.code,s.city,place_id.place_id FROM {source} "
            f"{names.lookup('place_names', values)}"
        ).fetchall()
        stored = conn.execute("SELECT count(*) FROM place_names").fetchone()[0]
    ids = {(code, city): place_id for code, city, place_id in found}
    assert len(found) == 4 and len(set(ids.values())) == len(ids) == stored == 3
