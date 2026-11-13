package build

import (
	"context"
	"fmt"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/store"
)

// broadCoverage is a coverage list of n City x ASN rows, not in IPs order (the
// framework stores them by completed requests), a third of them in the US and
// 4,000 ASNs, as a broad q= finds them (COVERAGE-SEARCH-2).
func broadCoverage(tb testing.TB, n int) *Pages {
	tb.Helper()
	us, de := ptr("US"), ptr("DE")
	nets := make([]*store.Network, 4000)
	for i := range nets {
		asn := int64(64512 + i)
		nets[i] = &store.Network{Asn: &asn, Name: ptr(fmt.Sprintf("Example Networks %d", i)), UsageType: ptr("isp")}
	}
	rows := make([]store.NetRow, n)
	for i := range rows {
		cc, country := de, ptr("Germany")
		if i%3 == 0 {
			cc, country = us, ptr("United States")
		}
		p := &store.Place{CountryCode: cc, Country: country, Region: ptr(fmt.Sprintf("Region %d", i%50)), City: ptr(fmt.Sprintf("City %d", i/3))}
		rows[i] = store.NetRow{Place: p, Network: nets[i%len(nets)], Ips: int64(i*7919%n + 1), P50: int32(100 + i%400)}
	}
	l, err := store.NewNetList(map[string][]store.NetRow{"city_asn": rows})
	if err != nil {
		tb.Fatal(err)
	}
	return &Pages{coverage: map[listKey]*coverageList{{"n/residential", "7d", "coverage"}: {list: l, scale: 1.7,
		ranks: map[string]model.CountryLatencyRank{"US": {Rank: 1, Peers: 9}, "DE": {Rank: 3, Peers: 9}}}}}
}

func BenchmarkBroadCoverageSearch(b *testing.B) {
	for _, n := range []int{150_000, 300_000} {
		pg := broadCoverage(b, n)
		for _, q := range []string{"us", "city"} {
			b.Run(fmt.Sprintf("%d/%s", n, q), func(b *testing.B) {
				for range b.N {
					if _, _, err := pg.Coverage(context.Background(), "at", "n/residential", "7d", ListFilter{Query: q}, 1); err != nil {
						b.Fatal(err)
					}
				}
			})
		}
	}
}
