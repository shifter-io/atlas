package store_test

import (
	"context"
	"slices"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
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
