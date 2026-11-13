package server_test

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/bench"
	"github.com/Zeus-Development/atlas/api/internal/testdb"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// The first q= search of a 150,000-row City x ASN list after a build, and of
// the network's place lists, reads no row from the database: the rebuild put
// its lists' rows in the row cache before it served (COVERAGE-SEARCH-1). The
// rows are deleted after the build, so a read would fail the answer. Each
// search, the first request of its list, answers within the list-search cold
// budget. A first search that matches every row (q=city: each row's city is
// "City <n>"), as Proxy-cheap's q=us matched much of its list, answers within
// the staging budget itself, 50 ms, measured once more when over it
// (COVERAGE-SEARCH-2). Serial and alone: api.yml runs it with TestPageLatency.
func TestFirstCoverageSearchAfterABuild(t *testing.T) {
	db := testdb.New(t)
	if testing.Short() {
		t.Skip("publishes a 150,000-row list")
	}
	shape := testscale.Week
	shape.Networks, shape.CityAsns, shape.Events = 1, 150_000, 10
	s := weekServer(t, db, shape)
	db.Exec(t, `DELETE FROM calculated_list_rows`)
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()
	m := &meter{t: t, base: ts.URL, client: &http.Client{Transport: &http.Transport{DisableCompression: true}}}
	budget := ms(bench.CIGuard().Classes[bench.ClassListSearch].ColdMs)
	slug := s.Current().Result.Pages.Networks.Networks[0].Slug
	for _, r := range weekRanges {
		for _, path := range []string{fmt.Sprintf("/v1/networks/%s/coverage?range=%s&q=city+12", slug, r),
			fmt.Sprintf("/v1/networks/%s/places?range=%s&dimension=city&q=city+12", slug, r)} {
			took := m.get(path)
			t.Logf("%-72s %v", path, took)
			if took > budget && !raceEnabled {
				t.Errorf("%s: %v, over the %v list-search cold budget", path, took, budget)
			}
		}
	}
	broad := ms(bench.StagingBudgets().Classes[bench.ClassListSearch].ColdMs)
	for _, r := range weekRanges {
		if raceEnabled {
			break
		}
		// Over the budget, once more with a word of its own (cit: every row too).
		m.within("first search of every row "+r, limits{broad, broad}, func(round int) timing {
			path := fmt.Sprintf("/v1/networks/%s/coverage?range=%s&q=%s", slug, r, []string{"city", "cit"}[round])
			took := m.get(path)
			t.Logf("%-72s %v", path, took)
			return timing{path, took}
		})
	}
}
