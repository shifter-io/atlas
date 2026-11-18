package store_test

import (
	"context"
	"slices"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testdb"
)

// tapeHeads is LoadEvents (for the event counts) and then the tab heads read
// in a read-only transaction on c.
func tapeHeads(t *testing.T, c *pgx.Conn) *store.Inputs {
	t.Helper()
	ctx := context.Background()
	in, err := store.LoadEvents(ctx, c)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := c.BeginTx(ctx, pgx.TxOptions{AccessMode: pgx.ReadOnly})
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx) //nolint:errcheck
	if err := in.LoadTapeHeads(ctx, tx); err != nil {
		t.Fatal(err)
	}
	return in
}

// The tab heads (HOME-TAPE-1) are each tab's newest store.TapeHeadRows
// events, newest first, even when the newest events are all one tab: 30
// census rows on frozen days, then 12 pulse rows spread over hours, 3
// incidents and no audit. Without the event counts the reads are unbounded
// and give the same rows.
func TestLoadTapeHeadsReadsEachTabsNewestEvents(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	ctx := context.Background()
	db.Exec(t, `INSERT INTO test_events(at,kind,signal,severity) SELECT '2026-11-10T20:00:00Z'::timestamptz - i * interval '1 minute',
 'daily_test_finished',0,'info' FROM generate_series(0, 29) i`)
	db.Exec(t, `INSERT INTO test_events(at,kind,signal,severity) SELECT '2026-11-10T19:00:00Z'::timestamptz - i * interval '20 minutes',
 'pulse_finished',0,'info' FROM generate_series(0, 11) i`)
	db.Exec(t, `INSERT INTO test_events(at,kind,signal,severity) SELECT '2026-11-09T10:00:00Z'::timestamptz - i * interval '1 day',
 'latency_rise',-1,'minor' FROM generate_series(0, 2) i`)
	c := db.Conn(t)
	defer c.Close(ctx)
	check := func(name string, in *store.Inputs) {
		t.Helper()
		tabs := map[string]int{}
		for i, e := range in.TapeHeads {
			tabs[store.Category(e.Kind)]++
			if i > 0 && store.Newer(&e, &in.TapeHeads[i-1]) {
				t.Errorf("%s: row %d is newer than the one before", name, e.ID)
			}
		}
		want := map[string]int{"census": store.TapeHeadRows, "pulse": store.TapeHeadRows, "incident": 3}
		if len(tabs) != 3 || tabs["census"] != want["census"] || tabs["pulse"] != want["pulse"] || tabs["incident"] != 3 {
			t.Errorf("%s: tabs %v", name, tabs)
		}
		// Census ids 1 to 10 of 30, pulse 31 to 40 of 42 (the 10th on its
		// bucket's first minute), incidents 43 to 45.
		if ids := idsOf(in.TapeHeads); !slices.Contains(ids, 1) || slices.Contains(ids, 11) || !slices.Contains(ids, 40) || slices.Contains(ids, 41) {
			t.Errorf("%s: ids %v", name, ids)
		}
	}
	check("counted", tapeHeads(t, c))
	db.Exec(t, "DROP TABLE test_event_buckets")
	check("without the counts", tapeHeads(t, c))
}

// The heads hold each product's newest store.TapeHeadRows events of every tab
// (NETWORK-TAPE-QUOTA-1), GroupRows times as many of a tab whose rows merge
// (all but census; the build merges and takes 10), even when the newest
// events of the product are all one kind and another product's rows are
// older than every tab's heads:
// product a's newest 13 are ip_overlap, then 15 census, 12 pulse and 3
// incident rows; product b has 4 old census rows and no other tab. Without
// the event counts the read is unbounded and gives the same rows.
func TestLoadTapeHeadsReadsEachNetworksTabs(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	ctx := context.Background()
	db.Exec(t, "INSERT INTO providers VALUES ('a'),('b')")
	db.Exec(t, "INSERT INTO products(provider,name) VALUES ('a','residential'),('b','residential')")
	insert := func(provider, kind, from, step string, n int) {
		db.Exec(t, `INSERT INTO test_events(at,kind,product_id,signal,severity) SELECT $1::timestamptz - i * $2::interval, $3,
 (SELECT product_id FROM products WHERE provider = $4), 0, 'info' FROM generate_series(0, $5 - 1) i`, from, step, kind, provider, n)
	}
	insert("a", "ip_overlap", "2026-11-10T20:00:00Z", "1 minute", 13)
	insert("a", "daily_test_finished", "2026-11-10T19:00:00Z", "1 minute", 15)
	insert("a", "pulse_finished", "2026-11-10T18:00:00Z", "10 minutes", 12)
	insert("a", "down", "2026-11-09T10:00:00Z", "1 day", 3)
	insert("b", "daily_test_started", "2026-11-05T10:00:00Z", "1 hour", 4)
	c := db.Conn(t)
	defer c.Close(ctx)
	check := func(name string, in *store.Inputs) {
		t.Helper()
		tabs, ids := map[string]int{}, map[int64]bool{}
		for i, e := range in.TapeHeads {
			tabs[*e.Provider+" "+store.Category(e.Kind)]++
			if ids[e.ID] || (i > 0 && store.Newer(&e, &in.TapeHeads[i-1])) {
				t.Errorf("%s: row %d is twice or out of order", name, e.ID)
			}
			ids[e.ID] = true
		}
		want := map[string]int{"a audit": 13, "a census": store.TapeHeadRows, "a pulse": 12, "a incident": 3, "b census": 4}
		if len(tabs) != len(want) {
			t.Errorf("%s: tabs %v", name, tabs)
		}
		for k, n := range want {
			if tabs[k] != n {
				t.Errorf("%s: %s has %d rows, want %d (%v)", name, k, tabs[k], n, tabs)
			}
		}
		// a's census ids 14 to 23 of 28 (15 rows), every other row.
		if !ids[1] || !ids[13] || !ids[23] || ids[24] || ids[28] || !ids[29] || !ids[46] {
			t.Errorf("%s: ids %v", name, idsOf(in.TapeHeads))
		}
	}
	check("counted", tapeHeads(t, c))
	db.Exec(t, "DROP TABLE test_event_buckets")
	check("without the counts", tapeHeads(t, c))
}

// /v1/events/stats' window ends at the newest event on frozen data
// (HOME-TAPE-1): two test days that ended days ago count their events in the
// 24h range; End is the end of the newest event's 30-minute bucket.
func TestEventStatsEndAtTheNewestEvent(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	ctx := context.Background()
	db.Exec(t, `INSERT INTO test_events(at,kind,signal,severity) SELECT '2026-11-10T20:10:00Z'::timestamptz - i * interval '1 hour',
 CASE WHEN i % 2 = 0 THEN 'pulse_finished' ELSE 'daily_test_finished' END,0,'info' FROM generate_series(0, 47) i`)
	c := db.Conn(t)
	defer c.Close(ctx)
	in, err := store.LoadEvents(ctx, c)
	if err != nil {
		t.Fatal(err)
	}
	s := in.EventStats
	if want := time.Date(2026, 11, 10, 20, 30, 0, 0, time.UTC); !s.End.Equal(want) {
		t.Fatalf("end %v, want %v", s.End, want)
	}
	day := 0
	for _, b := range s.Buckets {
		if !b.Start.Before(s.End.Add(-24 * time.Hour)) {
			day += b.Events
		}
	}
	if day != 24 {
		t.Errorf("the last 24h of the data hold %d events, want 24", day)
	}
	db.Exec(t, "INSERT INTO test_events(at,kind,signal,severity) VALUES (now() + interval '2 days','pulse_finished',0,'info')")
	if in, err := store.LoadEvents(ctx, c); err != nil || in.EventStats.End.After(time.Now().Add(30*time.Minute)) {
		t.Errorf("a future event moves the end past now: %v, %v", err, in.EventStats.End)
	}
}
