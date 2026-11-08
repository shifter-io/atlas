package server_test

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"slices"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/server"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// pageBudget is the p99 budget of one kind of request (PAGE-LATENCY-1).
type pageBudget struct {
	name  string
	limit time.Duration
}

var (
	answerBudget     = pageBudget{"a precomputed answer", 2 * time.Millisecond}
	eventsBudget     = pageBudget{"/v1/events", 2 * time.Millisecond}
	searchBudget     = pageBudget{"/v1/search?q=", 5 * time.Millisecond}
	firstBudget      = pageBudget{"a list page, first render", 50 * time.Millisecond}
	cachedBudget     = pageBudget{"a list page, cached", 2 * time.Millisecond}
	listSearchBudget = pageBudget{"a q= search over a 30,000-row list", 50 * time.Millisecond}
)

const (
	pageTotal    = 10 * time.Millisecond // the sum of one page's requests' p99
	rebuildLimit = 30 * time.Second      // the full rebuild of testscale.Week
	testLimit    = 60 * time.Second      // the whole test
	warmups      = 50                    // requests before the timed ones
	timed        = 200                   // timed requests of one repeated request
)

var weekRanges = []string{"24h", "7d", "30d"}

// TestPageLatency times every request of every Proxy Index page (docs/api-web-map.md
// section 10, after section 11) against testscale.Week as a v4 build, over loopback
// with keep-alive and brotli: each precomputed answer and cached list page
// repeated, and first renders and searches each asked once. Each kind of request
// has a p99 budget, and the p99 of one page's requests add up to at most pageTotal.
func TestPageLatency(t *testing.T) {
	// Serial and alone: api.yml runs it in its own process after the other tests.
	start := time.Now()
	db := testdb.New(t)
	if testing.Short() || raceEnabled {
		t.Skip("measures latency")
	}
	ts := httptest.NewServer(weekServer(t, db).Handler())
	defer ts.Close()
	m := &meter{t: t, base: ts.URL, client: &http.Client{Transport: &http.Transport{MaxIdleConnsPerHost: 4, DisableCompression: true}}}
	slugs := make([]string, testscale.Week.Networks)
	for n := range slugs {
		slugs[n] = fmt.Sprintf("net%02d", n)
	}
	p99 := map[string]time.Duration{}
	for _, u := range repeatedRequests(slugs) {
		p99[u] = m.repeat(u, budgetOf(u))
	}
	// First renders: page 2 of each list, its rows not read yet (net00's pages warm up).
	m.distinct(firstBudget, coveragePages(slugs[:1], 2, 18)[:warmups], coveragePages(slugs[1:], 2, 2))
	m.distinct(listSearchBudget, nil, listSearches(slugs, warmups+timed))
	m.distinct(searchBudget, nil, searches(warmups+timed))
	checkPageTotals(t, slugs, p99)
	if took := time.Since(start); took > testLimit {
		t.Errorf("the test took %v, over its %v budget", took, testLimit)
	}
}

// weekServer publishes testscale.Week as a v4 build with its browser targets
// and test events, and returns a server of its build, which must take at most
// rebuildLimit.
func weekServer(t *testing.T, db *testdb.DB) *server.Server {
	ctx := context.Background()
	start := time.Now()
	week := testscale.Week
	payload, err := week.Payload()
	if err != nil {
		t.Fatal(err)
	}
	db.Exec(t, week.TargetsSQL())
	db.PublishV4With(t, payload, testdb.V4Lists{Requested: week.Requested, Rows: week.Lists()})
	db.Exec(t, week.EventsSQL())
	// As production's autovacuum leaves it (its settings in calculated/schema.sql).
	db.Exec(t, "VACUUM ANALYZE calculated_list_rows")
	t.Logf("published the week in %v", time.Since(start).Round(time.Millisecond))
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	s := server.New(server.DBSource{Pool: pool}, server.Options{Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), FilesDir: t.TempDir()})
	start = time.Now()
	if _, err := s.Refresh(ctx, true); err != nil {
		t.Fatal(err)
	}
	took, cur := time.Since(start), s.Current()
	t.Logf("full rebuild took %v (load %v, assemble %v, render %v); dataset %d bytes", took.Round(time.Millisecond),
		cur.LoadTime, cur.AssembleTime, cur.RenderTime, cur.Dataset.Bytes)
	if took > rebuildLimit {
		t.Errorf("the full rebuild took %v, over its %v budget", took, rebuildLimit)
	}
	return s
}

// weekPage is a page of docs/api-web-map.md section 10 and the requests one
// view of it makes once section 11 is done: requests on the existing routes,
// and the routes of answers registered from Phase 3 (api/answers/), each
// standing for its Samples. A route not registered yet is left out.
type weekPage struct {
	name     string
	requests []string
	answers  []string
}

// weekPages is every page, the network page that of the network slug.
func weekPages(slug string) []weekPage {
	rk := perRange("/v1/rankings?range=%s")
	network := append([]string{"/v1/networks/" + slug}, perRange("/v1/networks/"+slug+"/chart?range=%s")...)
	network = append(append(network, perRange("/v1/networks/"+slug+"/coverage?range=%s")...), rk...)
	return []weekPage{
		{"/proxy-index", append(slices.Clone(rk), "/v1/events"), []string{"/v1/home"}},
		{"/proxy-index/targets", nil, []string{"/v1/targets", "/v1/meta"}},
		{"/proxy-index/networks", rk, []string{"/v1/meta"}},
		{"/proxy-index/networks/" + slug, network, []string{"/v1/targets", "/v1/overlap"}},
		{"/proxy-index/compare", rk, []string{"/v1/targets", "/v1/meta"}},
		{"/proxy-index/overlap", []string{"/v1/rankings?range=30d"}, []string{"/v1/overlap"}},
		{"/proxy-index/true-cost", rk, []string{"/v1/targets"}},
		{"/proxy-index/live-tape", []string{"/v1/events?limit=200", "/v1/events"}, []string{"/v1/events/stats"}},
		{"search palette (every page)", []string{"/v1/search-index"}, nil},
		{"freshness (every page)", []string{"/v1/version"}, nil},
	}
}

func perRange(format string) []string {
	out := []string{}
	for _, r := range weekRanges {
		out = append(out, fmt.Sprintf(format, r))
	}
	return out
}

// pageRequests is every request of page p: its requests and the samples of
// its registered answers.
func pageRequests(p weekPage) []string {
	out := slices.Clone(p.requests)
	for _, route := range p.answers {
		for _, a := range build.Answers() {
			if a.Route == "GET "+route {
				out = append(out, a.Samples...)
			}
		}
	}
	return out
}

// repeatedRequests is every request timed repeatedly: every page's, every
// registered answer's samples and the other precomputed answers.
func repeatedRequests(slugs []string) []string {
	out := []string{"/v1/networks", "/v1/networks/all", "/v1/countries", "/v1/coverage"}
	for _, slug := range slugs {
		for _, p := range weekPages(slug) {
			out = append(out, pageRequests(p)...)
		}
		out = append(out, "/v1/coverage/"+slug)
	}
	for _, a := range build.Answers() {
		out = append(out, a.Samples...)
	}
	slices.Sort(out)
	return slices.Compact(out)
}

// budgetOf is the budget of a repeated request.
func budgetOf(u string) pageBudget {
	switch {
	case strings.HasPrefix(u, "/v1/events"):
		return eventsBudget
	case strings.Contains(u, "/coverage?"):
		return cachedBudget
	}
	return answerBudget
}

// coveragePages is pages from to last of each network's City x ASN list
// (COV, the only long list a page asks for), in every range.
func coveragePages(slugs []string, from, last int) []string {
	out := []string{}
	for page := from; page <= last; page++ {
		for _, slug := range slugs {
			for _, r := range weekRanges {
				out = append(out, fmt.Sprintf("/v1/networks/%s/coverage?range=%s&page=%d", slug, r, page))
			}
		}
	}
	return out
}

// listSearches is n searches of the networks' City x ASN lists, each its own.
func listSearches(slugs []string, n int) []string {
	out := []string{}
	for i := range n {
		out = append(out, fmt.Sprintf("/v1/networks/%s/coverage?range=%s&q=city+%d", slugs[i%len(slugs)], weekRanges[i%3], 100+i))
	}
	return out
}

// searches is n searches of the search palette, each its own.
func searches(n int) []string {
	out := []string{}
	for i := range n {
		out = append(out, fmt.Sprintf("/v1/search?q=net%02d+%d", i%testscale.Week.Networks, i))
	}
	return out
}

// checkPageTotals checks that each page's requests' p99 add up to at most
// pageTotal: the network page's for every network.
func checkPageTotals(t *testing.T, slugs []string, p99 map[string]time.Duration) {
	for i, slug := range slugs {
		for _, p := range weekPages(slug) {
			network := strings.HasSuffix(p.name, "/"+slug)
			if i > 0 && !network {
				continue
			}
			var sum time.Duration
			reqs := pageRequests(p)
			for _, u := range reqs {
				sum += p99[u]
			}
			t.Logf("page %-34s %2d requests, p99 sum %v", p.name, len(reqs), sum)
			if sum > pageTotal && os.Getenv("SITEAPI_SKIP_LATENCY") == "" {
				t.Errorf("page %s: its %d requests' p99 add up to %v, over the %v budget", p.name, len(reqs), sum, pageTotal)
			}
		}
	}
}

// meter times requests over one keep-alive client, asking for brotli.
type meter struct {
	t      *testing.T
	base   string
	client *http.Client
}

// get asks for path, fails the test unless it answers 200, and returns how
// long the whole answer took.
func (m *meter) get(path string) time.Duration {
	req, err := http.NewRequest(http.MethodGet, m.base+path, nil)
	if err != nil {
		m.t.Fatal(err)
	}
	req.Header.Set("Accept-Encoding", "br")
	start := time.Now()
	resp, err := m.client.Do(req)
	if err != nil {
		m.t.Fatal(err)
	}
	_, err = io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	took := time.Since(start)
	if err != nil || resp.StatusCode != http.StatusOK {
		m.t.Fatalf("%s: %d (%v)", path, resp.StatusCode, err)
	}
	return took
}

// timing is one timed request.
type timing struct {
	path string
	took time.Duration
}

// repeat asks for path warmups times, then times it timed times, and checks
// its p99 against b.
func (m *meter) repeat(path string, b pageBudget) time.Duration {
	all := make([]timing, 0, timed)
	for i := range warmups + timed {
		if took := m.get(path); i >= warmups {
			all = append(all, timing{path, took})
		}
	}
	got := m.check(b, all)
	m.t.Logf("%-56s p99 %v", path, got)
	return got
}

// distinct asks for each of warm, then times each of paths once, and checks
// their p99 against b; without warm, the first warmups paths warm up.
func (m *meter) distinct(b pageBudget, warm, paths []string) {
	if warm == nil {
		warm, paths = paths[:warmups], paths[warmups:]
	}
	for _, p := range warm {
		m.get(p)
	}
	all := make([]timing, 0, len(paths))
	for _, p := range paths {
		all = append(all, timing{p, m.get(p)})
	}
	got := m.check(b, all)
	m.t.Logf("%-36s %3d requests such as %s: p99 %v", b.name, len(paths), paths[0], got)
}

// check is the p99 of timings, an error naming its request when it is over
// b's budget.
func (m *meter) check(b pageBudget, all []timing) time.Duration {
	sort.Slice(all, func(i, j int) bool { return all[i].took < all[j].took })
	p := all[len(all)*99/100]
	if p.took > b.limit && os.Getenv("SITEAPI_SKIP_LATENCY") == "" {
		m.t.Errorf("%s: p99 %v, over the %v budget of %s", p.path, p.took, b.limit, b.name)
	}
	return p.took
}
