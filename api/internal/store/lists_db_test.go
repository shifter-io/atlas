package store_test

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/jackc/pgx/v5"
)

// planNode is the part of EXPLAIN (FORMAT JSON) the tests read.
type planNode struct {
	NodeType   string     `json:"Node Type"`
	Relation   string     `json:"Relation Name"`
	Index      string     `json:"Index Name"`
	PlanRows   float64    `json:"Plan Rows"`
	ActualRows float64    `json:"Actual Rows"`
	Loops      float64    `json:"Actual Loops"`
	Plans      []planNode `json:"Plans"`
}

// scanOf runs EXPLAIN ANALYZE of sql and returns the plan node that reads
// relation (its estimate and its actual rows) and every index the plan uses.
func scanOf(t *testing.T, db *testdb.DB, relation, sql string, args ...any) (planNode, []string) {
	t.Helper()
	var out []struct{ Plan planNode }
	if err := json.Unmarshal([]byte(text(t, db, "EXPLAIN (ANALYZE, FORMAT JSON) "+sql, args...)), &out); err != nil {
		t.Fatal(err)
	}
	var found *planNode
	var indexes []string
	var walk func(n *planNode)
	walk = func(n *planNode) {
		if n.Index != "" {
			indexes = append(indexes, n.Index)
		}
		if n.Relation == relation && found == nil {
			found = n
		}
		for i := range n.Plans {
			walk(&n.Plans[i])
		}
	}
	walk(&out[0].Plan)
	if found == nil {
		t.Fatalf("no scan of %s in the plan of %s", relation, sql)
	}
	return *found, indexes
}

func within2x(n planNode) bool {
	actual := n.ActualRows * max(n.Loops, 1)
	return n.PlanRows >= actual/2 && n.PlanRows <= actual*2
}

// oldLongTailSQL is the long-tail query before API-LISTS-1 (store/longtail.go
// on main): it filters on the view's CASE output, so the planner cannot use
// the stored code's statistics.
const oldLongTailSQL = `SELECT w.product_id, w.cohort, w.window_hours, count(*)
 FROM public.calculated_windows w
 JOIN public.calculated_breakdown_details d ON d.result_id = w.result_id
 WHERE w.build_id = $1 AND w.target = '' AND d.dimension IN ('city_asn', 'asn')
 GROUP BY 1, 2, 3`

// emptyBuild publishes a build with one product and returns its id.
func emptyBuild(t *testing.T, db *testdb.DB) int64 {
	t.Helper()
	id := db.Publish(t, []byte(`{"as_of": "2026-11-04T00:00:00+00:00", "methodology": {"id": "m"}, "products": []}`))
	db.Exec(t, `INSERT INTO providers VALUES ('p') ON CONFLICT DO NOTHING; INSERT INTO products(provider,name) VALUES ('p','residential') ON CONFLICT DO NOTHING;
 INSERT INTO cohorts(cohort) VALUES ('c') ON CONFLICT DO NOTHING;
 INSERT INTO place_names(country_code,country,city) VALUES ('DE','Germany','Berlin');
 INSERT INTO asn_names(asn,name) VALUES (64496,'Example'); INSERT INTO asn_labels(asn_name_id,usage_type) SELECT asn_name_id,'isp' FROM asn_names`)
	return id
}

// The DB-AUDIT-2 shape: build 3's partition held 20,229 rows, 10,938 of them
// city x ASN and ASN rows (here three windows of 3,000 + 646). The old query
// estimated 202 of them; the stored-code query is within 2x.
func TestBreakdownQueryEstimatesTheStoredCode(t *testing.T) {
	db := testdb.New(t)
	build := emptyBuild(t, db)
	db.CalculatedV8(t)
	db.Exec(t, fmt.Sprintf(`CREATE TABLE calculated_breakdowns_b%[1]d PARTITION OF calculated_breakdowns FOR VALUES IN (%[1]d);
 INSERT INTO calculated_windows(build_id,product_id,cohort,target,window_hours,completed,successes,errors,body_bytes,coverage,readiness)
  SELECT %[1]d,(SELECT product_id FROM products),'c','',h,0,0,0,0,0,'ready' FROM unnest('{24,168,720}'::int[]) h;
 INSERT INTO calculated_breakdowns(build_id,result_id,dimension,position,ips,observations,place_id,asn_label_id)
  SELECT %[1]d, w.result_id, d.code, p, 1, 1, CASE WHEN d.code <> 'a' THEN (SELECT place_id FROM place_names) END,
   CASE WHEN d.code IN ('a','x','y') THEN (SELECT asn_label_id FROM asn_labels) END
  FROM calculated_windows w, (VALUES ('c'::"char",250),('r',900),('g',1500),('a',646),('x',447),('y',3000)) d(code,n), generate_series(0,d.n-1) p
  WHERE w.build_id = %[1]d;
 ANALYZE calculated_breakdowns, calculated_windows`, build))
	if n := text(t, db, `SELECT count(*)::text FROM calculated_breakdowns`); n != "20229" {
		t.Fatalf("%s rows", n)
	}
	partition := fmt.Sprintf("calculated_breakdowns_b%d", build)
	before, _ := scanOf(t, db, partition, oldLongTailSQL, build)
	t.Logf("before: estimated %.0f rows, actual %.0f", before.PlanRows, before.ActualRows)
	if within2x(before) {
		t.Errorf("the old query's estimate is within 2x (%+v): the regression fixture does not reproduce DB-AUDIT-2", before)
	}
	var ids []int64
	for _, s := range strings.Fields(text(t, db, `SELECT string_agg(result_id::text, ' ') FROM calculated_windows WHERE build_id = $1`, build)) {
		var id int64
		fmt.Sscan(s, &id)
		ids = append(ids, id)
	}
	after, _ := scanOf(t, db, partition, store.ListQuery(false), ids, []string{"y", "a"}, build)
	t.Logf("after: estimated %.0f rows, actual %.0f", after.PlanRows, after.ActualRows)
	// Half the partition: a sequential scan is the right plan here; the K2
	// query below reads a few lists by the key.
	if !within2x(after) || after.ActualRows != 10938 {
		t.Errorf("stored-code query: %+v", after)
	}
}

// On 100k list rows the list query reads the asked ids by the primary key,
// estimated within 2x.
func TestListQueryUsesThePrimaryKey(t *testing.T) {
	db := testdb.New(t)
	emptyBuild(t, db)
	db.Exec(t, `INSERT INTO calculated_lists(`+testdb.ListColumns+`) SELECT 1,'c'||i,1,`+testdb.ListFill+` FROM generate_series(1,100) i;
 INSERT INTO calculated_list_rows(list_id,dimension,position,place_id,asn_label_id,ips,observations)
  SELECT l.list_id, d.code, p, CASE WHEN d.code <> 'a' THEN (SELECT place_id FROM place_names) END,
   CASE WHEN d.code IN ('a','x','y') THEN (SELECT asn_label_id FROM asn_labels) END, 1, 1
  FROM calculated_lists l, (VALUES ('c'::"char",40),('r',150),('g',200),('a',150),('x',120),('y',340)) d(code,n), generate_series(0,d.n-1) p;
 ANALYZE calculated_list_rows`)
	if n := text(t, db, `SELECT count(*)::text FROM calculated_list_rows`); n != "100000" {
		t.Fatalf("%s rows", n)
	}
	ids := []int64{3, 17, 18, 40, 41, 55, 60, 77, 90, 99}
	scan, indexes := scanOf(t, db, "calculated_list_rows", store.ListQuery(true), ids, strings.Split("crgax", ""))
	t.Logf("estimated %.0f rows, actual %.0f, %s on %v", scan.PlanRows, scan.ActualRows, scan.NodeType, indexes)
	if !within2x(scan) || scan.ActualRows != 6600 {
		t.Errorf("list query: %+v", scan)
	}
	if len(indexes) != 1 || indexes[0] != "calculated_list_rows_pkey" {
		t.Errorf("list query uses indexes %v, want the primary key", indexes)
	}
}

// slimmedGroup is a v2 payload of one group whose 24h window kept one of its
// three city x ASN rows.
const slimmedGroup = `{"as_of": "2026-11-04T00:00:00+00:00", "methodology": {"id": "m"}, "schema_version": 2, "products": [{"provider": "p",
 "product": "residential", "kind": "residential", "product_id": %d, "cohort": "c", "windows": {"24h": {"period": {"hours": 24},
 "pool": {"list_entries": {}}, "breakdowns": {"city_asn": [{"city": "Berlin"}]}, "breakdown_entries": {"city_asn": 3}}}}]}`

// concurrentDrop publishes the group with its typed rows (in a K2 list or in
// the build's calculated v8 partition), pauses a load once it has read the snapshot row,
// runs gc in another session meanwhile, and returns the loaded payload's
// city x ASN rows and gc's error.
func concurrentDrop(t *testing.T, k2, lock bool, gc func(db *testdb.DB, build int64) error) (int, error) {
	t.Helper()
	db := testdb.New(t)
	emptyBuild(t, db)
	product := text(t, db, `SELECT product_id::text FROM products`)
	var p int
	fmt.Sscan(product, &p)
	build := db.Publish(t, []byte(fmt.Sprintf(slimmedGroup, p)))
	db.CalculatedV8(t)
	db.Exec(t, fmt.Sprintf(`CREATE TABLE calculated_breakdowns_b%[1]d PARTITION OF calculated_breakdowns FOR VALUES IN (%[1]d);
 INSERT INTO calculated_windows(build_id,product_id,cohort,target,window_hours,completed,successes,errors,body_bytes,coverage,readiness)
  VALUES (%[1]d,%[2]d,'c','',24,0,0,0,0,0,'ready');
 INSERT INTO calculated_breakdowns(build_id,result_id,dimension,position,ips,observations,place_id,asn_label_id)
  SELECT %[1]d, (SELECT result_id FROM calculated_windows WHERE build_id = %[1]d), 'y', p, 3 - p, 1,
   (SELECT place_id FROM place_names), (SELECT asn_label_id FROM asn_labels) FROM generate_series(0,2) p`, build, p))
	if k2 {
		db.MoveToLists(t)
	}
	ready, release, done := make(chan struct{}), make(chan struct{}), make(chan []byte)
	defer store.Pause(func() { close(ready); <-release }, lock)()
	go func() {
		in, err := tryLoad(t, db)
		if err != nil {
			t.Error(err)
			done <- nil
			return
		}
		done <- in.Snapshot.Full()
	}()
	<-ready
	gcErr := gc(db, build)
	close(release)
	full := <-done
	var doc struct {
		Products []struct {
			Windows map[string]struct {
				Breakdowns map[string][]any `json:"breakdowns"`
			} `json:"windows"`
		} `json:"products"`
	}
	if err := json.Unmarshal(full, &doc); err != nil {
		t.Fatal(err)
	}
	return len(doc.Products[0].Windows["24h"].Breakdowns["city_asn"]), gcErr
}

// exec runs sql in a session of its own, after a 2-second lock timeout.
func exec(db *testdb.DB, sql string) error {
	c, err := pgx.Connect(context.Background(), db.DSN)
	if err != nil {
		return err
	}
	defer c.Close(context.Background())
	_, err = c.Exec(context.Background(), "SET lock_timeout = '2s'; "+sql)
	return err
}

// WEB-LONGTAIL-RACE-1: a publication that drops the old build's partition
// while the API reads that build. Without the lock the read finds no typed
// rows and keeps the capped list; with it the drop waits.
func TestConcurrentPartitionDropWaitsForTheRead(t *testing.T) {
	drop := func(db *testdb.DB, build int64) error {
		return exec(db, fmt.Sprintf("DROP TABLE calculated_breakdowns_b%d", build))
	}
	rows, err := concurrentDrop(t, false, false, drop)
	if err != nil || rows != 1 {
		t.Errorf("without the lock: the drop returned %v and the read kept %d rows; want the race (nil, 1)", err, rows)
	}
	rows, err = concurrentDrop(t, false, true, drop)
	if err == nil || !strings.Contains(err.Error(), "lock timeout") || rows != 3 {
		t.Errorf("with the lock: the drop returned %v and the read has %d rows; want a lock timeout and 3", err, rows)
	}
}

// K2 lists are deleted, not dropped: a REPEATABLE READ load keeps seeing the
// rows of the build it read while a GC deletes them.
func TestConcurrentListDeleteKeepsTheRead(t *testing.T) {
	rows, err := concurrentDrop(t, true, true, func(db *testdb.DB, build int64) error {
		return exec(db, `UPDATE calculated_windows SET list_id = NULL; DELETE FROM calculated_list_rows; DELETE FROM calculated_lists`)
	})
	if err != nil || rows != 3 {
		t.Errorf("the GC returned %v and the read has %d city x ASN rows, want nil and 3", err, rows)
	}
}

// B1: a list read that fails after scanning a row leaves that row's place
// pending; the next load must not fill the names of a place it no longer
// holds (it dereferenced nil and crashed siteapi).
func TestALoadAfterAFailedReadDoesNotCrash(t *testing.T) {
	db := testdb.New(t)
	emptyBuild(t, db)
	db.Exec(t, `INSERT INTO place_names(country_code,country,city) VALUES ('FR','France','Paris')`)
	var stale, p int32
	fmt.Sscan(text(t, db, `SELECT place_id::text FROM place_names WHERE city = 'Paris'`), &stale)
	fmt.Sscan(text(t, db, `SELECT product_id::text FROM products`), &p)
	build := db.Publish(t, []byte(fmt.Sprintf(slimmedGroup, p)))
	db.CalculatedV8(t)
	db.Exec(t, fmt.Sprintf(`CREATE TABLE calculated_breakdowns_b%[1]d PARTITION OF calculated_breakdowns FOR VALUES IN (%[1]d);
 INSERT INTO calculated_windows(build_id,product_id,cohort,target,window_hours,completed,successes,errors,body_bytes,coverage,readiness)
  VALUES (%[1]d,%[2]d,'c','',24,0,0,0,0,0,'ready');
 INSERT INTO calculated_breakdowns(build_id,result_id,dimension,position,ips,observations,place_id,asn_label_id)
  SELECT %[1]d, (SELECT result_id FROM calculated_windows WHERE build_id = %[1]d), 'y', p, 3 - p, 1,
   (SELECT place_id FROM place_names WHERE city = 'Berlin'), (SELECT asn_label_id FROM asn_labels) FROM generate_series(0,2) p`, build, p))
	lists := store.NewLists()
	store.FailedRead(lists, stale)
	ctx := context.Background()
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	var in *store.Inputs
	crash := func() (v any) {
		defer func() { v = recover() }()
		in, err = store.LoadLists(ctx, pool, lists)
		return nil
	}()
	if crash != nil || err != nil {
		t.Fatalf("load after a failed read: panic %v, error %v", crash, err)
	}
	if rows := in.Snapshot.Places[store.PlaceWindow{Product: int64(p), Cohort: "c", Hours: 24}].Rows("city_asn"); len(rows) != 3 || *rows[0].Place.City != "Berlin" {
		t.Errorf("city x ASN rows %+v", rows)
	}
}
