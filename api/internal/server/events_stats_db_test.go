package server_test

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/shifter-io/atlas/api/internal/config"
	"github.com/shifter-io/atlas/api/internal/server"
	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testdb"
)

// /v1/events/stats against the database (TAPE-STATS-1): served from memory
// with the events' cache lifetime, and rebuilt by an events-only refresh (no
// full load) from the counts the trigger keeps; network= narrows it, an
// unknown network answers 404 and an unknown parameter 400.
func TestEventsStatsFollowNewEvents(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	ctx := context.Background()
	snap, err := os.ReadFile(config.FindUp(filepath.Join("fixtures", "measured", "snapshot.json")))
	if err != nil {
		t.Fatal(err)
	}
	db.PublishV4(t, snap)
	db.Exec(t, "INSERT INTO providers VALUES ('shifter') ON CONFLICT DO NOTHING")
	db.Exec(t, "INSERT INTO products(provider,name) VALUES ('shifter','residential') ON CONFLICT DO NOTHING")
	insert := `INSERT INTO test_events(at,kind,product_id,signal) SELECT now() - i * interval '1 minute', 'pulse_finished', product_id, 1
 FROM products, generate_series(1, $1::int) i WHERE provider='shifter' AND name='residential'`
	db.Exec(t, insert, 3)
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	src := &loadCounter{DBSource: server.DBSource{Pool: pool}}
	s := server.New(src, server.Options{Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), FilesDir: t.TempDir()})
	if _, err := s.Refresh(ctx, true); err != nil {
		t.Fatal(err)
	}
	type body struct {
		Totals   struct{ Events, Good, Reporting int }
		Networks []struct {
			Slug   string
			Events int
			Recent []int
		}
	}
	read := func(path string, status int) body {
		t.Helper()
		rec := httptest.NewRecorder()
		s.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		var b body
		if rec.Code != status || status == http.StatusOK && (json.Unmarshal(rec.Body.Bytes(), &b) != nil || rec.Header().Get("Cache-Control") != server.CacheEvents) {
			t.Fatalf("%s: %d %s %s", path, rec.Code, rec.Header().Get("Cache-Control"), rec.Body.String())
		}
		return b
	}
	if b := read("/v1/events/stats?range=24h", http.StatusOK); b.Totals.Events != 3 || b.Totals.Reporting != 1 || b.Networks[0].Slug != "shifter" {
		t.Fatalf("before %+v", b)
	}
	db.Exec(t, insert, 2) // two more rows, in one statement
	if refreshed, err := s.Refresh(ctx, false); err != nil || !refreshed || src.loads.Load() != 1 {
		t.Fatalf("refreshed %v, %v, %d full loads", refreshed, err, src.loads.Load())
	}
	for _, path := range []string{"/v1/events/stats?range=24h", "/v1/events/stats?network=shifter"} {
		if b := read(path, http.StatusOK); b.Totals.Events != 5 || b.Totals.Good != 5 || b.Networks[0].Events != 5 || len(b.Networks[0].Recent) != 5 {
			t.Errorf("%s after: %+v", path, b)
		}
	}
	read("/v1/events/stats?network=nope", http.StatusNotFound)
	read("/v1/events/stats?limit=5", http.StatusBadRequest)
}
