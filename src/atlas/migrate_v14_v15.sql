-- Transactional v14 → v15 migration, applied by storage.migrate() (proxy-index db-migrate),
-- alone from v14 or after the earlier steps. It adds three partial indexes to test_events for the
-- API's rare-value reads (DB-EXPLAIN-1): the non-routine rows, and the rows with a country or a
-- target. No stored row changes. Each index is made on the parent only, then built on every
-- monthly partition and attached to it; the last attach makes it valid. A partition's build
-- blocks writes to that partition until the migration commits, which follows at once; the tape
-- is small (about 2.5k rows a day). Partitions made later get the same index from the parent,
-- named as PostgreSQL names it (<partition>_<columns>_idx), as these are. The definitions repeat
-- schema.sql exactly (tests compare catalogs).
DO $$
DECLARE
 i record;
 p text;
BEGIN
 FOR i IN SELECT * FROM (VALUES
  ('test_events_notable','at_event_id_idx',$d$(at DESC,event_id DESC)
 WHERE NOT (left(kind,7)='census_' OR kind IN
 ('daily_test_started','daily_test_finished','estimate_hour_released','pulse_finished'))$d$),
  ('test_events_country','country_code_at_idx',
   '(country_code,at DESC) WHERE country_code IS NOT NULL'),
  ('test_events_target','target_id_at_idx','(target_id,at DESC) WHERE target_id IS NOT NULL'))
  AS v(name,suffix,definition)
 LOOP
  EXECUTE format('CREATE INDEX %I ON ONLY test_events %s',i.name,i.definition);
  FOR p IN SELECT c.relname FROM pg_inherits h JOIN pg_class c ON c.oid=h.inhrelid
   WHERE h.inhparent='test_events'::regclass ORDER BY 1
  LOOP
   EXECUTE format('CREATE INDEX %I ON %I %s',p||'_'||i.suffix,p,i.definition);
   EXECUTE format('ALTER INDEX %I ATTACH PARTITION %I',i.name,p||'_'||i.suffix);
  END LOOP;
 END LOOP;
END$$;
