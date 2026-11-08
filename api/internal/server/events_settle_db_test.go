package server

import (
	"context"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/config"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
)

// EV-CURSOR-SETTLE-1 against the database: while a transaction that wrote an
// event is open, a later committed event is held back from after answers
// (more stays true), from memory and from the fallback alike; once it commits
// and the rows settle, the next read returns both in id order, with no new
// event to trigger it. A burst of 300 rows in one transaction reads whole.
func TestAfterHoldsBackRowsBehindAnOpenTransaction(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	ctx := context.Background()
	snap, err := os.ReadFile(config.FindUp(filepath.Join("fixtures", "measured", "snapshot.json")))
	if err != nil {
		t.Fatal(err)
	}
	db.PublishV4(t, snap)
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	s := newTestServer(t, DBSource{Pool: pool})
	// poll reads after= from memory and from the fallback; both must agree.
	poll := func(after int64) tapeBody {
		t.Helper()
		b := readTape(t, s, "/v1/events?limit=100&after="+strconv.FormatInt(after, 10))
		f := store.EventFilter{After: &after, Limit: 100}
		rows, err := store.QueryEvents(ctx, pool, f)
		if err != nil {
			t.Fatal(err)
		}
		a := s.Current().Result.Events.FromRows(f, rows)
		if !slices.Equal(eventIDsOf(a.Events), b.ids()) || *a.Cursor != *b.Cursor || a.More != b.More {
			t.Fatalf("after %d: memory %v cursor %d more %v, fallback %v cursor %d more %v", after, b.ids(), *b.Cursor, b.More,
				eventIDsOf(a.Events), *a.Cursor, a.More)
		}
		return b
	}
	insert := `INSERT INTO test_events(at,kind,signal,severity) SELECT '2026-11-09T12:00:00Z','pulse_finished',0,'info' FROM generate_series(1,$1::int)`
	c := db.Conn(t)
	defer c.Close(ctx)
	txA, err := c.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer txA.Rollback(ctx) //nolint:errcheck
	// Event 1 in transaction A, which stays open; event 2 committed after it.
	if _, err := txA.Exec(ctx, insert, 1); err != nil {
		t.Fatal(err)
	}
	db.Exec(t, insert, 1)
	if refreshed, err := s.Refresh(ctx, false); err != nil || !refreshed {
		t.Fatalf("not refreshed: %v %v", refreshed, err)
	}
	if b := poll(0); len(b.Events) != 0 || *b.Cursor != 0 || !b.More {
		t.Fatalf("with A open: %v cursor %d more %v", b.ids(), *b.Cursor, b.More)
	}
	if b := readTape(t, s, "/v1/events"); !slices.Equal(b.ids(), []int64{2}) {
		t.Fatalf("the default poll: %v", b.ids())
	}
	if err := txA.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	db.Settle(t)
	if refreshed, err := s.Refresh(ctx, false); err != nil || !refreshed {
		t.Fatalf("held-back rows not read again: %v %v", refreshed, err)
	}
	if b := poll(0); !slices.Equal(b.ids(), []int64{1, 2}) || *b.Cursor != 2 || b.More {
		t.Fatalf("after A commits: %v cursor %d more %v", b.ids(), *b.Cursor, b.More)
	}
	db.Exec(t, insert, 300) // events 3 to 302, one transaction
	db.Settle(t)
	s.Refresh(ctx, false) //nolint:errcheck
	ids := []int64{}
	for after, more := int64(2), true; more; {
		b := poll(after)
		if len(ids) > 300 || len(b.Events) == 0 {
			t.Fatalf("the burst stalls at %d: %v", after, b.ids())
		}
		ids, after, more = append(ids, b.ids()...), *b.Cursor, b.More
	}
	for i, id := range ids {
		if id != int64(i+3) {
			t.Fatalf("the burst read %d rows, row %d is %d", len(ids), i, id)
		}
	}
	if len(ids) != 300 {
		t.Fatalf("the burst read %d rows", len(ids))
	}
}

func eventIDsOf(rows []build.EventRow) []int64 {
	out := []int64{}
	for _, r := range rows {
		out = append(out, r.ID)
	}
	return out
}
