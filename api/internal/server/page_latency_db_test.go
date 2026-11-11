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
	"testing"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/bench"
	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/server"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
	"github.com/Zeus-Development/proxy-index/api/internal/testdb"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// The test's own limits; every request's and page's are bench.CIGuard, with
// bench.StagingBudgets as warnings.
const (
	rebuildLimit = 30 * time.Second // the full rebuild of testscale.Week
	testLimit    = 60 * time.Second // the whole test
	warmups      = 50               // requests before the timed ones
	timed        = 200              // timed requests of one repeated request
)

var weekRanges = []string{"24h", "7d", "30d"}

// smallProvider's network has smallNetwork's lists in TestPageLatency, the
// others testscale.Week's: its first q= searches, of the smallest lists, are
// timed among the others' (ROW-CACHE-1: the warm step left Proxy-cheap's
// small lists cold, and their first searches missed the budget).
var (
	smallProvider = fmt.Sprintf("net%02d", testscale.Week.Networks-1)
	smallNetwork  = testscale.WeekShape{NetworkShape: testscale.Week.NetworkShape, Regions: 20, Cities: 60, Asns: 30, CityAsns: 60}
)

// TestPageLatency times every request of every Proxy Index page (bench.Specs:
// docs/api-web-map.md section 10, after section 11, with every registered
// answer's samples) against testscale.Week as a v4 build, over loopback with
// keep-alive and brotli, within bench.CIGuard: first the cold ones, each asked
// once (a q= search of every network's City x ASN lists, the first requests
// after the build, whose rows the rebuild put in the row cache, every list's
// under the default --row-cache, smallProvider's smallest lists among them; a City x ASN
// list page's first render; and searches), then every page's requests, each
// repeated. The p99 of one page's
// requests add up to at most the guard's page budget. A measurement over the
// guard is taken once more (meter.within); one over the staging budget only
// is a STAGING-BUDGET warning.
func TestPageLatency(t *testing.T) {
	// Serial and alone: api.yml runs it in its own process after the other tests.
	start := time.Now()
	db := testdb.New(t)
	if testing.Short() || raceEnabled {
		t.Skip("measures latency")
	}
	week := testscale.Week
	s := weekServerWith(t, db, week, testdb.V4Lists{Requested: week.Requested, Rows: week.Lists(), Small: smallNetwork.Lists(),
		SmallProvider: smallProvider}, nil)
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()
	if c := health(t, ts.URL).ColdLists; c.Count != 0 {
		t.Errorf("the default row cache left %d lists cold (%d rows, the largest %+v), want every list warm", c.Count, c.Rows, c.Largest)
	}
	m := &meter{t: t, base: ts.URL, client: &http.Client{Transport: &http.Transport{MaxIdleConnsPerHost: 4, DisableCompression: true}}}
	guard, staging := bench.CIGuard(), bench.StagingBudgets()
	sh, slugs := weekShape(t, s)
	if !slices.Contains(slugs, smallProvider) {
		t.Fatalf("no network %s among %v", smallProvider, slugs)
	}
	search := limits{ms(guard.Classes[bench.ClassListSearch].ColdMs), ms(staging.Classes[bench.ClassListSearch].ColdMs)}
	m.within(bench.ClassListSearch+" first after the build", search, func(round int) timing {
		return m.distinct(bench.ClassListSearch+" first after the build", []string{}, firstSearches(slugs, round))
	})
	for _, class := range []string{bench.ClassList, bench.ClassListSearch, bench.ClassSearch} {
		l := limits{ms(guard.Classes[class].ColdMs), ms(staging.Classes[class].ColdMs)}
		m.within(class+" cold", l, func(round int) timing {
			warm, paths := coldPaths(class, slugs, round)
			return m.distinct(class+" cold", warm, paths)
		})
	}
	pages := bench.Expand(bench.Specs, sh, build.Answers())
	p99 := map[string]time.Duration{}
	for _, p := range pages {
		for _, u := range p.Requests {
			if _, done := p99[u]; !done {
				class := bench.Class(u)
				l := limits{ms(guard.Classes[class].P99Ms), ms(staging.Classes[class].P99Ms)}
				p99[u] = m.within(u+" ("+class+")", l, func(int) timing { return m.repeat(u) })
			}
		}
	}
	checkPageTotals(m, pages, p99, guard, staging)
	t.Logf("LATENCY-RATIO largest p99 to CI guard %.2f (%s), to staging budget %.2f (%s)",
		m.toGuard.of, m.toGuard.name, m.toStaging.of, m.toStaging.name)
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

// weekServer publishes week (testscale.Week's shape) as a v4 build with its
// browser targets and test events, and returns a server of its build, which
// must take at most rebuildLimit.
func weekServer(t *testing.T, db *testdb.DB, week testscale.WeekShape) *server.Server {
	return weekServerWith(t, db, week, testdb.V4Lists{Requested: week.Requested, Rows: week.Lists()}, nil)
}

// weekServerWith is weekServer with the networks as o has them and the row
// cache of lists (nil: the default).
func weekServerWith(t *testing.T, db *testdb.DB, week testscale.WeekShape, o testdb.V4Lists, lists *store.Lists) *server.Server {
	ctx := context.Background()
	start := time.Now()
	payload, err := week.Payload()
	if err != nil {
		t.Fatal(err)
	}
	db.Exec(t, week.TargetsSQL())
	db.PublishV4With(t, payload, o)
	db.Exec(t, week.EventsSQL())
	// As production's autovacuum leaves it (its settings in calculated/schema.sql).
	db.Exec(t, "VACUUM ANALYZE calculated_list_rows")
	t.Logf("published the week in %v", time.Since(start).Round(time.Millisecond))
	pool, err := store.OpenReadOnly(ctx, db.DSN)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	s := server.New(server.DBSource{Pool: pool, Lists: lists}, server.Options{Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), FilesDir: t.TempDir()})
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

// coldPaths are the warm-ups and the timed paths of class's cold measurement
// in round 0, or 1 for its retry, each round's timed paths never read before:
// page 2, then 3, of each list after the first network's pages warm up; for
// searches, the round's first warmups paths warm up.
func coldPaths(class string, slugs []string, round int) (warm, paths []string) {
	n := warmups + timed
	switch class {
	case bench.ClassList:
		return coveragePages(slugs[:1], 2, 18)[:warmups], coveragePages(slugs[1:], 2+round, 2+round)
	case bench.ClassListSearch:
		return nil, listSearches(slugs, round*n, n)
	}
	return nil, searches(round*n, n)
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

// listSearches is n searches of the networks' City x ASN lists from the
// from-th, each its own.
func listSearches(slugs []string, from, n int) []string {
	out := []string{}
	for i := from; i < from+n; i++ {
		out = append(out, fmt.Sprintf("/v1/networks/%s/coverage?range=%s&q=city+%d", slugs[i%len(slugs)], weekRanges[i%3], 100+i))
	}
	return out
}

// firstSearches is a q= search of each network's City x ASN list in every
// range, round's own words: in round 0, the first request of each list.
func firstSearches(slugs []string, round int) []string {
	out := []string{}
	for _, slug := range slugs {
		for _, r := range weekRanges {
			out = append(out, fmt.Sprintf("/v1/networks/%s/coverage?range=%s&q=city+%d", slug, r, 10+round))
		}
	}
	return out
}

// searches is n searches of the search palette from the from-th, each its own.
func searches(from, n int) []string {
	out := []string{}
	for i := from; i < from+n; i++ {
		out = append(out, fmt.Sprintf("/v1/search?q=net%02d+%d", i%testscale.Week.Networks, i))
	}
	return out
}

// checkPageTotals checks that each page's requests' p99 add up to at most
// the guard's page budget (the registered samples no page draws are not a
// page); a page's retry measures each of its requests again.
func checkPageTotals(m *meter, pages []bench.Page, p99 map[string]time.Duration, guard, staging bench.Budgets) {
	for _, p := range pages {
		if p.Name == bench.OtherAnswers {
			continue
		}
		n := len(p.Requests)
		l := limits{ms(guard.PageBudgetMs(n)), ms(staging.PageBudgetMs(n))}
		m.within("page "+p.Name, l, func(round int) timing {
			var sum time.Duration
			for _, u := range p.Requests {
				if round > 0 {
					p99[u] = m.repeat(u).took
				}
				sum += p99[u]
			}
			m.t.Logf("page %-46s %2d requests, p99 sum %v", p.Name, n, sum)
			return timing{"its requests' p99 sum", sum}
		})
	}
}

// meter times requests over one keep-alive client, asking for brotli.
type meter struct {
	t      *testing.T
	base   string
	client *http.Client
	// The largest kept p99 to its guard and to its staging budget.
	toGuard, toStaging ratio
}

// ratio is a p99 over its limit, and the measurement's name.
type ratio struct {
	of   float64
	name string
}

func (r *ratio) keep(name string, took, limit time.Duration) {
	if of := float64(took) / float64(limit); of > r.of {
		*r = ratio{of, name}
	}
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

// repeat asks for path warmups times, then times it timed times: its p99.
func (m *meter) repeat(path string) timing {
	all := make([]timing, 0, timed)
	for i := range warmups + timed {
		if took := m.get(path); i >= warmups {
			all = append(all, timing{path, took})
		}
	}
	got := nearestP99(all)
	m.t.Logf("%-72s p99 %v", path, got.took)
	return got
}

// distinct asks for each of warm, then times each of paths once: their p99.
// Without warm, the first warmups paths warm up.
func (m *meter) distinct(name string, warm, paths []string) timing {
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
	got := nearestP99(all)
	m.t.Logf("%-28s %3d requests such as %s: p99 %v", name, len(paths), paths[0], got.took)
	return got
}

// nearestP99 is the p99 of all, with its request.
func nearestP99(all []timing) timing {
	sort.Slice(all, func(i, j int) bool { return all[i].took < all[j].took })
	return all[len(all)*99/100]
}

// within measures name once (round 0) and, over its CI guard, once more the
// same way (round 1), and returns the p99 it keeps, the last. It fails only
// when both are over the guard, and logs a STAGING-BUDGET warning when the
// last is within the guard but over the staging budget.
func (m *meter) within(name string, l limits, measure func(round int) timing) time.Duration {
	first := measure(0)
	last := first
	if first.took > l.guard {
		m.t.Logf("RETRY %s: p99 %v (%s), over its %v CI guard; measuring once more", name, first.took, first.path, l.guard)
		last = measure(1)
	}
	m.toGuard.keep(name, last.took, l.guard)
	m.toStaging.keep(name, last.took, l.staging)
	fail, warn := verdict(name, l, first, last)
	if warn != "" {
		m.t.Log(warn)
	}
	if fail != "" && os.Getenv("SITEAPI_SKIP_LATENCY") == "" {
		m.t.Error(fail)
	}
	return last.took
}
