-- Transactional calculated v22 → v23 migration (CALC-PERF-4), applied by storage.migrate() after
-- the earlier calculated steps: calculated_list_rows loses its foreign keys to place_names and
-- asn_labels. Their per-row checks were about 80% of a build's list-row writes (9.8 µs a row on a
-- copy, about 97 of 123 s for one build's network rows). Dropping a constraint rewrites and reads
-- no row; it holds the three tables' locks for a moment, so run it while no calculation runs.
-- store.V22_CHECKS checks neither key is left. No row of either table is ever deleted, and the
-- writers take each id from a row they read or insert in the same transaction (list_write.py,
-- netlists.py); store_steps.LABEL_ORPHANS counts the rows whose place or label is missing.
-- IF EXISTS: a layer rebuilt from this schema's tables, as some tests do, never had them.
ALTER TABLE calculated_list_rows DROP CONSTRAINT IF EXISTS calculated_list_rows_place_id_fkey,
 DROP CONSTRAINT IF EXISTS calculated_list_rows_asn_label_id_fkey;
