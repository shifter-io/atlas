package build_test

import (
	"context"
	"encoding/json"
	"math"
	"strconv"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/build"
	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

// The v4 dataset file alone regenerates every list answer and place page
// (contract K11, regression case 1 of API-DATASET-2): each network's places
// in every dimension, the all-networks places, each country's cities and
// regions (first page, last page and a recorded query), each network's
// Coverage (every page without a query, and three recorded queries with their
// totals) and every region and city page, with its ranks recomputed here by
// counting better peers. The expected answers are the build's (what the
// server renders), asked for by the build's keys.
func TestTheDatasetFileRegeneratesEveryListAnswer(t *testing.T) {
	t.Parallel()
	file, res := v4File(t)
	at, lists := readLists(t, file)
	pg := res.Pages
	if len(pg.Network) < 4 || len(pg.Country) < 2 {
		t.Fatalf("fixture too small: %d network answers, %d countries", len(pg.Network), len(pg.Country))
	}
	byScope := map[string]fileNetwork{}
	for _, n := range lists {
		byScope[n.slug+"/"+n.kind] = n
	}
	checked := 0
	for scope := range pg.Network {
		for _, r := range build.Ranges {
			for dim, q := range listQueries {
				var rows []build.PlaceRow
				if scope == build.AllNetworks+"/"+build.AllNetworks {
					var all []fileRow
					for _, n := range lists {
						all = append(all, n.rows[r.ID][dim]...)
					}
					rows = ordered(merged(dim, all, nil))
				} else {
					rows = byScope[scope].networkRows(r.ID, dim, nil)
				}
				checked += checkList(t, pg, at, scope, r.ID, dim, q, rows)
			}
		}
	}
	for cc := range pg.Country {
		in := func(r fileRow) bool { return strings.ToUpper(strings.TrimSpace(model.SV(r.cc))) == cc }
		for _, r := range build.Ranges {
			for _, dim := range []string{"city", "region"} {
				checked += checkList(t, pg, at, "country/"+cc, r.ID, dim, listQueries[dim], acrossNetworks(lists, r.ID, dim, in))
			}
		}
	}
	checked += checkCoverage(t, pg, at, byScope) + checkPlacePages(t, pg, at, lists)
	t.Logf("%d answers regenerated from the file", checked)
}

// checkList compares one list's first and last pages and its first page
// with query q; it returns how many answers it compared.
func checkList(t *testing.T, pg *build.Pages, at, scope, rangeID, dim, q string, rows []build.PlaceRow) int {
	t.Helper()
	n := 0
	for _, p := range pagesOf(len(rows)) {
		served, ok, _ := pg.List(context.Background(), at, scope, rangeID, dim, "", p)
		sameBody(t, scope+" "+rangeID+" "+dim+" page "+strconv.Itoa(p), served, ok, page(at, scope, rangeID, dim, "", p, rows))
		n++
	}
	served, ok, _ := pg.List(context.Background(), at, scope, rangeID, dim, q, 1)
	sameBody(t, scope+" "+rangeID+" "+dim+" q="+q, served, ok, page(at, scope, rangeID, dim, q, 1, rows))
	return n + 1
}

// coverageQueries are the recorded Coverage queries.
var coverageQueries = []string{"city 1", "AS64513", "aa region"}

// checkCoverage compares every Coverage page without a query and the first
// page of each recorded query, for every network kind the build has a
// Coverage answer for.
func checkCoverage(t *testing.T, pg *build.Pages, at string, byScope map[string]fileNetwork) int {
	t.Helper()
	n := 0
	for scope := range pg.Network {
		slug, kind, _ := strings.Cut(scope, "/")
		nw := byScope[scope]
		nw.slug, nw.kind = slug, kind
		for _, r := range build.Ranges {
			first, ok, _ := pg.Coverage(context.Background(), at, scope, r.ID, "", 1)
			if !ok {
				continue
			}
			for p := 1; p <= max(first.Pages, 1); p++ {
				served, ok, _ := pg.Coverage(context.Background(), at, scope, r.ID, "", p)
				sameBody(t, scope+" "+r.ID+" coverage page "+strconv.Itoa(p), served, ok, coveragePage(at, nw, r.ID, "", p))
				n++
			}
			for _, q := range coverageQueries {
				served, ok, _ := pg.Coverage(context.Background(), at, scope, r.ID, q, 1)
				sameBody(t, scope+" "+r.ID+" coverage q="+q, served, ok, coveragePage(at, nw, r.ID, q, 1))
				n++
			}
		}
	}
	if n == 0 {
		t.Fatal("the build has no Coverage answer")
	}
	return n
}

// coveragePage is a Coverage page: the rows (already most IPs first) that
// hold every word of q in their place or "as<n>" and ASN name, and with a
// query their totals.
func coveragePage(at string, nw fileNetwork, rangeID, q string, p int) *build.CoveragePage {
	words := strings.Fields(strings.ToLower(q))
	out := &build.CoveragePage{GeneratedAt: at, Network: nw.slug, Kind: nw.kind, Range: rangeID, Query: strings.Join(words, " "), Page: p,
		PageSize: build.PageSize, Rows: []build.CoverageRow{}}
	var rows []build.CoverageRow
	for _, r := range nw.coverage[rangeID] {
		c := r.CoverageRow
		place := strings.ToLower(strings.Join([]string{model.SV(c.CountryCode), model.SV(c.Country), model.SV(c.Region), model.SV(c.City)}, " "))
		network := strings.ToLower(model.SV(c.AsnName))
		if c.Asn.OK() {
			network += " as" + strconv.FormatInt(int64(c.Asn), 10)
		}
		if holdsAll(q, place, network) {
			rows = append(rows, r)
		}
	}
	out.Total, out.Pages = len(rows), (len(rows)+build.PageSize-1)/build.PageSize
	if len(words) > 0 {
		countries, cities, asns := map[string]bool{}, map[[4]string]bool{}, map[model.Num]bool{}
		out.Totals = &build.CoverageTotals{}
		for _, r := range rows {
			c := r.CoverageRow
			out.Totals.Ips += c.Ips
			if c.CountryCode != nil {
				countries[*c.CountryCode] = true
			}
			if c.City != nil {
				cities[[4]string{model.SV(c.CountryCode), model.SV(c.Country), model.SV(c.Region), *c.City}] = true
			}
			if c.Asn.OK() {
				asns[c.Asn] = true
			}
		}
		out.Totals.Countries, out.Totals.Cities, out.Totals.Asns = len(countries), len(cities), len(asns)
	}
	if start := (p - 1) * build.PageSize; start < len(rows) {
		out.Rows = rows[start:min(len(rows), start+build.PageSize)]
	}
	return out
}

// checkPlacePages compares every region and city page: the keys are those
// of the build's all-networks lists.
func checkPlacePages(t *testing.T, pg *build.Pages, at string, lists []fileNetwork) int {
	t.Helper()
	n := 0
	for _, r := range build.Ranges {
		for _, dim := range []string{"region", "city"} {
			for key := range placeKeys(pg, at, r.ID, dim) {
				cc, _, _ := strings.Cut(key, "|")
				in := func(f fileRow) bool { return strings.ToUpper(strings.TrimSpace(model.SV(f.cc))) == cc }
				want := &build.PlaceAnswer{GeneratedAt: at, Dimension: dim, Key: key, Range: r.ID, Rows: []build.PlaceRow{}}
				for _, row := range acrossNetworks(lists, r.ID, dim, in) {
					if row.Key == key {
						want.Rows = append(want.Rows, row)
					}
				}
				ranked(want.Rows)
				served, ok, _ := pg.Place(context.Background(), at, dim, key, r.ID)
				sameBody(t, dim+" page "+key+" "+r.ID, served, ok, want)
				n++
			}
		}
	}
	if n == 0 {
		t.Fatal("the build has no region or city page")
	}
	return n
}

// placeKeys is every key of the build's all-networks list of a range and
// dimension, every page read.
func placeKeys(pg *build.Pages, at, rangeID, dim string) map[string]bool {
	keys := map[string]bool{}
	for p := 1; ; p++ {
		list, ok, _ := pg.List(context.Background(), at, build.AllNetworks+"/"+build.AllNetworks, rangeID, dim, "", p)
		if !ok || len(list.Rows) == 0 {
			return keys
		}
		for _, row := range list.Rows {
			keys[row.Key] = true
		}
	}
}

// ranked ranks each row among the rows of its kind: IPs (more is better,
// among rows with IPs) and latency (lower is better, among rows with one),
// rank 1 plus the peers strictly better.
func ranked(rows []build.PlaceRow) {
	for i := range rows {
		ips, lat := model.Rank{Rank: model.Num(math.NaN())}, model.Rank{Rank: model.Num(math.NaN())}
		mine := rows[i]
		for _, peer := range rows {
			if peer.Kind != mine.Kind {
				continue
			}
			if peer.IpsSum.OK() && peer.IpsSum > 0 {
				ips.Peers++
			}
			if peer.LatencyP50.OK() {
				lat.Peers++
			}
		}
		if mine.IpsSum.OK() && mine.IpsSum > 0 {
			ips.Rank = 1 + better(rows, mine, func(p build.PlaceRow) bool { return p.IpsSum.OK() && p.IpsSum > mine.IpsSum })
		}
		if mine.LatencyP50.OK() {
			lat.Rank = 1 + better(rows, mine, func(p build.PlaceRow) bool { return p.LatencyP50.OK() && p.LatencyP50 < mine.LatencyP50 })
		}
		rows[i].IpsSumRank, rows[i].LatencyRank = &ips, &lat
	}
}

func better(rows []build.PlaceRow, mine build.PlaceRow, is func(build.PlaceRow) bool) model.Num {
	n := 0
	for _, p := range rows {
		if p.Kind == mine.Kind && is(p) {
			n++
		}
	}
	return model.Num(n)
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
// null (FIELDS.md, lists).
var rowTypes = map[string]string{"country": "ssnnnn", "region": "ssssnnnn", "city": "sssssnnnn", "asn": "nsnnnn", "city_asn": "ssssnssnnnn"}

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
					rows += checkRowTypes(t, slug.key+" "+kind.key+" "+rng.key+" "+dim.key, rowTypes[dim.key], dim.raw)
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

func checkRowTypes(t *testing.T, what, types string, raw []byte) int {
	t.Helper()
	var rows [][]any
	if err := json.Unmarshal(raw, &rows); err != nil {
		t.Fatal(err)
	}
	for i, r := range rows {
		got := make([]byte, len(r))
		for j, v := range r {
			switch v.(type) {
			case string:
				got[j] = 's'
			case float64:
				got[j] = 'n'
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
