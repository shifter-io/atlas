package store_test

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testdb"
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

// On 100k list rows the list query reads the asked ids by the primary key,
// estimated within 2x.
func TestListQueryUsesThePrimaryKey(t *testing.T) {
	t.Parallel()
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
