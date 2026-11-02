-- Transactional calculated v5 → v6 migration, applied by storage.migrate() after the earlier
-- calculated steps. calculated_breakdowns becomes a table partitioned by build: every row gains
-- the build_id of its window and moves to that build's partition. store.V5_CHECKS compares the
-- moved rows with the copy taken by store.V5_SNAPSHOT. The views are replaced by schema.sql.
-- Rows are copied once, so on a large database run proxy-index calculate-expire first (it
-- accepts calculated v5): only the kept builds move.
ALTER TABLE calculated_windows ADD UNIQUE(result_id,build_id);
DROP VIEW IF EXISTS calculated_breakdown_details;
ALTER TABLE calculated_breakdowns RENAME TO calculated_breakdowns_v5;
ALTER INDEX calculated_breakdowns_pkey RENAME TO calculated_breakdowns_v5_pkey;
-- Generated constraint names are unique per schema: free them for the new table.
DO $$
DECLARE name text;
BEGIN
 FOR name IN SELECT conname FROM pg_constraint
  WHERE conrelid='calculated_breakdowns_v5'::regclass AND contype<>'p' LOOP
  EXECUTE format('ALTER TABLE calculated_breakdowns_v5 DROP CONSTRAINT %I', name);
 END LOOP;
END $$;
CREATE TABLE calculated_breakdowns (
 build_id bigint NOT NULL, result_id bigint NOT NULL,
 ips bigint NOT NULL, observations bigint NOT NULL,
 place_id integer REFERENCES places(place_id), asn_label_id integer REFERENCES asn_labels(asn_label_id),
 position integer NOT NULL, dimension "char" NOT NULL CHECK(dimension IN ('g','c','a','r','x','y')),
 completed bigint, successes bigint, challenges bigint, blocks bigint,
 p50 integer, p90 integer, p99 integer,
 CHECK((place_id IS NULL) = (dimension = 'a')
  AND (asn_label_id IS NULL) = (dimension IN ('g','c','r'))),
 PRIMARY KEY(result_id,dimension,position,build_id),
 FOREIGN KEY(result_id,build_id) REFERENCES calculated_windows(result_id,build_id)
) PARTITION BY LIST(build_id);
DO $$
DECLARE build bigint;
BEGIN
 FOR build IN SELECT DISTINCT w.build_id FROM calculated_breakdowns_v5 o
  JOIN calculated_windows w USING(result_id) ORDER BY 1 LOOP
  EXECUTE format('CREATE TABLE %I PARTITION OF calculated_breakdowns FOR VALUES IN (%s)',
   'calculated_breakdowns_b' || build, build);
 END LOOP;
END $$;
INSERT INTO calculated_breakdowns(build_id,result_id,ips,observations,place_id,asn_label_id,
 position,dimension,completed,successes,challenges,blocks,p50,p90,p99)
SELECT w.build_id,o.result_id,o.ips,o.observations,o.place_id,o.asn_label_id,o.position,
 o.dimension,o.completed,o.successes,o.challenges,o.blocks,o.p50,o.p90,o.p99
FROM calculated_breakdowns_v5 o JOIN calculated_windows w USING(result_id);
DROP TABLE calculated_breakdowns_v5;
