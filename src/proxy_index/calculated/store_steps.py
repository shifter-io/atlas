"""Calculated-layer migration steps that storage.migrate() reads from store.py, kept here because
store.py is at its size limit (store.py re-exports them)."""

# Checksum of the unmodified calculated v10 schema (contract K6's memo tables).
V10_CHECKSUM = "1923805874141815e1af2beaf7176cc3f25a3101ad9b33d3501ae7346f5870eb"
# A v10 layer gains the empty per-hour geo rollups (CALC-PULSE-1a); its lists stay as they were.
V10_SNAPSHOT = "CREATE TEMP TABLE v10_before ON COMMIT DROP AS SELECT * FROM calculated_lists"
V10_CHECKS = {
    "lists kept with the geo rollups": "SELECT count(*) FROM v10_before o FULL JOIN "
    "calculated_lists n USING(list_id) WHERE row(o.*)::text IS DISTINCT FROM row(n.*)::text",
}
