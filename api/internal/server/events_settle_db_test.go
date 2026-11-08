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
	"github.com/jackc/pgx/v5"
)

// EV-CURSOR-SETTLE-1 against the database, the case one snapshot cannot
// catch: B gets its xid first (it writes another row), A inserts event 1 and
// stays open, B inserts event 2 and commits. Alone on the server, the read
// then sees a snapshot whose xmin and xmax are A's xid, so the mark of a
// read is checked against A's xid directly. While A is open no poll passes
// id 1, from memory or the fallback, and the default poll's cursor stops
// below it; once A commits, both arrive in id order, through the default
// poll's cursor too, with no new event to trigger the read. A burst of 300
// rows in one transaction then reads whole across pages.
func TestNoCursorPassesAnIdOfAnOpenTransaction(t *testing.T) {
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
	refresh := func() {
		t.Helper()
		if _, err := s.Refresh(ctx, false); err != nil {
			t.Fatal(err)
		}
	}
	// A bound is known (0, the table is empty) once a read follows the first
	// one with every transaction of it ended: a full build carries the reads.
	db.Settle(t)
	if _, err := s.Refresh(ctx, true); err != nil {
		t.Fatal(err)
	}
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
	begin := func() *pgxTx {
		t.Helper()
		c := db.Conn(t)
		t.Cleanup(func() { c.Close(ctx) })
		tx, err := c.Begin(ctx)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { tx.Rollback(ctx) }) //nolint:errcheck
		return &pgxTx{t, tx}
	}
	b, a := begin(), begin()
	b.exec("INSERT INTO providers VALUES ('writer-b')") // B's xid, older than A's
	a.exec(insert, 1)                                   // event 1, A stays open
	b.exec(insert, 1)                                   // event 2
	b.commit()
	// Whatever else the server runs, a read while A is open marks a Next
	// above A's xid, and its xmin is not above it: A was handed its xid.
	var xidA int64
	if err := a.tx.QueryRow(ctx, "SELECT pg_current_xact_id()::text::bigint").Scan(&xidA); err != nil {
		t.Fatal(err)
	}
	in, err := store.LoadEvents(ctx, pool)
	if err != nil {
		t.Fatal(err)
	}
	if r := in.EventsRead; r.MaxID != 2 || r.Next <= xidA || r.Xmin > xidA {
		t.Fatalf("a read with A (xid %d) open: %+v", xidA, r)
	}
	refresh()
	var handOff *int64
	for i := 0; i < 3; i++ { // a few refreshes while A is open
		if p := poll(0); len(p.Events) != 0 || *p.Cursor != 0 || !p.More {
			t.Fatalf("with A open, poll %d: %v cursor %d more %v", i, p.ids(), *p.Cursor, p.More)
		}
		def := readTape(t, s, "/v1/events")
		if !slices.Equal(def.ids(), []int64{2}) || *def.Cursor != 0 || !def.More {
			t.Fatalf("the default poll with A open: %v cursor %d more %v", def.ids(), *def.Cursor, def.More)
		}
		handOff = def.Cursor
		refresh()
	}
	a.commit()
	db.Settle(t)
	refresh() // no new event: the held ids alone make the server read again
	if s.Current().Result.Events.Pending() {
		t.Fatal("still pending once every transaction ended")
	}
	if p := poll(*handOff); !slices.Equal(p.ids(), []int64{1, 2}) || *p.Cursor != 2 || p.More {
		t.Fatalf("after A commits, from the default poll's cursor: %v cursor %d more %v", p.ids(), *p.Cursor, p.More)
	}
	db.Exec(t, insert, 300) // events 3 to 302, one transaction
	refresh()
	db.Settle(t)
	refresh()
	ids := []int64{}
	for after, more := int64(2), true; more; {
		p := poll(after)
		if len(ids) > 300 || len(p.Events) == 0 {
			t.Fatalf("the burst stalls at %d: %v", after, p.ids())
		}
		ids, after, more = append(ids, p.ids()...), *p.Cursor, p.More
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

// pgxTx is an open transaction that fails the test on an error.
type pgxTx struct {
	t  *testing.T
	tx pgx.Tx
}

func (x *pgxTx) exec(sql string, args ...any) {
	x.t.Helper()
	if _, err := x.tx.Exec(context.Background(), sql, args...); err != nil {
		x.t.Fatal(err)
	}
}

func (x *pgxTx) commit() {
	x.t.Helper()
	if err := x.tx.Commit(context.Background()); err != nil {
		x.t.Fatal(err)
	}
}

func eventIDsOf(rows []build.EventRow) []int64 {
	out := []int64{}
	for _, r := range rows {
		out = append(out, r.ID)
	}
	return out
}
