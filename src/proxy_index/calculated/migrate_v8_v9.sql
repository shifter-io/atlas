-- Transactional calculated v8 → v9 migration (contract K2, lists.py), applied by
-- storage.migrate() after the earlier calculated steps. Every target-free window gets one list
-- (signature NULL) holding its breakdown rows, numbered with the window's result_id; the
-- identity then continues after the highest. A migrated list's content_hash is the sha256 of its
-- rows' JSON array, its figures the pool numbers calculated_windows kept (the payload holds the
-- rest). calculated_breakdowns and its per-build partitions are then dropped. store.V8_CHECKS
-- compares every row with the copy taken by store.V8_SNAPSHOT; schema.sql then recreates the
-- breakdown views on the lists.
DROP VIEW IF EXISTS calculated_breakdown_details, current_breakdowns;
CREATE TABLE calculated_lists (
 list_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 product_id integer NOT NULL REFERENCES products(product_id), cohort text NOT NULL,
 window_hours integer NOT NULL, signature text, content_hash text NOT NULL,
 entries jsonb NOT NULL, figures jsonb NOT NULL, created_build bigint NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(product_id,cohort,window_hours,signature)
);
CREATE TABLE calculated_list_rows (
 list_id bigint NOT NULL REFERENCES calculated_lists(list_id), dimension "char" NOT NULL
  CHECK(dimension IN ('g','c','a','r','x','y')), position integer NOT NULL,
 place_id integer REFERENCES place_names(place_id),
 asn_label_id integer REFERENCES asn_labels(asn_label_id),
 ips bigint NOT NULL, observations bigint NOT NULL,
 completed bigint, successes bigint, challenges bigint, blocks bigint,
 p50 integer, p90 integer, p99 integer,
 CHECK((place_id IS NULL) = (dimension = 'a')
  AND (asn_label_id IS NULL) = (dimension IN ('g','c','r'))),
 PRIMARY KEY(list_id,dimension,position)
) WITH (autovacuum_vacuum_scale_factor = 0.01, autovacuum_vacuum_insert_scale_factor = 0.01);
ALTER TABLE calculated_windows ADD COLUMN list_id bigint REFERENCES calculated_lists(list_id);
CREATE INDEX calculated_windows_list ON calculated_windows(list_id);
INSERT INTO calculated_lists(list_id,product_id,cohort,window_hours,content_hash,entries,figures,
 created_build,created_at) OVERRIDING SYSTEM VALUE
SELECT w.result_id,w.product_id,w.cohort,w.window_hours,
 encode(sha256(convert_to(coalesce(r.rows,'[]')::text,'UTF8')),'hex'),
 coalesce(r.entries,jsonb_build_object('country',0,'region',0,'city',0,'asn',0,
  'country_asn',0,'city_asn',0)),
 jsonb_build_object('unique_ips',w.unique_ips,'recurring_share',w.recurring_share,
  'countries',w.countries,'regions',w.regions,'cities',w.cities,'asns',w.asns),
 w.build_id,cb.created_at
FROM calculated_windows w JOIN calculation_builds cb USING(build_id)
LEFT JOIN (SELECT b.build_id,b.result_id,
  jsonb_agg(to_jsonb(b) - 'build_id' - 'result_id' ORDER BY b.dimension,b.position) AS rows,
  jsonb_build_object('country',count(*) FILTER (WHERE b.dimension='c'),
   'region',count(*) FILTER (WHERE b.dimension='r'),'city',count(*) FILTER (WHERE b.dimension='g'),
   'asn',count(*) FILTER (WHERE b.dimension='a'),
   'country_asn',count(*) FILTER (WHERE b.dimension='x'),
   'city_asn',count(*) FILTER (WHERE b.dimension='y')) AS entries
 FROM calculated_breakdowns b GROUP BY 1,2) r USING(build_id,result_id)
WHERE w.target='';
SELECT setval(pg_get_serial_sequence('calculated_lists','list_id'),coalesce(max(list_id),1),
 max(list_id) IS NOT NULL) FROM calculated_lists;
INSERT INTO calculated_list_rows SELECT result_id,dimension,position,place_id,asn_label_id,ips,
 observations,completed,successes,challenges,blocks,p50,p90,p99 FROM calculated_breakdowns
ORDER BY result_id,dimension,position;
UPDATE calculated_windows SET list_id=result_id WHERE target='';
DROP TABLE calculated_breakdowns;
