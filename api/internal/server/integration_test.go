package server_test

import (
	"bytes"
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

	"github.com/Zeus-Development/proxy-index/api/internal/config"
	"github.com/Zeus-Development/proxy-index/api/internal/server"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// siteCatalog is test-only site data in every table the network pages read:
// the measured network's catalog row, product, pricing with a plan, trust
// signals, a review, a community run, a changelog row and a tape row, and
// one catalog network the framework does not measure.
const siteCatalog = `INSERT INTO site.providers(slug,name,framework_provider,hq,website) VALUES ('shifter','Shifter','shifter','Somewhere','https://shifter.example');
INSERT INTO site.providers(slug,name,kinds,primary_kind) VALUES ('acme-proxies','Acme Proxies','{datacenter}','datacenter');
INSERT INTO site.provider_products(provider_slug,product_kind,name,measured,framework_product) VALUES ('shifter','residential_rotating','Residential',true,'residential');
INSERT INTO site.pricing(provider_slug,category,model,price_per_gb) VALUES ('shifter','residential','per_gb',3);
INSERT INTO site.pricing_plans(provider_slug,category,position,name,price,gb) VALUES ('shifter','residential',0,'Starter',30,10);
INSERT INTO site.trust_signals(provider_slug,category,kyc_verified,claimed_pool_size) VALUES ('shifter','residential',true,1000000);
INSERT INTO site.reviews(review_id,provider_slug,author,role,rating,title,body,product_kind,use_case,monthly_spend,months_used,verification,posted_at)
 VALUES ('rev-1','shifter','A. Tester','Engineer',4,'Steady pool','Works.','residential_rotating','scraping','$100',3,'unverified',now());
INSERT INTO site.community_runs(run_id,provider_slug,submitted_by,region,product_kind,plan_tier,collector_version,status,submitted_at)
 VALUES ('run-1','shifter','tester','eu','residential_rotating','starter','1.0','accepted',now());
INSERT INTO site.changelog(change_id,kind,provider_slug,headline,detail,severity,occurred_at) VALUES ('chg-1','pricing','shifter','Price cut','x','good',now());
INSERT INTO site.tape_events(tape_id,kind,provider_slug,product,title,detail,occurred_at) VALUES ('tape-1','pool','shifter','residential','Pool grew','x',now());`

// TestServeFromDatabase runs the real read path against a scratch database
// with the measured snapshot published and test catalog rows, checks the
// runtime pool cannot write, that a database change is picked up, and that
// cached responses meet the latency target over loopback.
func TestServeFromDatabase(t *testing.T) {
	db := testdb.New(t)
	ctx := context.Background()
	w := db.Conn(t)
	defer w.Close(ctx)
	snap, err := os.ReadFile(config.FindUp(filepath.Join("fixtures", "measured", "snapshot.json")))
	if err != nil {
		t.Fatal(err)
	}
	db.Publish(t, snap)
	db.Exec(t, siteCatalog)
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	if _, err := pool.Exec(ctx, "INSERT INTO site.targets(target_id) VALUES ('nope')"); err == nil || !strings.Contains(err.Error(), "read-only") {
		t.Fatalf("runtime pool could write: %v", err)
	}
	s := server.New(server.DBSource{Pool: pool}, server.Options{Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), FilesDir: t.TempDir()})
	if _, err := s.Refresh(ctx, true); err != nil {
		t.Fatal(err)
	}
	cur := s.Current()
	t.Logf("build took %v (load %v, assemble %v, render %v); dataset %d bytes identity, %d gzip, %d br", cur.BuildTime, cur.LoadTime, cur.AssembleTime, cur.RenderTime,
		cur.Dataset.Bytes, cur.Dataset.Gzip, cur.Dataset.Brotli)
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
	for _, p := range []string{"/v1/dataset", "/v1/rankings", "/v1/networks", "/v1/networks/acme-proxies", "/v1/networks/shifter",
		"/v1/networks/shifter/chart?range=24h", "/v1/networks/all", "/v1/search-index", "/v1/search?q=shifter", "/v1/coverage", "/v1/events", "/healthz"} {
		resp, body := fetch(p)
		if resp.StatusCode != 200 {
			t.Errorf("%s: %d %s", p, resp.StatusCode, body)
		}
		t.Logf("%-36s %7d bytes (%s)", p, len(body), resp.Header.Get("Content-Encoding"))
	}
	if resp, _ := fetch("/v1/snapshot"); resp.StatusCode != 404 {
		t.Errorf("/v1/snapshot: %d, want 404 (removed)", resp.StatusCode)
	}
	// The network answer carries the catalog rows of every site table it reads.
	_, body := fetch("/v1/networks/shifter")
	var ans struct {
		Profile struct {
			HQ      *string `json:"hq"`
			Pricing struct {
				Plans []struct {
					Name *string `json:"name"`
				} `json:"plans"`
			} `json:"pricing"`
		} `json:"profile"`
		Reviews       []struct{ ID string } `json:"reviews"`
		CommunityRuns []struct{ ID string } `json:"communityRuns"`
		Changelog     []struct{ ID string } `json:"changelog"`
		Tape          []struct{ ID string } `json:"tape"`
	}
	if err := json.Unmarshal(mustBrotli(t, body), &ans); err != nil {
		t.Fatal(err)
	}
	if ans.Profile.HQ == nil || len(ans.Profile.Pricing.Plans) != 1 || len(ans.Reviews) != 1 || len(ans.CommunityRuns) != 1 || len(ans.Changelog) != 1 || len(ans.Tape) == 0 {
		t.Errorf("network answer lacks catalog rows: %s", mustBrotli(t, body))
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
	for _, p := range []string{"/v1/dataset", "/v1/rankings", "/v1/networks/acme-proxies", "/v1/search?q=shifter", "/v1/events"} {
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
// US). The site schema is, in turn, migrated by an older siteapi whose 0001
// file differed in a comment (the checksum a 2026-10-24 build wrote), missing
// altogether (`siteapi migrate` never ran), or missing some tables and a
// column. Every page answers 200, and a missing site table is logged once.
func TestAFreshDatabaseServesEveryPageOfOnePublishedProduct(t *testing.T) {
	for _, c := range []struct {
		name string
		db   func(t *testing.T) *testdb.DB
		gaps int
	}{
		{"older siteapi migration", func(t *testing.T) *testdb.DB {
			db := testdb.New(t)
			db.Exec(t, "UPDATE site.schema_versions SET checksum='8127cc64a2088bbd3667a4a55dee6f66e79b1d0c271354469aadc8e9c84741ec' WHERE version=1")
			return db
		}, 0},
		{"no site schema", func(t *testing.T) *testdb.DB { return testdb.NewFramework(t) }, 1},
		{"site tables missing", func(t *testing.T) *testdb.DB {
			db := testdb.New(t)
			db.Exec(t, "DROP TABLE site.reviews, site.tape_events, site.ranking_profiles; ALTER TABLE site.pricing_plans DROP COLUMN catalog")
			return db
		}, 4},
		{"browser targets", func(t *testing.T) *testdb.DB {
			db := testdb.NewFramework(t)
			db.Exec(t, testscale.BrowserTargetsSQL())
			return db
		}, 1},
	} {
		t.Run(c.name, func(t *testing.T) { freshDatabaseServesEveryPage(t, c.db(t), c.gaps) })
	}
}

func freshDatabaseServesEveryPage(t *testing.T, db *testdb.DB, gaps int) {
	ctx := context.Background()
	snap, err := os.ReadFile(config.FindUp(filepath.Join("fixtures", "measured", "snapshot-every-request.json")))
	if err != nil {
		t.Fatal(err)
	}
	db.Publish(t, snap)
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	var logs bytes.Buffer
	s := server.New(server.DBSource{Pool: pool}, server.Options{Logger: slog.New(slog.NewTextHandler(&logs, nil)), FilesDir: t.TempDir()})
	for range 2 {
		if _, err := s.Refresh(ctx, true); err != nil {
			t.Fatal(err)
		}
	}
	if n := strings.Count(logs.String(), "site tables missing"); (gaps > 0) != (n == 1) || n > 1 {
		t.Errorf("missing site tables logged %d times over two builds (%d gaps):\n%s", n, gaps, logs.String())
	}
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()
	for _, p := range []string{"/v1/networks", "/v1/networks/shifter", "/v1/networks/shifter/chart", "/v1/networks/shifter/places",
		"/v1/countries", "/v1/countries/us", "/v1/countries/us/cities", "/v1/rankings", "/v1/networks/all", "/v1/dataset",
		"/v1/search-index", "/v1/search?q=shifter", "/v1/coverage", "/v1/coverage/shifter", "/v1/events", "/v1/version"} {
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
	if err := json.NewDecoder(resp.Body).Decode(&h); err != nil || h.Status != "ok" || len(h.Unserved) != 0 || len(h.SiteGaps) != gaps {
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
