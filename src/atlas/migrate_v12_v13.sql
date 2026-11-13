-- Transactional v12 → v13 migration, applied by storage.migrate() (proxy-index db-migrate),
-- alone from v12 or after the earlier steps. It adds test_event_buckets, the live tape's counts
-- per 30-minute bucket, product and tape tab, the statement trigger on test_events that keeps it
-- on every insert, and fills it from the stored events. Creating the trigger locks test_events
-- against writes until the migration commits, so no event is inserted between the trigger and
-- the fill. The definitions repeat schema.sql exactly (tests compare catalogs).
CREATE TABLE test_event_buckets (
 bucket_start timestamptz NOT NULL, product_id integer NOT NULL, category text NOT NULL,
 events integer NOT NULL, good integer NOT NULL, bad integer NOT NULL, last_at timestamptz NOT NULL,
 PRIMARY KEY(bucket_start,product_id,category)
);
CREATE OR REPLACE VIEW test_event_bucket_counts AS
SELECT date_bin('30 minutes',at,'2000-01-01 00:00+00') AS bucket_start,
 coalesce(product_id,0) AS product_id,CASE WHEN left(kind,7)='census_'
 OR kind IN ('daily_test_started','daily_test_finished','estimate_hour_released') THEN 'census'
 WHEN left(kind,6)='pulse_' THEN 'pulse' WHEN kind IN ('new_hosting_asn','ip_overlap') THEN 'audit'
 ELSE 'incident' END AS category,count(*)::integer AS events,
 (count(*) FILTER (WHERE signal>0))::integer AS good,
 (count(*) FILTER (WHERE signal<0))::integer AS bad,max(at) AS last_at
FROM test_events GROUP BY 1,2,3;
-- One upsert per statement, its keys in order (concurrent inserts lock rows in the same order); it
-- calls no function, so the roles that insert events need no EXECUTE right.
CREATE OR REPLACE FUNCTION test_event_buckets_add() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO test_event_buckets AS b
 SELECT date_bin('30 minutes',at,'2000-01-01 00:00+00'),coalesce(product_id,0),
  CASE WHEN left(kind,7)='census_'
  OR kind IN ('daily_test_started','daily_test_finished','estimate_hour_released') THEN 'census'
  WHEN left(kind,6)='pulse_' THEN 'pulse' WHEN kind IN ('new_hosting_asn','ip_overlap') THEN 'audit'
  ELSE 'incident' END,count(*),count(*) FILTER (WHERE signal>0),count(*) FILTER (WHERE signal<0),
  max(at)
 FROM added GROUP BY 1,2,3 ORDER BY 1,2,3
 ON CONFLICT (bucket_start,product_id,category) DO UPDATE SET events=b.events+excluded.events,
  good=b.good+excluded.good,bad=b.bad+excluded.bad,last_at=greatest(b.last_at,excluded.last_at);
 RETURN NULL;
END$$;
CREATE TRIGGER test_event_buckets_add AFTER INSERT ON test_events REFERENCING NEW TABLE AS added
FOR EACH STATEMENT EXECUTE FUNCTION test_event_buckets_add();
INSERT INTO test_event_buckets SELECT * FROM test_event_bucket_counts;
