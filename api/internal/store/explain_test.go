package store_test

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
)

// readOnlyRole adds a login role with SELECT on the public and site tables
// only, as the API's role has, and returns its DSN.
func readOnlyRole(t *testing.T, db *testdb.DB) string {
	t.Helper()
	role := "siteapi_ro_" + strings.TrimPrefix(db.Name, "siteapi_test_")
	db.Exec(t, fmt.Sprintf(`CREATE ROLE %[1]s LOGIN PASSWORD 'read-only'; GRANT USAGE ON SCHEMA public, site TO %[1]s;
 GRANT SELECT ON ALL TABLES IN SCHEMA public, site TO %[1]s`, role))
	// Before the database is dropped (cleanups run last first).
	t.Cleanup(func() { db.Exec(t, "DROP OWNED BY "+role+"; DROP ROLE "+role) })
	cfg := db.Admin.WithDB(db.Name)
	cfg.User, cfg.Password = role, "read-only"
	return cfg.DSN()
}

// explainDB is networkLists' database with a v4 build published, list 2
// (the 7d window) one city row longer than the others, and events.
func explainDB(t *testing.T) *testdb.DB {
	t.Helper()
	db, _ := networkLists(t)
	db.Publish(t, v4Head(t, 0))
	db.Exec(t, `INSERT INTO calculated_list_rows(list_id,dimension,position,place_id,ips,observations)
  SELECT 2,'g',2,place_id,1,1 FROM place_names WHERE city = 'San Jose';
 INSERT INTO test_events(at,kind,signal,severity) VALUES ('2026-11-02T15:00:00Z','pulse_down',-1,'major'),
  ('2026-11-02T16:00:00Z','latency_rise',-1,'minor'), ('2026-11-02T16:30:00Z','pulse_finished',0,'info')`)
	c := db.Conn(t)
	testdb.CountListRows(t, c)
	c.Close(context.Background())
	return db
}

// TestExplainTimesEveryStatement: through a read-only role, every
// registered statement appears once, in registry order; every one but the
// pre-v4 shapes runs and has a plan; the parameters come from the build.
func TestExplainTimesEveryStatement(t *testing.T) {
	t.Parallel()
	db := explainDB(t)
	ctx := context.Background()
	pool, err := store.OpenReadOnly(ctx, readOnlyRole(t, db))
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	report, err := store.Explain(ctx, pool)
	if err != nil {
		t.Fatal(err)
	}
	want := store.Statements()
	if len(report.Statements) != len(want) {
		t.Fatalf("%d statements explained, %d registered", len(report.Statements), len(want))
	}
	for i, e := range report.Statements {
		if e.Name != want[i].Name || e.SQL == "" {
			t.Errorf("statement %d is %q (SQL %q), want %q", i, e.Name, e.SQL, want[i].Name)
		}
		switch pre := e.Only == "pre-v4"; {
		case e.Error != "":
			t.Errorf("%s failed: %s", e.Name, e.Error)
		case pre != (e.Skipped != ""):
			t.Errorf("%s (layout %q) skipped: %q", e.Name, e.Only, e.Skipped)
		case !pre && (len(e.Nodes) == 0 || e.ExecutionMs <= 0 || e.SharedHit+e.SharedRead < 0):
			t.Errorf("%s has no plan: %+v", e.Name, e)
		}
	}
	p := report.Params
	if !p.V4 || p.List != 2 || p.ListRows != 11 || p.Dimension != "g" || p.Provider != "shifter" || len(p.Lists) != 3 {
		t.Errorf("parameters %+v: want v4 list 2 of 11 rows, dimension g, of shifter, among 3 lists", p)
	}
	if p.LargestPlace == nil || p.LargestPlace.City == nil || *p.LargestPlace.City != "Berlin" || len(p.Places) != 2 {
		t.Errorf("largest place %+v of places %v: want Berlin of 2", p.LargestPlace, p.Places)
	}
	if p.Country == nil || *p.Country != "US" || p.City == nil || *p.City != "San Jose" {
		t.Errorf("country %v, city %v: want US and San Jose", p.Country, p.City)
	}
	md := string(report.Markdown())
	for _, e := range report.Statements {
		if !strings.Contains(md, "`"+e.Name+"`") {
			t.Errorf("explain.md lacks %s", e.Name)
		}
	}
}

// A role that can write is refused (the test admin is a superuser).
func TestExplainRefusesAWritableRole(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	pool, err := store.OpenReadOnly(context.Background(), db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	if _, err := store.Explain(context.Background(), pool); !errors.Is(err, store.ErrWritable) {
		t.Errorf("explain as the admin: %v, want ErrWritable", err)
	}
}

// Without a published build the build-free statements still run and the v4
// ones are skipped.
func TestExplainWithoutABuild(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	pool, err := store.OpenReadOnly(context.Background(), readOnlyRole(t, db))
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	report, err := store.Explain(context.Background(), pool)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range report.Statements {
		if want := e.Only != ""; e.Error != "" || want != (e.Skipped != "") {
			t.Errorf("%s (layout %q): skipped %q, error %q", e.Name, e.Only, e.Skipped, e.Error)
		}
	}
}

// explain.md lists the timed statements slowest first, flags those over
// 100 ms and over 10% of the timeout, then the skipped and failed ones.
func TestExplainMarkdownSortsAndFlags(t *testing.T) {
	t.Parallel()
	r := &store.ExplainReport{Statements: []store.Explained{
		{Name: "fast", ExecutionMs: 1},
		{Name: "slow", PlanningMs: 1, ExecutionMs: 150},
		{Name: "slowest", ExecutionMs: 3500},
		{Name: "old", Skipped: "pre-v4 layout: not timed"},
		{Name: "broken", Error: "boom"},
	}}
	md := string(r.Markdown())
	at := func(s string) int { return strings.Index(md, s) }
	if !(at("`slowest`") < at("`slow`") && at("`slow`") < at("`fast`") && at("`fast`") < at("## Skipped") && at("## Skipped") < at("## Failed")) {
		t.Errorf("order:\n%s", md)
	}
	lines := strings.Split(md, "\n")
	flag := func(name string) string {
		for _, l := range lines {
			if strings.HasPrefix(l, "| `"+name+"`") {
				return l
			}
		}
		return ""
	}
	if !strings.Contains(flag("slowest"), "10% of the 30 s timeout") || !strings.Contains(flag("slow"), "over 100 ms") || strings.Contains(flag("fast"), "**") {
		t.Errorf("flags:\n%s", md)
	}
	if !strings.Contains(md, "- `old` (``): pre-v4 layout: not timed") || !strings.Contains(md, "- `broken` (``): boom") {
		t.Errorf("skipped and failed:\n%s", md)
	}
}
