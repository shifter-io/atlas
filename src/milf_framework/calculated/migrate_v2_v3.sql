-- Transactional calculated v2 → v3 migration, applied by storage.migrate() after the base step
-- (and after migrate_v1_v2.sql when coming from v1). It only adds nullable columns and widens the
-- breakdown dimensions; stored rows keep their values, so every v2 payload fragment still rebuilds
-- (store.V2_CHECKS compares them with the copies taken by store.V2_SNAPSHOT). The view is
-- replaced by schema.sql, which appends the new breakdown columns.
ALTER TABLE calculated_windows ADD COLUMN challenge_rate double precision,
 ADD COLUMN block_rate double precision, ADD COLUMN uptime double precision,
 ADD COLUMN bytes_up bigint, ADD COLUMN bytes_down bigint;
ALTER TABLE calculated_breakdowns DROP CONSTRAINT calculated_breakdowns_dimension_check,
 DROP CONSTRAINT calculated_breakdowns_check,
 ADD COLUMN completed bigint, ADD COLUMN successes bigint, ADD COLUMN challenges bigint,
 ADD COLUMN blocks bigint, ADD COLUMN p50 integer, ADD COLUMN p90 integer, ADD COLUMN p99 integer,
 ADD CONSTRAINT calculated_breakdowns_dimension_check CHECK(dimension IN ('g','c','a','r','x')),
 ADD CONSTRAINT calculated_breakdowns_check CHECK((place_id IS NULL) = (dimension = 'a')
  AND (asn_label_id IS NULL) = (dimension IN ('g','c','r')));
