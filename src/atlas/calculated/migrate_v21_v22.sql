-- Transactional calculated v21 → v22 migration (CALC-PERF-1), applied by storage.migrate()
-- after the earlier calculated steps: a partial index of the geo hours of runs outside a batch
-- (schema.sql describes it), which rollups.STALE reads instead of every geo hour. No row is
-- written; building it reads calculated_geo_hours once and holds off its writes meanwhile, so run
-- it while no calculation runs. store.V21_CHECKS checks the index exists.
CREATE INDEX IF NOT EXISTS calculated_geo_hours_run ON calculated_geo_hours(run_key)
 WHERE run_key IS NOT NULL;
