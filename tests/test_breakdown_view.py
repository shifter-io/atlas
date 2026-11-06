"""calculated_breakdown_details is a reader contract (the website's long-tail API reads it): its
columns and its city_asn and asn labels, usage_type included, stay the same whatever tables hold
the place and ASN names behind it."""

import json

import pytest
from calculated_cases import benchmark_scenario, payload_mismatches

from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

COLUMNS = [
    ("result_id", "bigint"),
    ("dimension", "text"),
    ("position", "integer"),
    ("ips", "bigint"),
    ("observations", "bigint"),
    ("label", "jsonb"),
    ("completed", "bigint"),
    ("successes", "bigint"),
    ("success_rate", "double precision"),
    ("challenges", "bigint"),
    ("blocks", "bigint"),
    ("p50", "integer"),
    ("p90", "integer"),
    ("p99", "integer"),
]
PLACE = {"country_code": "US", "country": "United States", "region_code": "CA"}
ASN = {"asn": 15169, "name": "Network", "usage_type": "residential"}
LABELS = (
    ("asn", ASN),
    ("city_asn", PLACE | {"region": "California", "city": "Los Angeles"} | ASN),
    ("city_asn", PLACE | {"region": "California", "city": "San Jose"} | ASN),
)


@pytest.mark.asyncio
async def test_breakdown_details_columns_and_asn_labels_are_unchanged(database, tmp_path):
    _, _, first = await benchmark_scenario(database, tmp_path)
    with connect(database) as conn:
        columns = conn.execute(
            "SELECT column_name::text,data_type::text FROM information_schema.columns "
            "WHERE table_name='calculated_breakdown_details' ORDER BY ordinal_position"
        ).fetchall()
        labels = {
            (dimension, json.dumps(label, sort_keys=True))
            for dimension, label in conn.execute(
                "SELECT dimension,label FROM calculated_breakdown_details "
                "WHERE dimension IN ('city_asn','asn')"
            )
        }
        types = conn.execute(
            "SELECT DISTINCT jsonb_typeof(label->'asn'),jsonb_typeof(label->'usage_type') "
            "FROM calculated_breakdown_details WHERE dimension IN ('city_asn','asn')"
        ).fetchall()
    assert columns == COLUMNS
    assert labels == {(d, json.dumps(label, sort_keys=True)) for d, label in LABELS}
    assert types == [("number", "string")]
    assert payload_mismatches(database, int(first["snapshot_id"])) == []
