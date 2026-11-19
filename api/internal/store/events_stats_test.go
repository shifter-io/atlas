package store_test

import (
	"context"
	"testing"
	"time"

	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testdb"
)

// The event counts read with the events (TAPE-STATS-1): the trigger's buckets
// of the 30 days before End add up to the events there, each product's
// newest store.RecentSignals events come with them, and a framework schema
// without test_event_buckets reads as Missing without failing the read.
func TestLoadEventsReadsTheEventCounts(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	ctx := context.Background()
	db.Exec(t, "INSERT INTO providers VALUES ('a'),('b')")
	db.Exec(t, "INSERT INTO products(provider,name) VALUES ('a','residential'),('b','residential')")
	// a: 30 events, one a minute back from now; b: 2 two hours back; one
	// without a product; one of a 40 days back, outside every range.
	db.Exec(t, `INSERT INTO test_events(at,kind,product_id,signal) SELECT now() - i * interval '1 minute',
 CASE WHEN i % 2 = 0 THEN 'pulse_finished' ELSE 'latency_rise' END, product_id, i % 3 - 1
 FROM products, generate_series(0, 29) i WHERE provider='a'`)
	db.Exec(t, `INSERT INTO test_events(at,kind,product_id,signal) SELECT now() - interval '2 hours', 'ip_overlap', product_id, 0
 FROM products, generate_series(1, 2) WHERE provider='b'`)
	db.Exec(t, `INSERT INTO test_events(at,kind,signal) VALUES (now() - interval '1 hour', 'no_node', -1)`)
	db.Exec(t, `INSERT INTO test_events(at,kind,product_id,signal) SELECT now() - interval '40 days', 'pulse_down', product_id, -1
 FROM products WHERE provider='a'`)
	c := db.Conn(t)
	defer c.Close(ctx)
	in, err := store.LoadEvents(ctx, c)
	if err != nil {
		t.Fatal(err)
	}
	s := in.EventStats
	if s.Missing || s.End.Truncate(30*time.Minute) != s.End || time.Until(s.End) <= 0 || time.Until(s.End) > 30*time.Minute {
		t.Fatalf("end %v, missing %v", s.End, s.Missing)
	}
	events, providers, product := 0, map[string]int{}, 0
	for _, b := range s.Buckets {
		events += b.Events
		if b.Provider == nil {
			product++
		} else {
			providers[*b.Provider] += b.Events
		}
	}
	if events != 33 || providers["a"] != 30 || providers["b"] != 2 || product != 1 {
		t.Errorf("buckets %d events: %v, %d product-less", events, providers, product)
	}
	recent := map[string]int{}
	for _, r := range s.Recent {
		recent[r.Provider]++
	}
	if recent["a"] != store.RecentSignals || recent["b"] != 2 || len(recent) != 2 {
		t.Errorf("recent %v", recent)
	}
	db.Exec(t, "DROP TABLE test_event_buckets")
	if in, err := store.LoadEvents(ctx, c); err != nil || !in.EventStats.Missing || in.EventStats.Buckets != nil || len(in.Events) != 34 {
		t.Errorf("without the table: %v, %+v", err, in.EventStats)
	}
}
