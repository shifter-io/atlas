package build

import (
	"math"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// BANDWIDTH-PER-NETWORK-1: each window's wire bytes, both directions, in
// decimal units (GB = 10⁹ bytes, MB = 10⁶). The home and ranking rows'
// bandwidthGb sum to the market's for every range; a network page's
// stats.<range>.bandwidthMb is the same bytes in MB, and its profile's
// bandwidthMb24h the 24h window's.
func TestBandwidthPerNetworkSumsToTheMarket(t *testing.T) {
	t.Parallel()
	pg, _ := homePages(t)
	for _, r := range Ranges {
		h := homeOf(t, pg, r.ID)
		rows := pg.Rankings[r.ID].Networks
		home, ranked, measured := 0.0, 0.0, 0
		for i, n := range h.Networks {
			if !sameNum(n.BandwidthGb, rows[i].BandwidthGb) {
				t.Errorf("%s %s: home %v, row %v", r.ID, n.Slug, n.BandwidthGb, rows[i].BandwidthGb)
			}
			if n.BandwidthGb.OK() {
				home, ranked, measured = home+n.BandwidthGb.F(), ranked+rows[i].BandwidthGb.F(), measured+1
			}
		}
		if measured != 3 || !h.Market.BandwidthGb.OK() || math.Abs(home-h.Market.BandwidthGb.F()) > 1e-9 ||
			math.Abs(ranked-h.Market.BandwidthGb.F()) > 1e-9 {
			t.Errorf("%s: %d networks sum to %v (rows %v), market %v", r.ID, measured, home, ranked, h.Market.BandwidthGb)
		}
		for _, n := range h.Networks {
			ans := pg.Network[n.Slug+"/"+n.Kind]
			if ans == nil || ans.Stats[r.ID] == nil {
				continue
			}
			if mb := ans.Stats[r.ID].BandwidthMb; !mb.OK() || math.Abs(mb.F()-n.BandwidthGb.F()*1000) > 1e-6 {
				t.Errorf("%s %s: stats bandwidthMb %v, home %v GB", r.ID, n.Slug, mb, n.BandwidthGb)
			}
			if r.ID == "24h" && !sameNum(ans.Profile.Performance.BandwidthMb24h, ans.Stats[r.ID].BandwidthMb) {
				t.Errorf("%s: profile bandwidthMb24h %v, stats %v", n.Slug, ans.Profile.Performance.BandwidthMb24h, ans.Stats[r.ID].BandwidthMb)
			}
		}
	}
}

// A window with requests but no recorded bytes has a null bandwidth with
// not_measured; a window without bytes is null, never 0.
func TestBandwidthWithoutBytesIsNotMeasured(t *testing.T) {
	t.Parallel()
	ran := snap.Metrics{Completed: 10, BytesUp: nan, BytesDown: nan}
	none := snap.Metrics{Completed: 0, BytesUp: 0, BytesDown: 0}
	if bandwidthMb(ran).OK() || bandwidthMb(none).OK() {
		t.Errorf("no bytes: %v, %v", bandwidthMb(ran), bandwidthMb(none))
	}
	if got := bandwidthMb(snap.Metrics{Completed: 1, BytesUp: 1e6, BytesDown: 2e6}); got != 3 {
		t.Errorf("3,000,000 wire bytes: %v MB", got)
	}
	f := naFacts{measured: true, ran: true}
	for _, p := range []string{"bandwidthGb", "stats.24h.bandwidthMb", "performance.bandwidthMb24h"} {
		if got := f.code(p); got != NANotMeasured {
			t.Errorf("%s: %s, want %s", p, got, NANotMeasured)
		}
	}
	if got := (naFacts{measured: true}).code("bandwidthGb"); got != NANoRun {
		t.Errorf("no run: %s, want %s", got, NANoRun)
	}
}
