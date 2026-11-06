package build

import (
	"context"
	"strings"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/testscale"
)

// Two networks, each tested in two requested countries (AA and AB): four
// product groups. Group c's country breakdown holds three exit countries
// starting at the c-th code with 1000, 999 and 998 IPs; every group's region,
// city and ASN lists are capped and their 60 typed rows are the network's.
func smallPages(t *testing.T) *Pages {
	t.Helper()
	in, err := testscale.Inputs(testscale.Shape{Networks: 2, Countries: 2, Exits: 3, Tail: 60})
	if err != nil {
		t.Fatal(err)
	}
	res, err := Build(in)
	if err != nil {
		t.Fatal(err)
	}
	return res.Pages
}

func TestANetworkAnswerSumsItsProductGroups(t *testing.T) {
	pg := smallPages(t)
	if len(pg.Networks.Networks) != 2 || pg.Primary["net00"] != "residential" {
		t.Fatalf("networks %+v, primary %v", pg.Networks.Networks, pg.Primary)
	}
	ans := pg.Network["net00/residential"]
	if ans == nil || ans.ProductGroups != 2 || strings.Join(ans.RequestedCountries, ",") != "AA,AB" {
		t.Fatalf("answer %+v", ans)
	}
	r := ans.Ranges["30d"]
	// AB: 999 IPs in group AA, 1000 in group AB; latency of the row with most successes.
	if r.ExitCountries != 4 || r.Countries[0].Key != "AB" || r.Countries[0].IpsSum != 1999 || r.Countries[0].LatencyP50 != 400 ||
		r.Countries[0].Requests != 3*1999 || r.Countries[0].SuccessRate != Num(2.0/3) {
		t.Fatalf("countries %+v", r.Countries)
	}
	// The capped city list is read whole from the typed rows of both groups.
	if r.Lists["city"] != 60 || r.Lists["asn"] != 60 {
		t.Fatalf("lists %v", r.Lists)
	}
	if len(ans.Days) == 0 || ans.Days[0].Day >= ans.Days[len(ans.Days)-1].Day {
		t.Fatalf("days %+v", ans.Days)
	}
}

func TestPlaceListsPageAndSearch(t *testing.T) {
	pg := smallPages(t)
	first, ok, _ := pg.List(context.Background(), "", "net00/residential", "30d", "city", "", 1)
	if !ok || first.Total != 60 || first.Pages != 2 || len(first.Rows) != PageSize || first.Rows[0].IpsSum != 2000 {
		t.Fatalf("page 1: %+v", first)
	}
	second, _, _ := pg.List(context.Background(), "", "net00/residential", "30d", "city", "", 2)
	beyond, _, _ := pg.List(context.Background(), "", "net00/residential", "30d", "city", "", 3)
	if len(second.Rows) != 10 || len(beyond.Rows) != 0 || second.Rows[0].IpsSum >= first.Rows[PageSize-1].IpsSum {
		t.Fatalf("page 2: %d rows, page 3: %d rows", len(second.Rows), len(beyond.Rows))
	}
	found, _, _ := pg.List(context.Background(), "", "net00/residential", "30d", "asn", "AS64513", 1)
	if found.Total != 1 || *found.Rows[0].Asn != 64513 || model.SV(found.Rows[0].AsnName) != "Example Networks 1" {
		t.Fatalf("asn search: %+v", found)
	}
	if _, ok, _ := pg.List(context.Background(), "", "nobody/residential", "30d", "city", "", 1); ok {
		t.Fatal("an unknown network has no list")
	}
	if _, ok, _ := pg.List(context.Background(), "", "net00/residential", "30d", "street", "", 1); ok {
		t.Fatal("an unknown dimension has no list")
	}
}

func TestACountryAnswerRanksTheNetworksInIt(t *testing.T) {
	pg := smallPages(t)
	ans := pg.Country["AB"]
	if ans == nil || len(ans.Ranges["30d"]) != 2 {
		t.Fatalf("AB: %+v", ans)
	}
	for _, r := range ans.Ranges["30d"] {
		if r.IpsSum != 1999 || r.IpsSumRank.Rank != 1 || r.IpsSumRank.Peers != 2 || r.LatencyRank.Rank != 1 {
			t.Fatalf("row %+v", r)
		}
	}
	if pg.Countries.Countries[0].CountryCode != "AB" || pg.Countries.Countries[0].Networks != 2 || pg.Countries.Countries[0].IpsSum != 2*1999 {
		t.Fatalf("index %+v", pg.Countries.Countries)
	}
	cities, ok, _ := pg.List(context.Background(), "", "country/"+pg.Countries.Countries[0].CountryCode, "30d", "city", "", 1)
	if !ok {
		t.Fatal("no city list")
	}
	for _, r := range cities.Rows {
		if model.SV(r.CountryCode) != "AB" || r.Network == "" {
			t.Fatalf("city row %+v", r)
		}
	}
}

func TestTheCoverageIndexKeepsOnlyAnEntryPerProvider(t *testing.T) {
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
