package build

import (
	"context"
	"strings"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/testscale"
)

// Two networks as payload v4, each one summary over two requested countries
// (AA and AB). Each network list holds three exit countries, AA, AB and AC
// with 1000, 999 and 998 IPs, and 60 region, city and ASN rows.
func smallPages(t *testing.T) *Pages {
	t.Helper()
	in, err := testscale.NetworkInputs(testscale.NetworkShape{Shape: testscale.Shape{Networks: 2, Countries: 1, Exits: 3, Tail: 60}, Requested: 2})
	if err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	return res.Pages
}

func TestANetworkAnswerIsItsSummary(t *testing.T) {
	t.Parallel()
	pg := smallPages(t)
	if len(pg.Networks.Networks) != 2 || pg.Primary["net00"] != "residential" {
		t.Fatalf("networks %+v, primary %v", pg.Networks.Networks, pg.Primary)
	}
	ans := pg.Network["net00/residential"]
	if ans == nil || ans.ProductGroups != 2 || strings.Join(ans.RequestedCountries, ",") != "AA,AB" {
		t.Fatalf("answer %+v", ans)
	}
	r := ans.Ranges["30d"]
	if r.ExitCountries != 3 || r.Countries[0].Key != "AA" || r.Countries[0].IpsSum != 1000 || r.Countries[0].LatencyP50 != 400 ||
		r.Countries[0].Requests != 3*1000 || r.Countries[0].SuccessRate != Num(2.0/3) {
		t.Fatalf("countries %+v", r.Countries)
	}
	// The city and ASN lists are the network list's rows.
	if r.Lists["city"] != 60 || r.Lists["asn"] != 60 {
		t.Fatalf("lists %v", r.Lists)
	}
	if len(ans.Days) == 0 || ans.Days[0].Day >= ans.Days[len(ans.Days)-1].Day {
		t.Fatalf("days %+v", ans.Days)
	}
}

func TestPlaceListsPageAndSearch(t *testing.T) {
	t.Parallel()
	pg := smallPages(t)
	first, ok, _ := pg.List(context.Background(), "", "net00/residential", "30d", "city", ListFilter{}, 1)
	if !ok || first.Total != 60 || first.Pages != 2 || len(first.Rows) != PageSize || first.Rows[0].IpsSum != 1000 {
		t.Fatalf("page 1: %+v", first)
	}
	second, _, _ := pg.List(context.Background(), "", "net00/residential", "30d", "city", ListFilter{}, 2)
	beyond, _, _ := pg.List(context.Background(), "", "net00/residential", "30d", "city", ListFilter{}, 3)
	if len(second.Rows) != 10 || len(beyond.Rows) != 0 || second.Rows[0].IpsSum >= first.Rows[PageSize-1].IpsSum {
		t.Fatalf("page 2: %d rows, page 3: %d rows", len(second.Rows), len(beyond.Rows))
	}
	found, _, _ := pg.List(context.Background(), "", "net00/residential", "30d", "asn", ListFilter{Query: "AS64513"}, 1)
	if found.Total != 1 || *found.Rows[0].Asn != 64513 || model.SV(found.Rows[0].AsnName) != "Example Networks 1" {
		t.Fatalf("asn search: %+v", found)
	}
	if _, ok, _ := pg.List(context.Background(), "", "nobody/residential", "30d", "city", ListFilter{}, 1); ok {
		t.Fatal("an unknown network has no list")
	}
	if _, ok, _ := pg.List(context.Background(), "", "net00/residential", "30d", "street", ListFilter{}, 1); ok {
		t.Fatal("an unknown dimension has no list")
	}
}

// country= keeps one country's rows of every page; a search matches codes
// and served names, never a country's name (the fixture's are "Country
// <code>"); an ASN row has no country (PLACE-CODES-1).
func TestPlaceListsFilterByCountry(t *testing.T) {
	t.Parallel()
	pg, ctx := smallPages(t), context.Background()
	var all []PlaceRow
	for p := 1; ; p++ {
		page, _, _ := pg.List(ctx, "", "net00/residential", "30d", "city", ListFilter{}, p)
		if len(page.Rows) == 0 {
			break
		}
		all = append(all, page.Rows...)
	}
	cc, want := model.SV(all[0].CountryCode), 0
	for _, r := range all {
		if model.SV(r.CountryCode) == cc {
			want++
		}
	}
	got, ok, _ := pg.List(ctx, "", "net00/residential", "30d", "city", ListFilter{Country: cc}, 1)
	if !ok || want == 0 || got.Total != want || len(got.Rows) != min(want, PageSize) {
		t.Fatalf("%s: %d of %d rows (%d in the list)", cc, got.Total, want, len(all))
	}
	for _, r := range got.Rows {
		if model.SV(r.CountryCode) != cc {
			t.Fatalf("row %+v is not in %s", r, cc)
		}
	}
	for _, c := range []struct {
		dimension string
		f         ListFilter
	}{{"city", ListFilter{Country: "ZZ"}}, {"asn", ListFilter{Country: cc}}, {"city", ListFilter{Query: "country"}},
		{"country", ListFilter{Query: "country " + cc}}} {
		if page, ok, _ := pg.List(ctx, "", "net00/residential", "30d", c.dimension, c.f, 1); !ok || page.Total != 0 || len(page.Rows) != 0 {
			t.Errorf("%s %+v: %d rows", c.dimension, c.f, page.Total)
		}
	}
	if page, _, _ := pg.List(ctx, "", "net00/residential", "30d", "country", ListFilter{Query: strings.ToLower(cc)}, 1); page.Total == 0 {
		t.Errorf("a search for the code %s finds no country", cc)
	}
}

func TestACountryAnswerRanksTheNetworksInIt(t *testing.T) {
	t.Parallel()
	pg := smallPages(t)
	ans := pg.Country["AB"]
	if ans == nil || len(ans.Ranges["30d"]) != 2 {
		t.Fatalf("AB: %+v", ans)
	}
	for _, r := range ans.Ranges["30d"] {
		if r.IpsSum != 999 || r.IpsSumRank.Rank != 1 || r.IpsSumRank.Peers != 2 || r.LatencyRank.Rank != 1 {
			t.Fatalf("row %+v", r)
		}
	}
	if pg.Countries.Countries[0].CountryCode != "AA" || pg.Countries.Countries[0].Networks != 2 || pg.Countries.Countries[0].IpsSum != 2*1000 {
		t.Fatalf("index %+v", pg.Countries.Countries)
	}
	cities, ok, _ := pg.List(context.Background(), "", "country/"+pg.Countries.Countries[0].CountryCode, "30d", "city", ListFilter{}, 1)
	if !ok {
		t.Fatal("no city list")
	}
	for _, r := range cities.Rows {
		if model.SV(r.CountryCode) != "AA" || r.Network == "" {
			t.Fatalf("city row %+v", r)
		}
	}
}

func TestTheCoverageIndexKeepsOnlyAnEntryPerProvider(t *testing.T) {
	t.Parallel()
	in, err := testscale.Inputs(testscale.Shape{Networks: 2, Countries: 1, Exits: 3, Tail: 30, Unmeasured: 1, Events: 5})
	if err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	idx := res.Coverage.Index()
	if len(idx.Providers) != 3 || len(idx.Providers) != len(res.Coverage.Providers) {
		t.Fatalf("index providers %+v", idx.Providers)
	}
	for i, e := range idx.Providers {
		full := res.Coverage.Providers[i]
		if e.ID != full.ID || e.NACount != full.NACount || e.NACount == 0 {
			t.Fatalf("entry %+v, report %+v", e, full)
		}
	}
	if idx.Summary.Providers != 3 || len(idx.Fields) == 0 {
		t.Fatalf("summary %+v, %d fields", idx.Summary, len(idx.Fields))
	}
}
