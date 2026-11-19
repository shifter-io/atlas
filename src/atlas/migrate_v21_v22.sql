-- Transactional v21 → v22 migration, applied by storage.migrate() (atlas db-migrate), alone from
-- v21 or after the earlier steps (VERIFY-PAUSED-BATCH-1, #1299). test_batches.paused_network: the
-- network whose pause skipped a browser batch at planning (orchestrator/browser.py), so the hour
-- report expects it instead of counting it as a failure. A nullable column: no row changes and no
-- rewrite. The definition repeats schema.sql exactly (tests compare catalogs).
ALTER TABLE test_batches ADD COLUMN paused_network text;
