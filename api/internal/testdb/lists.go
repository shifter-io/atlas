package testdb

import (
	"context"
	"testing"

	"github.com/jackc/pgx/v5"
)

// ListColumns are the calculated_lists columns of atlas calculated v9
// (contract K2, CALC-LISTS-1a) that have no default; an insert names them all
// and fills the ones the API never reads with ListFill:
//
//	INSERT INTO calculated_lists(ListColumns) SELECT <product>,<cohort>,<hours>,ListFill ...
const (
	ListColumns = "product_id,cohort,window_hours,content_hash,entries,figures,created_build"
	ListFill    = "'test','{}','{}',0"
)

// countRowsSQL sets each list's entries to its rows' count per breakdown name,
// as the framework writes them with the rows (the API reads a network list's
// counts from them): the lists written with ListFill's '{}', or every list
// when $1.
const countRowsSQL = `UPDATE calculated_lists l SET entries = (SELECT jsonb_build_object(
 'country', count(*) FILTER (WHERE r.dimension = 'c'), 'region', count(*) FILTER (WHERE r.dimension = 'r'),
 'city', count(*) FILTER (WHERE r.dimension = 'g'), 'asn', count(*) FILTER (WHERE r.dimension = 'a'),
 'country_asn', count(*) FILTER (WHERE r.dimension = 'x'), 'city_asn', count(*) FILTER (WHERE r.dimension = 'y'))
 FROM calculated_list_rows r WHERE r.list_id = l.list_id) WHERE $1 OR l.entries = '{}'`

// CountListRows sets every list's entries to its rows' count per breakdown
// name, for a test that changes a list's rows after writing them.
func CountListRows(t testing.TB, c *pgx.Conn) {
	t.Helper()
	if err := countRows(context.Background(), c, true); err != nil {
		t.Fatal(err)
	}
}

// countRows runs countRowsSQL, when the database has lists (not a calculated
// v8 one, CalculatedV8).
func countRows(ctx context.Context, c *pgx.Conn, all bool) error {
	var lists bool
	err := c.QueryRow(ctx, "SELECT to_regclass('calculated_lists') IS NOT NULL").Scan(&lists)
	if err == nil && lists {
		_, err = c.Exec(ctx, countRowsSQL, all)
	}
	return err
}

// Product registers provider and product name as the framework does (the
// same rows Publish writes) and returns the product id. A list references its
// product, so a list written before its snapshot is published needs this.
func Product(t testing.TB, c *pgx.Conn, provider, name string) int64 {
	t.Helper()
	ctx := context.Background()
	if _, err := c.Exec(ctx, "INSERT INTO providers VALUES ($1) ON CONFLICT DO NOTHING", provider); err != nil {
		t.Fatal(err)
	}
	var id int64
	if err := c.QueryRow(ctx, `INSERT INTO products(provider,name) VALUES ($1,$2)
 ON CONFLICT (provider,name) DO UPDATE SET name=excluded.name RETURNING product_id`, provider, name).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return id
}

// calculatedV8 is what atlas calculated v8 (before CALC-LISTS-1a) has
// where v9 has the list tables: typed breakdown rows in calculated_breakdowns,
// LIST-partitioned by build (one calculated_breakdowns_b<build_id> partition
// per build, no default partition), the two views over them and current_windows
// (the same text, whose w.* has no list_id in v8). Copied from that version's
// calculated/schema.sql.
const calculatedV8 = `CREATE TABLE calculated_breakdowns (
 build_id bigint NOT NULL, result_id bigint NOT NULL,
 ips bigint NOT NULL, observations bigint NOT NULL,
 place_id integer REFERENCES place_names(place_id),
 asn_label_id integer REFERENCES asn_labels(asn_label_id),
 position integer NOT NULL, dimension "char" NOT NULL CHECK(dimension IN ('g','c','a','r','x','y')),
 completed bigint, successes bigint, challenges bigint, blocks bigint,
 p50 integer, p90 integer, p99 integer,
 CHECK((place_id IS NULL) = (dimension = 'a')
  AND (asn_label_id IS NULL) = (dimension IN ('g','c','r'))),
 PRIMARY KEY(result_id,dimension,position,build_id),
 FOREIGN KEY(result_id,build_id) REFERENCES calculated_windows(result_id,build_id)
) PARTITION BY LIST(build_id);
CREATE VIEW calculated_breakdown_details AS
SELECT b.result_id,CASE b.dimension WHEN 'g' THEN 'geography' WHEN 'c' THEN 'country'
 WHEN 'r' THEN 'region' WHEN 'x' THEN 'country_asn' WHEN 'y' THEN 'city_asn' ELSE 'asn' END
 AS dimension,b.position,b.ips,b.observations,CASE b.dimension
 WHEN 'g' THEN jsonb_build_object('country_code',p.country_code,'country',p.country,
  'region',p.region,'region_code',p.region_code,'city',p.city)
 WHEN 'c' THEN jsonb_build_object('country_code',p.country_code,'country',p.country)
 WHEN 'r' THEN jsonb_build_object('country_code',p.country_code,'country',p.country,
  'region',p.region,'region_code',p.region_code)
 WHEN 'x' THEN jsonb_build_object('country_code',p.country_code,'country',p.country,
  'asn',a.asn,'name',a.name,'usage_type',a.usage_type)
 WHEN 'y' THEN jsonb_build_object('country_code',p.country_code,'country',p.country,
  'region',p.region,'region_code',p.region_code,'city',p.city,
  'asn',a.asn,'name',a.name,'usage_type',a.usage_type)
 ELSE jsonb_build_object('asn',a.asn,'name',a.name,'usage_type',a.usage_type) END AS label,
 b.completed,b.successes,b.successes::double precision/nullif(b.completed,0) AS success_rate,
 b.challenges,b.blocks,b.p50,b.p90,b.p99
FROM calculated_breakdowns b LEFT JOIN place_names p USING(place_id)
LEFT JOIN asn_label_names a USING(asn_label_id);
CREATE VIEW current_breakdowns AS
SELECT b.build_id,b.result_id,pr.provider,pr.name AS product,w.cohort,w.window_hours,
 CASE b.dimension WHEN 'g' THEN 'geography' WHEN 'c' THEN 'country' WHEN 'r' THEN 'region'
 WHEN 'x' THEN 'country_asn' WHEN 'y' THEN 'city_asn' ELSE 'asn' END AS dimension,b.position,
 p.country_code,p.country,p.region_code,p.region,p.city,a.asn,a.name AS asn_name,a.usage_type,
 b.ips,b.observations,b.completed,b.successes,
 b.successes::double precision/nullif(b.completed,0) AS success_rate,
 b.challenges,b.blocks,b.p50,b.p90,b.p99
FROM calculated_breakdowns b JOIN calculated_windows w USING(result_id,build_id)
JOIN products pr USING(product_id)
LEFT JOIN place_names p USING(place_id) LEFT JOIN asn_label_names a USING(asn_label_id)
WHERE b.build_id=(SELECT build_id FROM current_calculation);
CREATE VIEW current_windows AS
SELECT p.provider,p.name AS product,w.*
FROM calculated_windows w JOIN products p USING(product_id)
WHERE w.build_id=(SELECT build_id FROM current_calculation)`

// CalculatedV8 turns the scratch database's calculated v9 schema back into
// calculated v8's, for the tests of the API's read before K2: the list tables,
// calculated_windows.list_id and the views over them go, calculated_breakdowns
// and its v8 views come back. Nothing else differs between v8 and v9.
func (d *DB) CalculatedV8(t testing.TB) {
	t.Helper()
	d.Exec(t, `DROP VIEW current_breakdowns, calculated_breakdown_details, calculated_list_details, current_windows;
ALTER TABLE calculated_windows DROP COLUMN list_id;
DROP TABLE calculated_list_rows, calculated_lists;
`+calculatedV8)
}

// MoveToLists does what calculated v9 does to a v8 database (CalculatedV8):
// it creates the list tables and views from the framework's calculated
// schema, moves each target-free window's calculated_breakdowns rows into a
// list of its own (signature NULL) and drops calculated_breakdowns.
func (d *DB) MoveToLists(t testing.TB) {
	t.Helper()
	d.Exec(t, `ALTER TABLE calculated_windows ADD COLUMN list_id bigint`)
	d.loadCalculated(t)
	d.Exec(t, `ALTER TABLE calculated_windows ADD FOREIGN KEY(list_id) REFERENCES calculated_lists(list_id);
DO $$ DECLARE w record; id bigint; BEGIN
 FOR w IN SELECT * FROM calculated_windows WHERE target = '' AND result_id IN (SELECT result_id FROM calculated_breakdowns) LOOP
  INSERT INTO calculated_lists(`+ListColumns+`) VALUES (w.product_id,w.cohort,w.window_hours,'test','{}','{}',w.build_id)
   RETURNING list_id INTO id;
  INSERT INTO calculated_list_rows SELECT id,dimension,position,place_id,asn_label_id,ips,observations,completed,successes,
   challenges,blocks,p50,p90,p99 FROM calculated_breakdowns WHERE result_id = w.result_id;
  UPDATE calculated_windows SET list_id = id WHERE result_id = w.result_id;
 END LOOP; END $$;
DROP TABLE calculated_breakdowns`)
}
