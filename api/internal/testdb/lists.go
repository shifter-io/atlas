package testdb

import "testing"

// ListTables are the shared list tables of proxy-index calculated v9
// (contract K2, CALC-LISTS-1a), for a framework schema older than that:
// calculated_lists, calculated_list_rows and calculated_windows.list_id.
const ListTables = `CREATE TABLE IF NOT EXISTS calculated_lists (
 list_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, product_id integer, cohort text, window_hours integer,
 signature text, content_hash text NOT NULL DEFAULT 'test', entries jsonb NOT NULL DEFAULT '{}',
 figures jsonb NOT NULL DEFAULT '{}', created_build bigint, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(product_id,cohort,window_hours,signature)
);
CREATE TABLE IF NOT EXISTS calculated_list_rows (
 list_id bigint NOT NULL REFERENCES calculated_lists(list_id), dimension "char" NOT NULL,
 position integer NOT NULL, place_id integer REFERENCES place_names(place_id),
 asn_label_id integer REFERENCES asn_labels(asn_label_id), ips bigint NOT NULL, observations bigint NOT NULL,
 completed bigint, successes bigint, challenges bigint, blocks bigint, p50 integer, p90 integer, p99 integer,
 PRIMARY KEY(list_id,dimension,position)
) WITH (autovacuum_vacuum_scale_factor = 0.01, autovacuum_vacuum_insert_scale_factor = 0.01);
ALTER TABLE calculated_windows ADD COLUMN IF NOT EXISTS list_id bigint REFERENCES calculated_lists(list_id)`

// MoveToLists does what calculated v9 does to a database: it creates the
// list tables, moves each target-free window's calculated_breakdowns rows into
// a list of its own (signature NULL) and drops calculated_breakdowns.
func (d *DB) MoveToLists(t testing.TB) {
	t.Helper()
	d.Exec(t, ListTables)
	d.Exec(t, `DO $$ DECLARE w record; id bigint; BEGIN
 FOR w IN SELECT * FROM calculated_windows WHERE target = '' AND result_id IN (SELECT result_id FROM calculated_breakdowns) LOOP
  INSERT INTO calculated_lists(product_id,cohort,window_hours,created_build) VALUES (w.product_id,w.cohort,w.window_hours,w.build_id)
   RETURNING list_id INTO id;
  INSERT INTO calculated_list_rows SELECT id,dimension,position,place_id,asn_label_id,ips,observations,completed,successes,
   challenges,blocks,p50,p90,p99 FROM calculated_breakdowns WHERE result_id = w.result_id;
  UPDATE calculated_windows SET list_id = id WHERE result_id = w.result_id;
 END LOOP; END $$;
DROP TABLE calculated_breakdowns CASCADE`)
}
