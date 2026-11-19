package build

import (
	"context"
	"encoding/json"
	"math/rand/v2"
	"strconv"
	"strings"
	"testing"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
	"github.com/shifter-io/atlas/api/internal/store"
	"github.com/shifter-io/atlas/api/internal/testscale"
)

// smallNetBuild is a v4 build of three networks, each one summary over three
// requested countries (criteria.groups 3), its lists holding three exit
// countries, 60 region, city and ASN rows and 120 City x ASN rows a window.
func smallNetBuild(t *testing.T) (*Result, *store.Inputs) {
	t.Helper()
	in, err := testscale.NetworkInputs(testscale.NetworkShape{Shape: testscale.Shape{Networks: 3, Countries: 1, Exits: 3, Tail: 60,
		CityAsn: 120}, Requested: 3})
	if err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	return res, in
}

// Case 7 (API-SUM-1 review N3): a network summarizing three groups answers
// the summary's groups, requested countries and totals, and its provider
// lists every ASN with IPs, more than 50.
func TestAV4NetworkAnswerIsItsSummary(t *testing.T) {
	t.Parallel()
	res, in := smallNetBuild(t)
	ans := res.Pages.Network["net00/residential"]
	if ans == nil || ans.ProductGroups != 3 || strings.Join(ans.RequestedCountries, ",") != "AA,AB,AC" {
		t.Fatalf("answer %+v", ans)
	}
	var doc snap.Snapshot
	if err := json.Unmarshal(in.Snapshot.Full(), &doc); err != nil {
		t.Fatal(err)
	}
	g := &doc.Products[0]
	w := g.Window("30d")
	var p *model.Provider
	for _, q := range res.Dataset.Providers {
		if q.Slug == "net00" {
			p = q
		}
	}
	if p == nil || g.Provider != p.Framework.Provider || ans.Ranges["30d"].Requests != w.Metrics.Completed || ans.Ranges["30d"].UniqueIps != ipsOf(w) {
		t.Errorf("30d totals %+v, summary completed %v", ans.Ranges["30d"], w.Metrics.Completed)
	}
	l := netListOf(in.Snapshot, g, w)
	withIps := 0
	for i := range l.Len("asn") {
		if dim(t, l, "asn").Row(i).Ips > 0 {
			withIps++
		}
	}
	if withIps <= 50 || len(p.Asns) != withIps || p.Asns[0].Ips < p.Asns[withIps-1].Ips {
		t.Errorf("provider asns: %d, list has %d with IPs", len(p.Asns), withIps)
	}
	if res.Pages.Network[AllNetworks+"/"+AllNetworks].ProductGroups != 9 {
		t.Errorf("all networks: %d product groups, want 9", res.Pages.Network[AllNetworks+"/"+AllNetworks].ProductGroups)
	}
}

// coverageText is a City x ASN row's searchable text (contract K11),
// independent of the store's search.
func coverageText(r store.NetRow) string {
	parts := []string{}
	if p := r.Place; p != nil {
		parts = append(parts, model.SV(p.CountryCode), model.SV(p.Region), model.SV(p.City)) // no country name (PLACE-CODES-1)
	}
	if n := r.Network; n != nil {
		if n.Asn != nil {
			parts = append(parts, "as"+strconv.FormatInt(*n.Asn, 10))
		}
		parts = append(parts, model.SV(n.Name))
	}
	return strings.ToLower(strings.Join(parts, " "))
}

// Case 2: the Coverage answer pages every row with IPs, most first, and a
// search's total and totals equal a brute-force pass (seed recorded).
func TestTheCoverageAnswer(t *testing.T) {
	t.Parallel()
	res, _ := smallNetBuild(t)
	pg, scope := res.Pages, "net01/residential"
	c := pg.coverage[listKey{scope, "30d", "coverage"}]
	var order []int32
	for _, i := range dim(t, c.list, "city_asn").ByIps() {
		if dim(t, c.list, "city_asn").Row(int(i)).Ips > 0 {
			order = append(order, i)
		}
	}
	first, ok, _ := pg.Coverage(context.Background(), "", scope, "30d", ListFilter{}, 1)
	second, _, _ := pg.Coverage(context.Background(), "", scope, "30d", ListFilter{}, 2)
	if !ok || first.Total != len(order) || len(order) <= PageSize || first.Totals != nil || len(first.Rows) != PageSize ||
		first.Network != "net01" || len(second.Rows) == 0 {
		t.Fatalf("total %d of %d rows (%+v)", first.Total, len(order), first)
	}
	if !same(second.Rows[0], c.row(dim(t, c.list, "city_asn"), order[PageSize])) {
		t.Errorf("page 2 starts %+v, want row 51 %+v", second.Rows[0], c.row(dim(t, c.list, "city_asn"), order[PageSize]))
	}
	var ranks map[string]model.CountryLatencyRank
	for _, p := range res.Dataset.Providers {
		if p.Slug == "net01" {
			ranks = p.CountryLatencyRanks["residential"]["30d"]
		}
	}
	if len(ranks) == 0 {
		t.Fatal("net01 has no country latency ranks")
	}
	for _, r := range first.Rows {
		want, ok := ranks[model.SV(r.CountryCode)]
		if !ok {
			want.Rank = nan // the country has no rank: its rows show null and 0 peers
		}
		if r.Rank != want.Rank && (r.Rank.OK() || want.Rank.OK()) || r.Peers != want.Peers {
			t.Fatalf("row %+v, country rank %+v", r, want)
		}
	}
	seed := uint64(20261105)
	rng := rand.New(rand.NewPCG(seed, 1))
	for range 200 {
		words := strings.Fields(coverageText(dim(t, c.list, "city_asn").Row(int(order[rng.IntN(len(order))]))))
		q := strings.Join([]string{words[rng.IntN(len(words))], words[rng.IntN(len(words))]}, " ")
		want, ips, countries, cities, asns := 0, Num(0), map[string]bool{}, map[*store.Place]bool{}, map[int64]bool{}
		var firstRow *CoverageRow
		for _, i := range order {
			r := dim(t, c.list, "city_asn").Row(int(i))
			text := coverageText(r)
			if !strings.Contains(text, strings.Fields(q)[0]) || !strings.Contains(text, strings.Fields(q)[1]) {
				continue
			}
			if want++; firstRow == nil {
				row := c.row(dim(t, c.list, "city_asn"), i)
				firstRow = &row
			}
			ips += c.row(dim(t, c.list, "city_asn"), i).Ips
			if r.Place != nil && r.Place.CountryCode != nil {
				countries[*r.Place.CountryCode] = true
			}
			if r.Place != nil && r.Place.City != nil {
				cities[r.Place] = true
			}
			if r.Network != nil && r.Network.Asn != nil {
				asns[*r.Network.Asn] = true
			}
		}
		got, _, _ := pg.Coverage(context.Background(), "", scope, "30d", ListFilter{Query: q}, 1)
		if got.Total != want || got.Totals == nil || got.Totals.Ips != ips || got.Totals.Countries != len(countries) ||
			got.Totals.Cities != len(cities) || got.Totals.Asns != len(asns) || want > 0 && !same(got.Rows[0], *firstRow) {
			t.Fatalf("seed %d, q %q: total %d (want %d), totals %+v (want %v IPs, %d countries, %d cities, %d ASNs)",
				seed, q, got.Total, want, got.Totals, ips, len(countries), len(cities), len(asns))
		}
	}
	if _, ok, _ := pg.Coverage(context.Background(), "", "nobody/residential", "30d", ListFilter{}, 1); ok {
		t.Error("an unknown network has a Coverage answer")
	}
}

// same reports whether two Coverage rows are written alike.
func same(a, b CoverageRow) bool {
	x, errA := a.MarshalJSON()
	y, errB := b.MarshalJSON()
	return errA == nil && errB == nil && string(x) == string(y)
}

// A Coverage row's IPs follow K8 and a row without IPs is left out.
func TestCoverageRowsScaleAndSkipEmptyRows(t *testing.T) {
	t.Parallel()
	berlin := &store.Place{CountryCode: ptr("DE"), Country: ptr("Germany"), Region: ptr("Berlin"), City: ptr("Berlin")}
	asn := int64(3320)
	l, err := store.NewNetList(map[string][]store.NetRow{"city_asn": {
		{Place: berlin, Network: &store.Network{Asn: &asn, Name: ptr("Telekom"), UsageType: ptr("isp")}, Ips: 3, P50: 120},
		{Place: berlin, Network: &store.Network{Name: ptr("Other")}, Ips: 0, P50: -1},
		{Place: berlin, Ips: -1, P50: -1}}})
	if err != nil {
		t.Fatal(err)
	}
	pg := &Pages{coverage: map[listKey]*coverageList{{"n/residential", "24h", "coverage"}: {list: l, scale: 1.5,
		ranks: map[string]model.CountryLatencyRank{"DE": {P50: 120, Rank: 2, Peers: 4}}}}}
	page, ok, _ := pg.Coverage(context.Background(), "at", "n/residential", "24h", ListFilter{}, 1)
	if !ok || page.Total != 1 || len(page.Rows) != 1 {
		t.Fatalf("page %+v", page)
	}
	b, err := page.Rows[0].MarshalJSON()
	if want := `["DE","Berlin","Berlin",3320,"Telekom","isp",5,120,2,4,null,null]`; err != nil || string(b) != want {
		t.Errorf("row %s, want %s", b, want)
	}
	if b, _ := json.Marshal(page.Columns); string(b) != `["countryCode","region","city","asn","asnName","usageType","ips",`+
		`"latencyP50","latencyRank","latencyPeers","ipsRank","ipsPeers"]` {
		t.Errorf("columns %s", b)
	}
	// A search's totals: the IPs its rows show, of the rows with IPs; a
	// country filter alone has them too.
	want := CoverageTotals{Ips: 5, Countries: 1, Cities: 1, Asns: 1}
	for _, f := range []ListFilter{{Query: "berlin"}, {Country: "DE"}, {Query: "de telekom", Country: "DE"}} {
		page, ok = pageOf(pg.Coverage(context.Background(), "at", "n/residential", "24h", f, 1))
		if !ok || page.Total != 1 || page.Totals == nil || *page.Totals != want || len(page.Rows) != 1 || page.Rows[0].Ips != 5 {
			t.Errorf("%+v: %+v, totals %+v, want %+v", f, page, page.Totals, want)
		}
	}
	// Neither the country's name nor another country matches.
	for _, f := range []ListFilter{{Query: "germany"}, {Country: "FR"}, {Query: "berlin", Country: "AT"}} {
		if page, ok = pageOf(pg.Coverage(context.Background(), "at", "n/residential", "24h", f, 1)); !ok || page.Total != 0 ||
			len(page.Rows) != 0 || page.Totals == nil || *page.Totals != (CoverageTotals{}) {
			t.Errorf("%+v: %+v", f, page)
		}
	}
}

func pageOf(p *CoveragePage, ok bool, _ error) (*CoveragePage, bool) { return p, ok }

func ptr(s string) *string { return &s }

// withPlaceKeys is pg with the place keys its sources hold, as a build
// gathers them (countryLists.add).
func withPlaceKeys(t *testing.T, pg *Pages) *Pages {
	t.Helper()
	pg.placeKeys = map[placeList]map[string]struct{}{}
	for k, srcs := range pg.sources {
		rows, err := mergeSources(context.Background(), srcs, k.dimension, "", false)
		if err != nil {
			t.Fatal(err)
		}
		pg.placeKeys[k] = map[string]struct{}{}
		for _, r := range rows {
			pg.placeKeys[k][r.Key] = struct{}{}
		}
	}
	return pg
}

// SEC-COST-1: a place key that no list of its range and dimension holds
// answers not found from the build's place keys, without reading or merging
// a row (no allocation); a held key merges its rows.
func TestAMadeUpPlaceReadsNoRow(t *testing.T) {
	// Not parallel: testing.AllocsPerRun.
	res, _ := smallNetBuild(t)
	pg := res.Pages
	key := pg.lists[listKey{"country/AA", "30d", "city"}].first[0].Key
	if _, ok, err := pg.Place(context.Background(), "", "city", key, "30d"); !ok || err != nil {
		t.Fatalf("city %s: %v %v", key, ok, err)
	}
	if _, ok, _ := pg.Place(context.Background(), "", "region", key, "30d"); ok {
		t.Errorf("city key %s has a region page", key)
	}
	for _, made := range []string{key + "x", "AA|made|up", "ZZ|" + key} {
		if a := testing.AllocsPerRun(20, func() {
			if _, ok, err := pg.Place(context.Background(), "", "city", made, "30d"); ok || err != nil {
				t.Errorf("%s: a page (%v)", made, err)
			}
		}); a != 0 {
			t.Errorf("%s: %v allocations, want none (no row read)", made, a)
		}
	}
	if a := testing.AllocsPerRun(5, func() { pg.Place(context.Background(), "", "city", key, "30d") }); a == 0 { //nolint:errcheck
		t.Errorf("a held key allocates nothing: the allocation count proves nothing")
	}
}

// netSource is a network's compact list of the given city rows (IPs, p50)
// tagged with its slug.
func netSource(t *testing.T, slug string, cities map[string][2]int64) placeSource {
	t.Helper()
	rows := []store.NetRow{}
	for city, v := range cities {
		rows = append(rows, store.NetRow{Place: &store.Place{CountryCode: ptr("DE"), RegionCode: ptr("BE"), Region: ptr("Berlin"), City: ptr(city)},
			Ips: v[0], Completed: 10, Successes: 9, P50: int32(v[1])})
	}
	l, err := store.NewNetList(map[string][]store.NetRow{"city": rows})
	if err != nil {
		t.Fatal(err)
	}
	return placeSource{list: l, scale: nan, network: slug, name: strings.ToUpper(slug), kind: "residential"}
}

// Case 3: a city seen by three networks has their three rows, ranked by IPs
// and by p50 among networks of the same kind; an unknown key has no page.
func TestACityPageRanksTheNetworksInIt(t *testing.T) {
	t.Parallel()
	pg := withPlaceKeys(t, &Pages{sources: map[placeList][]placeSource{{"30d", "city"}: {
		netSource(t, "a", map[string][2]int64{"Berlin": {10, 300}, "Potsdam": {99, 10}}),
		netSource(t, "b", map[string][2]int64{"Berlin": {30, 100}}),
		netSource(t, "c", map[string][2]int64{"Berlin": {20, 200}})}}})
	page, ok, _ := pg.Place(context.Background(), "at", "city", "DE|BE|Berlin|Berlin", "30d")
	if !ok || len(page.Rows) != 3 {
		t.Fatalf("page %+v", page)
	}
	for i, want := range []struct {
		network        string
		ipsRank, latRk Num
	}{{"b", 1, 1}, {"c", 2, 2}, {"a", 3, 3}} {
		r := page.Rows[i]
		if r.Network != want.network || r.IpsSumRank.Rank != want.ipsRank || r.LatencyRank.Rank != want.latRk || r.IpsSumRank.Peers != 3 {
			t.Errorf("row %d: %s ranks %+v %+v, want %+v", i, r.Network, r.IpsSumRank, r.LatencyRank, want)
		}
	}
	for _, key := range []string{"DE|BE|Berlin|Hamburg", "|BE|Berlin|Berlin", "DE|BE|Berlin"} {
		if _, ok, _ := pg.Place(context.Background(), "at", "city", key, "30d"); ok {
			t.Errorf("%q has a city page", key)
		}
	}
	if _, ok, _ := pg.Place(context.Background(), "at", "street", "DE|BE|Berlin|Berlin", "30d"); ok {
		t.Error("a street page")
	}
}

// Case 4: a country's region list holds every region row of every network
// in that country, in the order of its city list.
func TestACountryRegionListHoldsEveryNetworksRows(t *testing.T) {
	t.Parallel()
	res, _ := smallNetBuild(t)
	pg := res.Pages
	for _, d := range []string{"region", "city"} {
		want := 0
		for _, n := range []string{"net00", "net01", "net02"} {
			l, _, _ := pg.List(context.Background(), "", n+"/residential", "7d", d, ListFilter{}, 1)
			for p := 1; p <= l.Pages; p++ {
				page, _, _ := pg.List(context.Background(), "", n+"/residential", "7d", d, ListFilter{}, p)
				for _, r := range page.Rows {
					if model.SV(r.CountryCode) == "AA" {
						want++
					}
				}
			}
		}
		list, ok, _ := pg.List(context.Background(), "", "country/AA", "7d", d, ListFilter{}, 1)
		if !ok || list.Total != want || want == 0 || len(list.Rows) != min(want, PageSize) {
			t.Fatalf("%s: %d rows of %d", d, list.Total, want)
		}
		rows, err := pg.lists[listKey{"country/AA", "7d", d}].rows(context.Background(), nil, "")
		if err != nil {
			t.Fatal(err)
		}
		for i, r := range list.Rows {
			if r.Network == "" || r.Key != rows[i].Key || r.Network != rows[i].Network || i > 0 && placeLess(list.Rows[i], list.Rows[i-1]) {
				t.Fatalf("%s row %d: %+v", d, i, r)
			}
		}
		if second, _, _ := pg.List(context.Background(), "", "country/AA", "7d", d, ListFilter{}, 2); want > PageSize && second.Rows[0].Key != rows[PageSize].Key {
			t.Errorf("%s page 2 starts %+v", d, second.Rows[0])
		}
	}
	key := pg.lists[listKey{"country/AA", "30d", "city"}].first[0].Key
	if page, ok, _ := pg.Place(context.Background(), "", "city", key, "30d"); !ok || len(page.Rows) == 0 || page.Rows[0].Key != key {
		t.Errorf("city page %s: %+v", key, page)
	}
	if _, ok, _ := pg.List(context.Background(), "", "country/ZZ", "7d", "region", ListFilter{}, 1); ok {
		t.Error("a country without networks has a region list")
	}
}

// Review N1: a v4 build keeps each list's first page in its own array, never
// the merged list it was cut from.
func TestAV4BuildKeepsOnlyItsFirstPages(t *testing.T) {
	t.Parallel()
	res, _ := smallNetBuild(t)
	n := 0
	for k, l := range res.Pages.lists {
		if cap(l.first) > PageSize || l.srcs == nil {
			t.Errorf("%v: first page of %d rows holds %d (a list before v4: %v)", k, len(l.first), cap(l.first), l.srcs == nil)
		}
		n++
	}
	if n == 0 || cap(res.Pages.lists[listKey{"net00/residential", "30d", "city"}].first) != PageSize {
		t.Errorf("%d lists", n)
	}
}

// dim is l's rows of a dimension, failing t when they cannot be read.
func dim(t testing.TB, l *store.NetList, dimension string) *store.NetRows {
	t.Helper()
	v, err := l.Rows(context.Background(), dimension)
	if err != nil {
		t.Fatal(err)
	}
	return v
}
