package build

import (
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

func TestCountryLatencyRanks(t *testing.T) {
	makeEntry := func(kind, cohort, end string, latency Num) *entry {
		country := " bg "
		product := &snap.Product{Kind: kind, Cohort: cohort, Windows: map[string]snap.Window{
			"30d": {Period: snap.Period{Start: "2026-09-01", End: end},
				Breakdowns: map[string][]snap.BreakdownRow{"country": {
					{CountryCode: &country, P50: latency, Successes: 100},
				}}},
		}}
		return &entry{P: &model.Provider{Source: model.SourceMeasured}, Snap: map[string]*snap.Product{kind: product}}
	}
	fast := makeEntry("residential", "pool", "2026-10-01", 100)
	tied := makeEntry("residential", "pool", "2026-10-01", 100)
	slow := makeEntry("residential", "pool", "2026-10-01", 200)
	missing := makeEntry("residential", "pool", "2026-10-01", nan)
	otherKind := makeEntry("mobile", "pool", "2026-10-01", 10)
	otherCohort := makeEntry("residential", "other", "2026-10-01", 10)
	otherPeriod := makeEntry("residential", "pool", "2026-09-30", 10)
	noSuccess := makeEntry("residential", "pool", "2026-10-01", 20)
	w := noSuccess.Snap["residential"].Windows["30d"]
	w.Breakdowns["country"][0].Successes = 0
	negative := makeEntry("residential", "pool", "2026-10-01", -1)
	// The same country has a different ordering in a different selected window.
	for _, e := range []*entry{fast, slow} {
		w := e.Snap["residential"].Window("30d")
		value := Num(300)
		if e == slow {
			value = 50
		}
		country := "BG"
		w.Breakdowns = map[string][]snap.BreakdownRow{"country": {{CountryCode: &country, P50: value, Successes: 10}}}
		e.Snap["residential"].Windows["7d"] = w
	}
	addCountryLatencyRanks([]*entry{fast, tied, slow, missing, otherKind, otherCohort, otherPeriod, noSuccess, negative})
	for _, c := range []struct {
		e             *entry
		kind, rangeID string
		rank          Num
		peers         int
	}{{fast, "residential", "30d", 1, 3}, {tied, "residential", "30d", 1, 3},
		{slow, "residential", "30d", 3, 3}, {fast, "residential", "7d", 2, 2},
		{slow, "residential", "7d", 1, 2}, {otherKind, "mobile", "30d", 1, 1},
		{otherCohort, "residential", "30d", 1, 1}, {otherPeriod, "residential", "30d", 1, 1}} {
		got := c.e.P.CountryLatencyRanks[c.kind][c.rangeID]["BG"]
		if got.Rank != c.rank || got.Peers != c.peers {
			t.Errorf("%s/%s: got %+v, want rank %v with %d peers", c.kind, c.rangeID, got, c.rank, c.peers)
		}
	}
	for _, e := range []*entry{missing, noSuccess, negative} {
		got := e.P.CountryLatencyRanks["residential"]["30d"]["BG"]
		if got.P50.OK() || got.Rank.OK() || got.Peers != 3 {
			t.Errorf("invalid country latency was ranked: %+v", got)
		}
	}
	if _, ok := fast.P.CountryLatencyRanks["residential"]["30d"]["RO"]; ok {
		t.Error("invented a rank for an unmeasured country")
	}
}
