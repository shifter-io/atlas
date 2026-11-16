-- Transactional v19 → v20 migration, applied by storage.migrate() (atlas db-migrate), alone from
-- v19 or after the earlier steps (OUTAGE-NOT-MEASURED-1, #1277). outages.end_reason may also be
-- 'not_measured': the pulse evaluator ends a network outage whose network had no pulse data in a
-- window there (orchestrator/pulse_unmeasured.py). No row changes. The definitions repeat
-- schema.sql exactly (tests compare catalogs).
ALTER TABLE outages DROP CONSTRAINT outages_end_reason_check,
 ADD CONSTRAINT outages_end_reason_check CHECK(end_reason IN ('too_few_ips','not_measured'));
