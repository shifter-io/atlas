package build_test

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
)

// The v4 dataset file holds every network list's first page, its exact
// total and its page count (contract K11 as DATASET-SIZE-2 changed it): for
// each network kind, range and place dimension, and each Coverage list, the
// first page regenerated from the file alone is the served one byte for
// byte, and the API serves every further page: the rows of every page add up
// to the file's total. The expected answers are the build's (what the server
// renders), asked for by the build's keys.
func TestTheDatasetFileHoldsEveryListsFirstPage(t *testing.T) {
	t.Parallel()
	file, res := v4File(t)
	at, lists := readLists(t, file)
	pg := res.Pages
	byScope := map[string]fileNetwork{}
	for _, n := range lists {
		byScope[n.slug+"/"+n.kind] = n
	}
	checked, long := 0, 0
	for scope := range pg.Network {
		if scope == build.AllNetworks+"/"+build.AllNetworks {
			continue
		}
		// A network kind without a published group has no lists in the
		// file: every list it serves is empty.
		n, ok := byScope[scope]
		if !ok {
			n.slug, n.kind, _ = strings.Cut(scope, "/")
		}
		for _, r := range build.Ranges {
			for _, dim := range []string{"country", "region", "city", "asn"} {
				l := n.lists[r.ID][dim]
				served, ok, _ := pg.List(context.Background(), at, scope, r.ID, dim, "", 1)
				sameBody(t, scope+" "+r.ID+" "+dim, served, ok, l.firstPage(at, scope, r.ID, dim))
				long += everyPage(t, scope+" "+r.ID+" "+dim, l, func(p int) int {
					page, _, _ := pg.List(context.Background(), at, scope, r.ID, dim, "", p)
					return len(page.Rows)
				})
			}
			long += checkCoverage(t, pg, at, n, r.ID)
			checked += 5
		}
	}
	if len(byScope) < 3 || long == 0 {
		t.Fatalf("fixture too small: %d network kinds in the file, %d lists of more than a page", len(byScope), long)
	}
	t.Logf("%d first pages regenerated from the file, %d lists of more than a page served whole", checked, long)
}

// checkCoverage compares a network kind's Coverage first page of a range
// with the file's, then reads its further pages; it returns 1 for a list of
// more than a page.
func checkCoverage(t *testing.T, pg *build.Pages, at string, n fileNetwork, rangeID string) int {
	t.Helper()
	scope, l := n.slug+"/"+n.kind, n.lists[rangeID]["city_asn"]
	served, ok, _ := pg.Coverage(context.Background(), at, scope, rangeID, "", 1)
	if !ok {
		if l.total != 0 || len(l.coverage) != 0 {
			t.Errorf("%s %s: no Coverage answer, but the file has %d rows", scope, rangeID, l.total)
		}
		return 0
	}
	want := &build.CoveragePage{GeneratedAt: at, Network: n.slug, Kind: n.kind, Range: rangeID, Page: 1, PageSize: build.PageSize,
		Total: l.total, Pages: l.pages, Rows: append([]build.CoverageRow{}, l.coverage...)}
	sameBody(t, scope+" "+rangeID+" coverage", served, ok, want)
	return everyPage(t, scope+" "+rangeID+" coverage", l, func(p int) int {
		page, _, _ := pg.Coverage(context.Background(), at, scope, rangeID, "", p)
		return len(page.Rows)
	})
}

// everyPage checks that the file's page count is its total's and that the
// served pages after the first hold the rest of its rows (rows(p) is page
// p's row count); it returns 1 for a list of more than a page.
func everyPage(t *testing.T, what string, l fileList, rows func(int) int) int {
	t.Helper()
	first := max(len(l.rows), len(l.coverage))
	if l.pages != (l.total+build.PageSize-1)/build.PageSize || first != min(l.total, build.PageSize) {
		t.Errorf("%s: total %d, %d pages, %d rows in the file", what, l.total, l.pages, first)
	}
	n := first
	for p := 2; p <= l.pages; p++ {
		n += rows(p)
	}
	if n != l.total {
		t.Errorf("%s: the served pages hold %d rows, the file's total is %d", what, n, l.total)
	}
	if l.pages > 1 {
		return 1
	}
	return 0
}

// The pages and lists hold nothing the pages do not show (regression case
// 2): the file's top-level keys, a v4 provider's keys (no coverageEvidence),
// every network kind's ranges and dimensions, and each dimension's row
// length and field types.
func TestTheDatasetFileHoldsOnlyWhatThePagesShow(t *testing.T) {
	t.Parallel()
	file, _ := v4File(t)
	var keys []string
	for _, kv := range pairs(t, file) {
		keys = append(keys, kv.key)
		if kv.key == "providers" {
			checkProviderKeys(t, kv.raw)
		}
		if kv.key == "lists" {
			checkListShapes(t, kv.raw)
		}
	}
	want := "schemaVersion generatedAt seed simulated runId collectorVersion windowHours targets scopes weights profiles totals " +
		"measuredTotals measuredMarket providers tape changelog reviews communityRuns pages lists snapshot na"
	if got := strings.Join(keys, " "); got != want {
		t.Errorf("top-level keys:\n got %s\nwant %s", got, want)
	}
}

// providerKeys are the keys a v4 provider may have.
var providerKeys = strings.Fields("source slug name tagline kinds primaryKind hq founded claim verdict strengths weaknesses bestFor " +
	"website products measuredProduct productMeasurements score pool performance pricing trust targets series ipSamples incidents " +
	"geo asns overlap reviews countryLatencyRanks tileRanks framework na")

func checkProviderKeys(t *testing.T, raw []byte) {
	t.Helper()
	allowed := map[string]bool{}
	for _, k := range providerKeys {
		allowed[k] = true
	}
	var providers []map[string]any
	if err := json.Unmarshal(raw, &providers); err != nil {
		t.Fatal(err)
	}
	for _, p := range providers {
		for k := range p {
			if !allowed[k] {
				t.Errorf("provider %v has %q, which no page shows", p["slug"], k)
			}
		}
	}
}

// rowTypes are each dimension's fields: s a string or null, n a number or
// null, a an array (a place row's success score) (FIELDS.md, lists).
var rowTypes = map[string]string{"country": "ssnnnna", "region": "ssssnnnna", "city": "sssssnnnna", "asn": "nsnnnna", "city_asn": "ssssnssnnnn"}

func checkListShapes(t *testing.T, raw []byte) {
	t.Helper()
	rows := 0
	for _, slug := range pairs(t, raw) {
		for _, kind := range pairs(t, slug.raw) {
			var ranges []string
			for _, rng := range pairs(t, kind.raw) {
				ranges = append(ranges, rng.key)
				var dims []string
				for _, dim := range pairs(t, rng.raw) {
					dims = append(dims, dim.key)
					rows += checkList(t, slug.key+" "+kind.key+" "+rng.key+" "+dim.key, rowTypes[dim.key], dim.raw)
				}
				if got := strings.Join(dims, " "); got != "country region city asn city_asn" {
					t.Errorf("%s %s %s: dimensions %s", slug.key, kind.key, rng.key, got)
				}
			}
			if got := strings.Join(ranges, " "); got != "24h 7d 30d" {
				t.Errorf("%s %s: ranges %s", slug.key, kind.key, got)
			}
		}
	}
	if rows == 0 {
		t.Fatal("no list rows")
	}
}

// checkList checks a list's keys (total, pages, rows), that it holds at most
// a page of rows, and each row's field types; it returns its row count.
func checkList(t *testing.T, what, types string, raw []byte) int {
	t.Helper()
	var keys []string
	var rows []byte
	for _, kv := range pairs(t, raw) {
		keys = append(keys, kv.key)
		if kv.key == "rows" {
			rows = kv.raw
		}
	}
	if got := strings.Join(keys, " "); got != "total pages rows" {
		t.Errorf("%s: keys %s", what, got)
	}
	return checkRowTypes(t, what, types, rows)
}

func checkRowTypes(t *testing.T, what, types string, raw []byte) int {
	t.Helper()
	var rows [][]any
	if err := json.Unmarshal(raw, &rows); err != nil {
		t.Fatal(err)
	}
	if len(rows) > build.PageSize {
		t.Errorf("%s: %d rows, more than a page", what, len(rows))
	}
	for i, r := range rows {
		got := make([]byte, len(r))
		for j, v := range r {
			switch v.(type) {
			case string:
				got[j] = 's'
			case float64:
				got[j] = 'n'
			case []any:
				got[j] = 'a'
			case nil:
				got[j] = types[min(j, len(types)-1)]
			default:
				got[j] = '?'
			}
		}
		if string(got) != types {
			t.Errorf("%s row %d: fields %s, want %s", what, i, got, types)
			return len(rows)
		}
	}
	return len(rows)
}
