package build

import (
	"context"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// A Coverage row writes its stored IP rank and peers (calculated v15) at
// positions 11 and 12, with and without q; a network place list's row has
// them as ipsRank, a list merged from several sources and a place page none.
func TestIpsRankOnCoverageAndPlaceRows(t *testing.T) {
	t.Parallel()
	berlin := &store.Place{CountryCode: ptr("DE"), Country: ptr("Germany"), RegionCode: ptr("BE"), Region: ptr("Berlin"), City: ptr("Berlin")}
	asn := int64(3320)
	telekom := &store.Network{Asn: &asn, Name: ptr("Telekom"), UsageType: ptr("isp")}
	l, err := store.NewNetList(map[string][]store.NetRow{
		"city_asn": {{Place: berlin, Network: telekom, Ips: 500, P50: 120, IpsRank: 1, IpsPeers: 3}},
		"city":     {{Place: berlin, Ips: 100, Completed: 10, Successes: 9, P50: 120, IpsRank: 3, IpsPeers: 3}}})
	if err != nil {
		t.Fatal(err)
	}
	pg := &Pages{coverage: map[listKey]*coverageList{{"n/residential", "24h", "coverage"}: {list: l, scale: nan,
		ranks: map[string]model.CountryLatencyRank{}}}}
	for _, q := range []string{"", "telekom"} {
		page, ok, _ := pg.Coverage(context.Background(), "at", "n/residential", "24h", q, 1)
		if !ok || len(page.Rows) != 1 {
			t.Fatalf("q %q: page %+v", q, page)
		}
		b, err := page.Rows[0].MarshalJSON()
		if want := `["DE","Germany","Berlin","Berlin",3320,"Telekom","isp",500,120,null,0,1,3]`; err != nil || string(b) != want {
			t.Errorf("q %q: row %s, want %s", q, b, want)
		}
	}
	src := placeSource{list: l, scale: nan, network: "n", name: "N", kind: "residential"}
	one, err := mergeSources(context.Background(), []placeSource{src}, "city", "", false)
	if err != nil || len(one) != 1 || one[0].IpsRank == nil || *one[0].IpsRank != (model.Rank{Rank: 3, Peers: 3}) {
		t.Errorf("one list's city row: %+v (%v)", one, err)
	}
	two, err := mergeSources(context.Background(), []placeSource{src, src}, "city", "", false)
	if err != nil || len(two) != 1 || two[0].IpsRank != nil {
		t.Errorf("a merged city row has a rank: %+v (%v)", two, err)
	}
	apart, err := mergeSources(context.Background(), []placeSource{src}, "city", "DE", true)
	if err != nil || len(apart) != 1 || apart[0].IpsRank != nil {
		t.Errorf("a country list's row has a rank: %+v (%v)", apart, err)
	}
	country, err := mergeSources(context.Background(), []placeSource{src}, "city", "DE", false)
	if err != nil || len(country) != 1 || country[0].IpsRank != nil {
		t.Errorf("a country page of one network has a rank: %+v (%v)", country, err)
	}
}

// A row without a stored rank (a list before v15) writes nulls at 11 and 12.
func TestCoverageRowWithoutIpsRank(t *testing.T) {
	t.Parallel()
	if got := ipsRankOf(store.NetRow{Ips: 5}); got != nil {
		t.Errorf("rank %+v of an unranked row", got)
	}
	if got := ipsRankOf(store.NetRow{Ips: 5, IpsRank: 1, IpsPeers: 1}); got == nil || *got != (model.Rank{Rank: 1, Peers: 1}) {
		t.Errorf("a place of one network: %+v, want 1 of 1", got)
	}
}
