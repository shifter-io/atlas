-- Transactional v6 → v7 migration, applied by storage.migrate() (milf-framework db-migrate),
-- alone from v6 or after the earlier steps. The latency-under-load test runs as test_batches of
-- kind 'load', and a census the orchestrator reruns after a provider outage names the census it
-- replaces (retry_of). No stored row changes. Definitions repeat schema.sql exactly (tests
-- compare catalogs).
ALTER TABLE test_batches DROP CONSTRAINT test_batches_kind_check;
ALTER TABLE test_batches ADD CONSTRAINT test_batches_kind_check
 CHECK(kind IN ('census','pulse','load'));
ALTER TABLE test_batches ADD COLUMN retry_of bigint REFERENCES test_batches(batch_id);
