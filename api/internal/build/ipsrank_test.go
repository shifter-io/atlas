package build

import (
	"context"
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/store"
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
		page, ok, _ := pg.Coverage(context.Background(), "at", "n/residential", "24h", ListFilter{Query: q}, 1)
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
	// A country's first page, gathered from the networks' own rows, has none either.
	c := newCountryLists()
	c.add(placeList{"24h", "city"}, []placeSource{src}, one)
	for k, rows := range c.cand {
		if len(rows) != 1 || rows[0].IpsRank != nil {
			t.Errorf("%v: a country's first page row has a rank: %+v", k, rows)
		}
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

// A network's two labels of one ASN (two usage types) are one place: the
// calculation writes the place's rank on both rows (ips_rank.sql), and the
// merged place row keeps it.
func TestIpsRankOfAPlaceOfTwoLabels(t *testing.T) {
	t.Parallel()
	asn := int64(7922)
	label := func(usage string) *store.Network {
		return &store.Network{Asn: &asn, Name: ptr("Comcast"), UsageType: ptr(usage)}
	}
	l, err := store.NewNetList(map[string][]store.NetRow{"asn": {
		{Network: label("residential"), Ips: 400, Completed: 2, Successes: 2, P50: 100, IpsRank: 1, IpsPeers: 2},
		{Network: label("isp"), Ips: 200, Completed: 1, Successes: 1, P50: 100, IpsRank: 1, IpsPeers: 2}}})
	if err != nil {
		t.Fatal(err)
	}
	src := placeSource{list: l, scale: nan, network: "n", name: "N", kind: "residential"}
	rows, err := mergeSources(context.Background(), []placeSource{src}, "asn", "", false)
	if err != nil || len(rows) != 1 || rows[0].IpsRank == nil || *rows[0].IpsRank != (model.Rank{Rank: 1, Peers: 2}) {
		t.Errorf("AS7922's merged row: %+v (%v)", rows, err)
	}
}
