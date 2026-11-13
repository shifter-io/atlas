-- Transactional v10 → v11 migration, applied by storage.migrate() (proxy-index db-migrate),
-- alone from v10 or after the earlier steps. It adds the batch kind 'browser' (the browser
-- pulse); no stored row changes. The definition repeats schema.sql exactly (tests compare
-- catalogs).
ALTER TABLE test_batches DROP CONSTRAINT test_batches_kind_check;
ALTER TABLE test_batches ADD CONSTRAINT test_batches_kind_check
 CHECK(kind IN ('census','pulse','load','browser'));
