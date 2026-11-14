package server_test

import (
	"context"
	"encoding/json"
	"fmt"
	"maps"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"regexp"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/bench"
	"github.com/Zeus-Development/atlas/api/internal/build"
	"github.com/Zeus-Development/atlas/api/internal/server"
	"github.com/Zeus-Development/atlas/api/internal/testdb"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
	"github.com/jackc/pgx/v5"
)

// hostileValues are what every parameter and path value of every route is
// sent, as they stand in the URL: quotes and SQL fragments, LIKE wildcards,
// zero bytes, invalid UTF-8, bidi marks, 64-bit edges, negative and zero
// numbers, a 5,000-character value, a 2,000-item list, encoded path tricks,
// template markers and a query that does not parse.
var hostileValues = []string{
	"%27", "%27%20OR%20%271%27%3D%271", "1%3B%20DROP%20TABLE%20providers--", "%22%29%20UNION%20SELECT%20NULL--",
	"%25", "_", "%25%25_", "a%00", "%00", "%ff", "%c3%28", "%ed%a0%80", "%e2%80%ae", "%e2%81%a6x%e2%81%a9",
	"9223372036854775807", "9223372036854775808", "-9223372036854775808", "18446744073709551615", "1e309",
	"-1", "0", "-0", strings.Repeat("a", 5000), strings.Repeat("ab,", 1999) + "ab",
	"..", "%2e%2e", "..%2f..%2f", "%2f", "%252e%252e%252f", "%5c..%5c", "a%2fb",
	"%7B%7B7*7%7D%7D", "%24%7B7*7%7D", "%7Bslug%7D", "%3C%25%3D7*7%25%3E", "100%", "a;b",
}

var (
	allowedStatus = map[int]bool{200: true, 302: true, 304: true, 400: true, 404: true}
	addressLike   = regexp.MustCompile(`\b\d{1,3}(\.\d{1,3}){3}\b`)
)

// TestEveryRouteRefusesHostileInput sends every route (every request of
// every page, bench.Specs on testscale.Week, and hostileRoutes: those no page
// draws) each hostileValues value in each parameter and path value, and an
// unknown parameter: only 200, 302, 304, 400 and 404 answer, every one with
// nosniff and no credentials header, no body tells a database error, a
// source file, a local path or an address, nothing is logged as an error, no
// row changes, and every unknown parameter answers 400 unknown_parameter. A
// route no request reaches fails the test.
func TestEveryRouteRefusesHostileInput(t *testing.T) {
	db := testdb.New(t)
	week := testscale.Week
	log := &server.LockedBuffer{}
	s, _ := weekServerTo(t, db, week, testdb.V4Lists{Requested: week.Requested, Rows: week.Lists()}, nil, log)
	h := s.Handler()
	rows := rowCounts(t, db.DSN)
	sh, _ := weekShape(t, s)
	patterns, pattern := server.RoutePatterns(s)
	bases := map[string]string{} // pattern and query names → a request
	var reached []string
	for _, u := range append(walkedRequests(sh), hostileRoutes(t, h, s, sh)...) {
		req := httptest.NewRequest(http.MethodGet, u, nil)
		p := pattern(req)
		reached = append(reached, p)
		bases[p+"?"+strings.Join(slices.Sorted(maps.Keys(req.URL.Query())), "&")] = u
	}
	for _, p := range patterns {
		if p != "/" && !slices.Contains(reached, p) {
			t.Errorf("no request reaches the route %s: add one to hostileRoutes", p)
		}
	}
	forbidden := forbiddenText(t, db.DSN)
	var mu sync.Mutex
	failures, sent := []string{}, 0
	jobs := make(chan string)
	var wg sync.WaitGroup
	for range 8 {
		wg.Go(func() {
			for raw := range jobs {
				if msg := hostileAnswer(h, raw, forbidden); msg != "" {
					mu.Lock()
					failures = append(failures, msg)
					mu.Unlock()
				}
			}
		})
	}
	for _, base := range slices.Sorted(maps.Values(bases)) {
		for _, raw := range hostileRequests(base, pattern) {
			jobs <- raw
			sent++
		}
	}
	close(jobs)
	wg.Wait()
	t.Logf("%d routes, %d requests", len(bases), sent)
	for i, f := range failures {
		if i == 40 {
			t.Errorf("and %d more", len(failures)-i)
			break
		}
		t.Error(f)
	}
	if strings.Contains(log.String(), "level=ERROR") {
		t.Errorf("an error was logged:\n%s", log.String())
	}
	if after := rowCounts(t, db.DSN); !maps.Equal(rows, after) {
		t.Errorf("row counts changed: %v, now %v", rows, after)
	}
}

// walkedRequests is every request of every page, once.
func walkedRequests(sh bench.Shape) []string {
	var out []string
	for _, p := range bench.Expand(bench.Specs, sh, build.Answers()) {
		out = append(out, p.Requests...)
	}
	return out
}

// hostileRoutes is a request of each route and parameter no page draws.
func hostileRoutes(t *testing.T, h http.Handler, s *server.Server, sh bench.Shape) []string {
	slug, kind := sh.Networks[0].Slug, sh.Networks[0].Kinds[0]
	list := "range=30d&q=a&page=1&kind=" + kind + "&country=AA"
	out := []string{"/v1/dataset", s.Current().Dataset.Path(), "/v1/coverage", "/v1/coverage/" + slug, "/healthz",
		"/v1/networks", "/v1/countries", "/v1/countries/AA", "/v1/countries/AA/cities?range=30d&q=a&page=1",
		"/v1/countries/AA/regions?range=30d&q=a&page=1", "/v1/places/city/AA?range=30d", "/v1/places/region/AA?range=30d",
		"/v1/rankings?range=30d&kind=" + kind, "/v1/networks/" + slug + "/places?" + list + "&dimension=city",
		"/v1/networks/" + slug + "/coverage?" + list, "/v1/search?q=net",
		"/v1/events?product=x&network=" + slug + "&kind=down&category=pulse&country=AA&target=generic_http&q=a&proxyKind=" + kind +
			"&since=2026-01-01&after=1&limit=10&event=down",
	}
	r := httptest.NewRecorder()
	h.ServeHTTP(r, httptest.NewRequest(http.MethodGet, "/v1/events?limit=1", nil))
	var tape struct {
		Events []struct {
			ID int64 `json:"id"`
		} `json:"events"`
	}
	if err := json.Unmarshal(r.Body.Bytes(), &tape); err != nil || len(tape.Events) == 0 {
		t.Fatalf("no event to page from: %d %s", r.Code, r.Body.String())
	}
	return append(out, fmt.Sprintf("/v1/events?before=%d&target=generic_http&q=a&event=down", tape.Events[0].ID))
}

// hostileRequests is base with an unknown parameter, then with each
// hostileValues value in each of its parameters and path values (the
// segments its route pattern names).
func hostileRequests(base string, pattern func(*http.Request) string) []string {
	u, _ := url.Parse(base)
	out := []string{base, base + map[bool]string{true: "&", false: "?"}[u.RawQuery != ""] + "cb=1"}
	segs := strings.Split(u.EscapedPath(), "/")
	pat := strings.Split(strings.TrimPrefix(pattern(httptest.NewRequest(http.MethodGet, base, nil)), "GET "), "/")
	q := u.Query()
	for _, v := range hostileValues {
		for i, seg := range pat {
			if strings.HasPrefix(seg, "{") && i < len(segs) {
				path := slices.Clone(segs)
				path[i] = v
				out = append(out, strings.Join(path, "/")+"?"+u.RawQuery)
			}
		}
		for k := range q {
			parts := []string{}
			for other := range q {
				if other != k {
					parts = append(parts, url.QueryEscape(other)+"="+url.QueryEscape(q.Get(other)))
				}
			}
			out = append(out, u.EscapedPath()+"?"+strings.Join(append(parts, k+"="+v), "&"))
		}
	}
	return out
}

// hostileAnswer is what is wrong with the answer to raw ("" when nothing);
// a URL no client can send is skipped.
func hostileAnswer(h http.Handler, raw string, forbidden []string) string {
	req, err := http.NewRequest(http.MethodGet, "http://api.test"+raw, nil)
	if err != nil {
		return ""
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	body, hd := rec.Body.String(), rec.Header()
	short := raw[:min(len(raw), 160)]
	switch {
	case !allowedStatus[rec.Code]:
		return fmt.Sprintf("%s: status %d %s", short, rec.Code, body[:min(len(body), 300)])
	case hd.Get("X-Content-Type-Options") != "nosniff" || hd.Get("Access-Control-Allow-Credentials") != "":
		return fmt.Sprintf("%s: headers %v", short, hd)
	case strings.HasSuffix(raw, "cb=1") && (rec.Code != 400 || !strings.Contains(body, `"unknown_parameter"`)):
		return fmt.Sprintf("%s: an unknown parameter answered %d %s", short, rec.Code, body[:min(len(body), 300)])
	case rec.Code >= 400 && addressLike.MatchString(body):
		return fmt.Sprintf("%s: an address in %s", short, body[:min(len(body), 300)])
	}
	for _, f := range forbidden {
		if strings.Contains(body, f) {
			return fmt.Sprintf("%s: %q in the body", short, f)
		}
	}
	return ""
}

// forbiddenText is what no answer may hold: a database error, a Go source
// position or stack, the working and temporary directories, the database host.
func forbiddenText(t *testing.T, dsn string) []string {
	out := []string{"SQLSTATE", "pgx", ".go:", "goroutine ", os.TempDir()}
	if wd, err := os.Getwd(); err == nil {
		out = append(out, wd)
	}
	if cfg, err := pgx.ParseConfig(dsn); err == nil && len(cfg.Host) > 3 {
		out = append(out, cfg.Host)
	}
	return out
}

// rowCounts is every table's row count.
func rowCounts(t *testing.T, dsn string) map[string]int64 {
	ctx := context.Background()
	c, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close(ctx)
	rows, err := c.Query(ctx, `SELECT format('%I.%I', table_schema, table_name) FROM information_schema.tables
 WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1`)
	if err != nil {
		t.Fatal(err)
	}
	names, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		t.Fatal(err)
	}
	out := map[string]int64{}
	for _, n := range names {
		var count int64
		if err := c.QueryRow(ctx, "SELECT count(*) FROM "+n).Scan(&count); err != nil {
			t.Fatal(err)
		}
		out[n] = count
	}
	return out
}
