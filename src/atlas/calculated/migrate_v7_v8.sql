-- Transactional calculated v7 → v8 migration, applied by storage.migrate() after the earlier
-- calculated steps. A build stored from v8 on keeps its product groups and runs in
-- calculated_snapshot_parts (contract K1, parts.py); every kept build keeps its single payload
-- row, which every reader still accepts. store.V7_CHECKS compares each snapshot with the copy
-- taken by store.V7_SNAPSHOT. schema.sql then recreates calculated_snapshots_list, whose
-- payload_bytes adds the parts.
DROP VIEW IF EXISTS calculated_snapshots_list;
CREATE TABLE calculated_snapshot_parts (
 build_id bigint NOT NULL REFERENCES calculated_snapshots(build_id) DEFERRABLE INITIALLY DEFERRED,
 kind text NOT NULL CHECK(kind IN ('product','runs')), position integer NOT NULL CHECK(position>=0),
 part_id text NOT NULL, product_id integer, cohort text, content_hash text NOT NULL,
 part jsonb NOT NULL, CHECK((kind='product')=(product_id IS NOT NULL AND cohort IS NOT NULL)),
 PRIMARY KEY(build_id,kind,position)
) PARTITION BY LIST(build_id);
