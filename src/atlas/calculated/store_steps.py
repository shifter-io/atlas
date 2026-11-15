"""Calculated-layer schema version, checksums and migration steps that storage.migrate() reads
from store.py, kept here because store.py is at its size limit (store.py re-exports every name)
and store_retention.py checks the layer without importing store.py."""

import hashlib
from pathlib import Path

SCHEMA_VERSION = 16
# Checksums of the unmodified v1 and v2 calculated schemas that storage.migrate() upgrades in place.
V1_CHECKSUM = "2d4157aeadac7da566ab1edde723ad425cb068318c4a3722ca94f3c57d9f68a0"
V2_CHECKSUM = "19bf2db0cd112b87d2de06aa0156d2693f36d8e21c2c2b1215ecc00ccb7f7699"
V3_CHECKSUM = "5f24879fc7f37486903881c91f5334f85cfa83dcffd90fc00a972508424b394d"
V4_CHECKSUM = "83d3794413bb56b371ff04ac7f9978652310b20013077c0400d1477d7fb24938"
V5_CHECKSUM = "ccb566016dba5d6d18b09da7cb88ced16758f0fbf446fcac88495d67d94b8782"
V6_CHECKSUM = "140ad11f64d300f4e8104da53421bcf00816464fa052d854082831775286dd26"
V7_CHECKSUM = "66c66164b5915e2015382de9f640d997d1cece6df88f2ab3436f265a597a661a"
V8_CHECKSUM = "65f217a8497750078be93711d245f9de7c873c933a67146f38b6520bcb4de277"
V9_CHECKSUM = "0efaf3f1c8b25cf7866263c851499edf73ee997dce8211c265c1707c1bd49a54"


def schema():
    ddl = Path(__file__).with_name("schema.sql").read_text()
    return ddl, hashlib.sha256(ddl.encode()).hexdigest()


# Checksum of the unmodified calculated v10 schema (contract K6's memo tables).
V10_CHECKSUM = "1923805874141815e1af2beaf7176cc3f25a3101ad9b33d3501ae7346f5870eb"
# A v10 layer gains the empty per-hour geo rollups (CALC-PULSE-1a); its lists stay as they were.
V10_SNAPSHOT = "CREATE TEMP TABLE v10_before ON COMMIT DROP AS SELECT * FROM calculated_lists"
V10_CHECKS = {
    "lists kept with the geo rollups": "SELECT count(*) FROM v10_before o FULL JOIN "
    "calculated_lists n USING(list_id) WHERE row(o.*)::text IS DISTINCT FROM row(n.*)::text",
}

# Checksum of the unmodified calculated v11 schema (CALC-PULSE-1a's geo rollups).
V11_CHECKSUM = "8a3393f67ce3d90c374fb106b27a47da909f9b3ad3a226f5eb3df0c795652f3b"
# A v11 layer gains the empty per-hour batch-run buckets (CALC-PULSE-1b); its lists stay.
V11_SNAPSHOT = "CREATE TEMP TABLE v11_before ON COMMIT DROP AS SELECT * FROM calculated_lists"
V11_CHECKS = {
    "lists kept with the pulse buckets": "SELECT count(*) FROM v11_before o FULL JOIN "
    "calculated_lists n USING(list_id) WHERE row(o.*)::text IS DISTINCT FROM row(n.*)::text",
}

# Checksum of the unmodified calculated v12 schema (CALC-PULSE-1b's pulse buckets).
V12_CHECKSUM = "2051da8ffb4b9cf9b91cbf2c8469a96d9899f4a1570e0da018f34818f9da3efd"
# A v12 layer's list rows lose their foreign key to calculated_lists; every row stays.
V12_SNAPSHOT = (
    "CREATE TEMP TABLE v12_before ON COMMIT DROP AS "
    "SELECT list_id,count(*) AS n FROM calculated_list_rows GROUP BY list_id"
)
V12_CHECKS = {
    "list rows kept without the key": "SELECT count(*) FROM v12_before o FULL JOIN (SELECT "
    "list_id,count(*) AS n FROM calculated_list_rows GROUP BY list_id) n USING(list_id) "
    "WHERE o.n IS DISTINCT FROM n.n",
    "every list row has its list": "SELECT count(*) FROM calculated_list_rows r WHERE NOT EXISTS "
    "(SELECT FROM calculated_lists l WHERE l.list_id=r.list_id)",
}

# Checksum of the unmodified calculated v13 schema (LISTS-WRITE-1's list rows without the key).
V13_CHECKSUM = "4fb1e90cab6d8185a9f4cc58fa13351247ecdac689e0eb17a0b76a5a3ff2ffbd"
# A v13 layer's snapshots gain api_head, which no stored build gets; every head and hash stays.
V13_SNAPSHOT = (
    "CREATE TEMP TABLE v13_before ON COMMIT DROP AS SELECT build_id,content_hash,"
    "sha256(convert_to(payload::text,'UTF8')) AS payload FROM calculated_snapshots"
)
V13_CHECKS = {
    "snapshots kept with the API head": "SELECT count(*) FROM v13_before o FULL JOIN "
    "calculated_snapshots n USING(build_id) WHERE (o.content_hash,o.payload) IS DISTINCT FROM "
    "(n.content_hash,sha256(convert_to(n.payload::text,'UTF8')))",
}

# Checksum of the unmodified calculated v14 schema (API-LOAD-STATEMENTS-1's API head).
V14_CHECKSUM = "66e085808d95234e95758e282577b5944f6460b03957868124b8d6b98840a256"
# A v14 layer's list rows gain ips_rank and ips_peers, which no stored row gets; every row stays.
V14_SNAPSHOT = (
    "CREATE TEMP TABLE v14_before ON COMMIT DROP AS "
    "SELECT list_id,count(*) AS n,sum(ips) AS ips FROM calculated_list_rows GROUP BY list_id"
)
V14_CHECKS = {
    "list rows kept with the IP ranks": "SELECT count(*) FROM v14_before o FULL JOIN (SELECT "
    "list_id,count(*) AS n,sum(ips) AS ips FROM calculated_list_rows GROUP BY list_id) n "
    "USING(list_id) WHERE (o.n,o.ips) IS DISTINCT FROM (n.n,n.ips)",
}

# Checksum of the unmodified calculated v15 schema (COVERAGE-IPS-RANK-1's list row IP ranks).
V15_CHECKSUM = "7474acb4525dd3b1b032ffe2b0e61829380b1995522b02abc84a1e5aff636c68"
# A v15 layer gains the empty per-scope IP state (CALC-SCALE-1); its bucket memo rows stay.
V15_SNAPSHOT = (
    "CREATE TEMP TABLE v15_before ON COMMIT DROP AS "
    "SELECT bucket_start,runs_digest,unique_ips FROM calculated_bucket_ips"
)
V15_CHECKS = {
    "bucket memos kept with the IP state": "SELECT count(*) FROM v15_before o FULL JOIN "
    "calculated_bucket_ips n USING(bucket_start) WHERE (o.runs_digest,o.unique_ips) "
    "IS DISTINCT FROM (n.runs_digest,n.unique_ips)",
}
