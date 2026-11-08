package server_test

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"sort"
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/bench"
	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/server"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// The test's own limits; every request's and page's are bench.CIBudgets.
const (
	rebuildLimit = 30 * time.Second // the full rebuild of testscale.Week
	testLimit    = 60 * time.Second // the whole test
	warmups      = 50               // requests before the timed ones
	timed        = 200              // timed requests of one repeated request
)

var weekRanges = []string{"24h", "7d", "30d"}

// TestPageLatency times every request of every Proxy Index page (bench.Specs:
// docs/api-web-map.md section 10, after section 11, with every registered
// answer's samples) against testscale.Week as a v4 build, over loopback with
// keep-alive and brotli, within bench.CIBudgets: first the cold ones, each asked
// once (a City x ASN list page's first render, its rows not read yet, and
// searches), then every page's requests, each repeated. The p99 of one page's
// requests add up to at most the budgets' PageMs.
func TestPageLatency(t *testing.T) {
	// Serial and alone: api.yml runs it in its own process after the other tests.
	start := time.Now()
	db := testdb.New(t)
	if testing.Short() || raceEnabled {
		t.Skip("measures latency")
	}
	s := weekServer(t, db)
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()
	m := &meter{t: t, base: ts.URL, client: &http.Client{Transport: &http.Transport{MaxIdleConnsPerHost: 4, DisableCompression: true}}}
	budgets := bench.CIBudgets()
	sh, slugs := weekShape(t, s)
	for _, c := range []struct {
		class       string
		warm, paths []string
	}{
		// Page 2 of each list, its rows not read yet (the first network's pages warm up).
		{bench.ClassList, coveragePages(slugs[:1], 2, 18)[:warmups], coveragePages(slugs[1:], 2, 2)},
		{bench.ClassListSearch, nil, listSearches(slugs, warmups+timed)},
		{bench.ClassSearch, nil, searches(warmups + timed)},
	} {
		m.distinct(c.class+" cold", ms(budgets.Classes[c.class].ColdMs), c.warm, c.paths)
	}
	pages := bench.Expand(bench.Specs, sh, build.Answers())
	p99 := map[string]time.Duration{}
	for _, p := range pages {
		for _, u := range p.Requests {
			if _, done := p99[u]; !done {
				class := bench.Class(u)
				p99[u] = m.repeat(u, class, ms(budgets.Classes[class].P99Ms))
			}
		}
	}
	checkPageTotals(t, pages, p99, ms(budgets.PageMs))
	if took := time.Since(start); took > testLimit {
		t.Errorf("the test took %v, over its %v budget", took, testLimit)
	}
}

// weekShape is the bench shape of the server's build and its network slugs:
// a list search asks for a country code, a palette search for a network.
func weekShape(t *testing.T, s *server.Server) (bench.Shape, []string) {
	sh := bench.Shape{Query: "aa", Search: "net00"}
	slugs := []string{}
	for _, n := range s.Current().Result.Pages.Networks.Networks {
		sh.Networks = append(sh.Networks, bench.Network{Slug: n.Slug, Kinds: n.Kinds})
		slugs = append(slugs, n.Slug)
	}
	if len(slugs) != testscale.Week.Networks {
		t.Fatalf("%d networks, want %d", len(slugs), testscale.Week.Networks)
	}
	return sh, slugs
}

func ms(v float64) time.Duration { return time.Duration(v * float64(time.Millisecond)) }

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
// limit (the registered samples no page draws are not a page).
func checkPageTotals(t *testing.T, pages []bench.Page, p99 map[string]time.Duration, limit time.Duration) {
	for _, p := range pages {
		if p.Name == bench.OtherAnswers {
			continue
		}
		var sum time.Duration
		for _, u := range p.Requests {
			sum += p99[u]
		}
		t.Logf("page %-46s %2d requests, p99 sum %v", p.Name, len(p.Requests), sum)
		if sum > limit && os.Getenv("SITEAPI_SKIP_LATENCY") == "" {
			t.Errorf("page %s: its %d requests' p99 add up to %v, over the %v budget", p.Name, len(p.Requests), sum, limit)
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
// its p99 against the limit of its budget class.
func (m *meter) repeat(path, class string, limit time.Duration) time.Duration {
	all := make([]timing, 0, timed)
	for i := range warmups + timed {
		if took := m.get(path); i >= warmups {
			all = append(all, timing{path, took})
		}
	}
	got := m.check(class, limit, all)
	m.t.Logf("%-72s p99 %v", path, got)
	return got
}

// distinct asks for each of warm, then times each of paths once, and checks
// their p99 against limit; without warm, the first warmups paths warm up.
func (m *meter) distinct(name string, limit time.Duration, warm, paths []string) {
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
	got := m.check(name, limit, all)
	m.t.Logf("%-28s %3d requests such as %s: p99 %v", name, len(paths), paths[0], got)
}

// check is the p99 of timings, an error naming its request when it is over
// limit, the budget of class.
func (m *meter) check(class string, limit time.Duration, all []timing) time.Duration {
	sort.Slice(all, func(i, j int) bool { return all[i].took < all[j].took })
	p := all[len(all)*99/100]
	if p.took > limit && os.Getenv("SITEAPI_SKIP_LATENCY") == "" {
		m.t.Errorf("%s: p99 %v, over its %v budget (%s)", p.path, p.took, limit, class)
	}
	return p.took
}
