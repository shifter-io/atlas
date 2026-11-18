package server_test

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"

	"github.com/shifter-io/atlas/api/internal/config"
	"github.com/shifter-io/atlas/api/internal/server"
	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testdb"
)

// loadCounter is the database source, counting full loads.
type loadCounter struct {
	server.DBSource
	loads atomic.Int64
}

func (c *loadCounter) Load(ctx context.Context) (*store.Inputs, error) {
	c.loads.Add(1)
	return c.DBSource.Load(ctx)
}

// eventQueries are the /v1/events answers compared after each refresh: the
// live tape's polls (prioritized) and named kinds and tabs, some answered from
// memory and some only by the database once the window is cut.
var eventQueries = []string{"/v1/events", "/v1/events?limit=500", "/v1/events?limit=1", "/v1/events?kind=incident&limit=500",
	"/v1/events?kind=census", "/v1/events?event=pulse_down&limit=500", "/v1/events?event=pulse_down%2Clatency_rise&limit=50",
	"/v1/events?limit=500&since=2026-11-02T00%3A20%3A00Z", "/v1/events?kind=pulse&limit=500&since=2026-11-01T23%3A59%3A00Z"}

// API-CACHE-2 against the database (the v1 fixture as a v4 build): with 1, 2,000 (the in-memory window) and
// 2,501 events (the window cut, older notable rows held past it), an
// events-only change refreshes without a full load, keeps every other answer's
// Rendered, and answers every events query byte for byte like a full build of
// the same database.
func TestEventAnswersAfterAnEventsOnlyRefresh(t *testing.T) {
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
	quiet := server.Options{Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), FilesDir: t.TempDir()}
	src := &loadCounter{DBSource: server.DBSource{Pool: pool}}
	s := server.New(src, quiet)
	if _, err := s.Refresh(ctx, true); err != nil {
		t.Fatal(err)
	}
	first := s.Current()
	held := 0
	for _, total := range []int{1, store.EventWindow, 2501} {
		// Event i is written at i seconds past 2026-11-02 00:00 UTC: pulse downs
		// and latency rises (notable) among pulse and census rows (routine).
		db.Exec(t, `INSERT INTO test_events(at,kind,signal,severity) SELECT '2026-11-02T00:00:00Z'::timestamptz + i * interval '1 second',
 CASE WHEN i % 50 = 0 THEN 'pulse_down' WHEN i % 7 = 0 THEN 'latency_rise' WHEN i % 3 = 0 THEN 'census_finished' ELSE 'pulse_finished' END,
 0, 'info' FROM generate_series($1::int, $2::int) i`, held+1, total)
		held = total
		if refreshed, err := s.Refresh(ctx, false); err != nil || !refreshed {
			t.Fatalf("%d events: not refreshed: %v %v", total, refreshed, err)
		}
		// The cursor bound (EV-CURSOR-SETTLE-1) depends on the reads before:
		// once the rows settle, a read raises it to the newest id, here and
		// in the full build below, so their cursors agree.
		db.Settle(t)
		if _, err := s.Refresh(ctx, false); err != nil {
			t.Fatal(err)
		}
		cur := s.Current()
		if src.loads.Load() != 1 || cur.Dataset != first.Dataset {
			t.Fatalf("%d events: %d full loads, dataset kept %v", total, src.loads.Load(), cur.Dataset == first.Dataset)
		}
		for path, r := range first.Static {
			if cur.Static[path] != r {
				t.Errorf("%d events: %s rendered again", total, path)
			}
		}
		full := server.New(server.DBSource{Pool: pool}, quiet)
		for range 2 {
			if _, err := full.Refresh(ctx, true); err != nil {
				t.Fatal(err)
			}
			db.Settle(t)
		}
		for _, q := range eventQueries {
			got, want := identity(t, s, q), identity(t, full, q)
			if got != want {
				t.Errorf("%d events: %s differs from a full build:\n got %.300s\nwant %.300s", total, q, got, want)
			}
		}
	}
	if refreshed, _ := s.Refresh(ctx, false); refreshed {
		t.Fatal("refreshed without a change")
	}
}

func identity(t *testing.T, s *server.Server, path string) string {
	t.Helper()
	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("%s: %d %s", path, rec.Code, rec.Body.String())
	}
	return rec.Body.String()
}
