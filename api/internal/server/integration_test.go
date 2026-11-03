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
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index-website/api/internal/config"
	"github.com/Zeus-Development/proxy-index-website/api/internal/server"
	"github.com/Zeus-Development/proxy-index-website/api/internal/store"
	"github.com/Zeus-Development/proxy-index-website/api/internal/testdb"
)

// TestServeFromDatabase runs the real read path against a seeded scratch
// database with the site's measured snapshot published, checks the runtime
// pool cannot write, that a database change is picked up, and that cached
// responses meet the latency target over loopback.
func TestServeFromDatabase(t *testing.T) {
	db := testdb.New(t)
	ctx := context.Background()
	w := db.Conn(t)
	defer w.Close(ctx)
	if _, err := store.SeedDemo(ctx, w, config.FindUp(filepath.Join("fixtures", "demo", "design-demo.json")), false); err != nil {
		t.Fatal(err)
	}
	if snap, err := os.ReadFile(config.FindUp(filepath.Join("fixtures", "measured", "snapshot.json"))); err == nil {
		db.Publish(t, snap)
	}
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	if _, err := pool.Exec(ctx, "INSERT INTO site.targets(target_id) VALUES ('nope')"); err == nil || !strings.Contains(err.Error(), "read-only") {
		t.Fatalf("runtime pool could write: %v", err)
	}
	s := server.New(server.DBSource{Pool: pool}, server.Options{Logger: slog.New(slog.NewTextHandler(io.Discard, nil))})
	if _, err := s.Refresh(ctx, true); err != nil {
		t.Fatal(err)
	}
	cur := s.Current()
	t.Logf("build took %v (load %v, assemble %v, render %v); dataset %d bytes identity, %d gzip, %d br", cur.BuildTime, cur.LoadTime, cur.AssembleTime, cur.RenderTime,
		len(cur.Dataset.Identity), len(cur.Dataset.Gzip), len(cur.Dataset.Brotli))
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()
	client := &http.Client{Transport: &http.Transport{MaxIdleConnsPerHost: 4, DisableCompression: true}}
	fetch := func(path string) (*http.Response, []byte) {
		req, _ := http.NewRequest(http.MethodGet, ts.URL+path, nil)
		req.Header.Set("Accept-Encoding", "br")
		resp, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		return resp, body
	}
	for _, p := range []string{"/v1/dataset", "/v1/rankings", "/v1/networks", "/v1/networks/nimbus-residential", "/v1/networks/shifter",
		"/v1/networks/shifter/chart?range=24h", "/v1/networks/all", "/v1/search-index", "/v1/search?q=nimbus", "/v1/coverage", "/v1/snapshot", "/v1/events", "/healthz"} {
		resp, body := fetch(p)
		if resp.StatusCode != 200 {
			t.Errorf("%s: %d %s", p, resp.StatusCode, body)
		}
		t.Logf("%-36s %7d bytes (%s)", p, len(body), resp.Header.Get("Content-Encoding"))
	}

	// A write to the site schema is noticed by the next refresh.
	if _, err := w.Exec(ctx, `INSERT INTO site.changelog(change_id,kind,headline,detail,severity,occurred_at) VALUES ('chg-test','methodology','Test change','x','info',now())`); err != nil {
		t.Fatal(err)
	}
	if rebuilt, err := s.Refresh(ctx, false); err != nil || !rebuilt {
		t.Fatalf("change not picked up: %v %v", rebuilt, err)
	}
	if _, body := fetch("/v1/dataset"); !strings.Contains(string(mustBrotli(t, body)), "Test change") {
		t.Fatal("new changelog row missing from the rebuilt dataset")
	}
	if rebuilt, _ := s.Refresh(ctx, false); rebuilt {
		t.Fatal("rebuilt without a change")
	}

	// Latency of cached responses over loopback (keep-alive, brotli bodies).
	if testing.Short() || raceEnabled {
		return
	}
	for _, p := range []string{"/v1/dataset", "/v1/rankings", "/v1/networks/nimbus-residential", "/v1/search?q=nimbus", "/v1/events"} {
		for i := 0; i < 50; i++ {
			fetch(p)
		}
		const n = 1000
		lat := make([]time.Duration, n)
		for i := range lat {
			start := time.Now()
			fetch(p)
			lat[i] = time.Since(start)
		}
		sort.Slice(lat, func(i, j int) bool { return lat[i] < lat[j] })
		p50, p99 := lat[n/2], lat[n*99/100]
		t.Logf("%-36s p50 %v  p99 %v", p, p50, p99)
		if p99 > 2*time.Millisecond && os.Getenv("SITEAPI_SKIP_LATENCY") == "" {
			t.Errorf("%s p99 %v exceeds 2ms", p, p99)
		}
	}
}

// TestAFreshDatabaseServesEveryPageOfOnePublishedProduct is the database
// DROPLET-1 left (API-ROUTE-CHECK-1): framework data only, no site catalog
// rows, one published product group (Shifter residential, requested country
// US), and a site schema migrated by an older siteapi whose 0001 file differed
// in a comment (the checksum a 2026-10-24 build wrote).
func TestAFreshDatabaseServesEveryPageOfOnePublishedProduct(t *testing.T) {
	db := testdb.New(t)
	ctx := context.Background()
	snap, err := os.ReadFile(config.FindUp(filepath.Join("fixtures", "measured", "snapshot-every-request.json")))
	if err != nil {
		t.Fatal(err)
	}
	db.Publish(t, snap)
	db.Exec(t, "UPDATE site.schema_versions SET checksum='8127cc64a2088bbd3667a4a55dee6f66e79b1d0c271354469aadc8e9c84741ec' WHERE version=1")
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	var catalog int
	if err := pool.QueryRow(ctx, "SELECT count(*) FROM site.providers").Scan(&catalog); err != nil || catalog != 0 {
		t.Fatalf("site.providers has %d rows (%v)", catalog, err)
	}
	s := server.New(server.DBSource{Pool: pool}, server.Options{Logger: slog.New(slog.NewTextHandler(io.Discard, nil))})
	if _, err := s.Refresh(ctx, true); err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()
	for _, p := range []string{"/v1/networks", "/v1/networks/shifter", "/v1/networks/shifter/chart", "/v1/networks/shifter/places",
		"/v1/countries", "/v1/countries/us", "/v1/rankings", "/v1/networks/all"} {
		resp, err := http.Get(ts.URL + p)
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != 200 {
			t.Errorf("%s: %d %s", p, resp.StatusCode, body)
		}
	}
	resp, err := http.Get(ts.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var h server.Health
	if err := json.NewDecoder(resp.Body).Decode(&h); err != nil || h.Status != "ok" || len(h.Unserved) != 0 {
		t.Fatalf("healthz %+v (%v)", h, err)
	}
}

func mustBrotli(t *testing.T, b []byte) []byte {
	t.Helper()
	out, err := io.ReadAll(brotliReader(b))
	if err != nil {
		t.Fatal(err)
	}
	return out
}
