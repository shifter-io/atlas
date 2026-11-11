package store_test

import (
	"context"
	"slices"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
)

func idsOf(rows []store.Event) []int64 {
	out := []int64{}
	for _, e := range rows {
		out = append(out, e.ID)
	}
	return out
}

// The live tape's filters as database predicates (TAPE-ROWS-1): network,
// country, target and proxy kind narrow the rows as Match does, after reads
// the oldest rows past it by id (one more than the limit, to tell more), and
// q reads store.TextScan rows unpicked for the caller to match.
func TestQueryEventsTapeFilters(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	ctx := context.Background()
	db.Exec(t, "INSERT INTO providers VALUES ('a'),('b')")
	db.Exec(t, "INSERT INTO products(provider,name) VALUES ('a','residential'),('b','datacenter')")
	// Ids 1 to 6 at one time: a in US on generic_http (odd ids), b in DE on generic_https (even).
	for i := 1; i <= 6; i++ {
		provider, country, target := "a", "US", "generic_http"
		if i%2 == 0 {
			provider, country, target = "b", "DE", "generic_https"
		}
		db.Exec(t, `INSERT INTO test_events(at,kind,product_id,country_code,target_id,signal,severity)
 SELECT '2026-11-02T12:00:00Z','pulse_finished',product_id,$2,$3,0,'info' FROM products WHERE provider=$1`, provider, country, target)
	}
	c := db.Conn(t)
	defer c.Close(ctx)
	after := int64(2)
	cases := []struct {
		name string
		f    store.EventFilter
		want []int64
	}{
		{"network", store.EventFilter{Provider: "a", Limit: 100}, []int64{5, 3, 1}},
		{"country", store.EventFilter{Country: "DE", Limit: 100}, []int64{6, 4, 2}},
		{"target", store.EventFilter{Target: "generic_http", Limit: 100}, []int64{5, 3, 1}},
		{"proxyKind", store.EventFilter{ProxyKind: "datacenter", Limit: 100}, []int64{6, 4, 2}},
		{"network and product", store.EventFilter{Provider: "a", Product: "b", Limit: 100}, []int64{}},
		// A merged answer reads store.GroupRows times the limit, plus one.
		{"after", store.EventFilter{After: &after, Limit: 2}, []int64{3, 4, 5, 6}},
		{"after a target", store.EventFilter{After: &after, Target: "generic_http", Limit: 1}, []int64{3, 5}},
		{"after a network", store.EventFilter{After: &after, Provider: "b", Limit: 1}, []int64{4, 6}},
		{"q", store.EventFilter{Words: []string{"x"}, Limit: 1}, []int64{6, 5, 4, 3, 2, 1}},
		{"q after", store.EventFilter{Words: []string{"x"}, After: &after, Country: "US", Limit: 1}, []int64{3, 5}},
	}
	for _, tc := range cases {
		rows, err := store.QueryEvents(ctx, c, tc.f)
		if err != nil {
			t.Fatalf("%s: %v", tc.name, err)
		}
		if !slices.Equal(idsOf(rows), tc.want) {
			t.Errorf("%s: %v, want %v", tc.name, idsOf(rows), tc.want)
		}
		for i := range rows {
			if !tc.f.Match(&rows[i]) {
				t.Errorf("%s: row %d does not pass Match", tc.name, rows[i].ID)
			}
		}
	}
}

// Since or after makes a filter unprioritized: a client catching up gets
// every row in order.
func TestCursorsAreNotPrioritized(t *testing.T) {
	t.Parallel()
	after := int64(1)
	if !(store.EventFilter{Provider: "a", Words: []string{"x"}}).Prioritized() || (store.EventFilter{After: &after}).Prioritized() {
		t.Fatal("only a cursor turns prioritizing off")
	}
}

// A row written after the window's rows but stamped older (the daily ingest
// writes a test's own times) is read as a late row, by id; rows older by id
// are not.
func TestLoadReadsLateEvents(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	ctx := context.Background()
	db.Exec(t, "INSERT INTO test_events(at,kind,signal,severity) VALUES ('2026-11-02T10:00:00Z','daily_test_finished',0,'info')")
	db.Exec(t, "INSERT INTO test_events(at,kind,signal,severity) SELECT '2026-11-02T16:00:00Z','pulse_finished',0,'info'"+
		" FROM generate_series(1,$1::int)", store.EventWindow+1)
	db.Exec(t, "INSERT INTO test_events(at,kind,signal,severity) VALUES ('2026-11-02T09:00:00Z','daily_test_finished',0,'info')")
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	in, err := store.LoadEvents(ctx, pool)
	if err != nil {
		t.Fatal(err)
	}
	// The window is ids 2002 down to 3; ids 2 and 1 are cut and older by id, and
	// the late row is the 09:00 one, id 2003.
	want := int64(store.EventWindow + 3)
	if in.EventsComplete || !in.LateComplete || !slices.Equal(idsOf(in.Late), []int64{want}) {
		t.Fatalf("late %v complete %v (window %d to %d)", idsOf(in.Late), in.LateComplete, in.Events[0].ID, store.MinEventID(in.Events))
	}
}
