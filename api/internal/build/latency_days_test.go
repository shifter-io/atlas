package build

import (
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// latencyDaysProduct is a measured product whose 24h window carries the
// calculator's latency change of two whole days (delta: its JSON, "" for a
// snapshot from before it) and whose 7d window's latency rose from 300 to 600.
func latencyDaysProduct(t *testing.T, delta string) *snap.Product {
	t.Helper()
	day := `{"period":{"start":"2026-11-15T12:00:00Z","end":"2026-11-16T12:00:00Z"}`
	if delta != "" {
		day += `,"latency_day_delta":` + delta
	}
	return decodeProduct(t, `{"id":"p","windows":{"24h":`+day+`},
"7d":{"period":{"start":"2026-11-09T12:00:00Z","end":"2026-11-16T12:00:00Z"},
 "series":[{"at":"2026-11-10T00:00:00Z","latency_ms":{"p50":300}},{"at":"2026-11-15T00:00:00Z","latency_ms":{"p50":600}}]}}}`)
}

// LATENCY-DELTA-24H-1: the 24h latencyDeltaPct is the calculator's change of
// the last whole day against the day before, at 1 dp, and the changelog
// compares those two days' p50s; the 7d keeps its closing fifth against its
// opening fifth.
func TestTheDayLatencyChangeIsTheCalculators(t *testing.T) {
	t.Parallel()
	p := latencyDaysProduct(t, `{"pct":23.4567,"reason":null,"basis":"under_load","computed_at":"2026-11-16T03:00:00Z",
"days":[{"start":"2026-11-14T00:00:00Z","end":"2026-11-15T00:00:00Z","p50":1000,"countries":40},
 {"start":"2026-11-15T00:00:00Z","end":"2026-11-16T00:00:00Z","p50":1234.4,"countries":40}]}`)
	d, edges, why := latencyChange(p, "24h")
	if d != 23.5 || edges != (trendEdges{1000, 1234.4}) || why != "" {
		t.Errorf("24h: %v %v %q, want 23.5 [1000 1234.4] and no reason", d, edges, why)
	}
	if d, edges, why := latencyChange(p, "7d"); d != 100 || edges != (trendEdges{300, 600}) || why != "" {
		t.Errorf("7d: %v %v %q, want the fifths' 100 [300 600]", d, edges, why)
	}
	ws := &WindowStats{LatencyDeltaPct: d, latencyEdges: edges}
	entries := performanceEntries(func(kind, id string) ChangeEntry { return ChangeEntry{Kind: kind, ID: id} }, ws)
	if len(entries) != 1 || entries[0].Values["value"] != Num(1234) || entries[0].Values["previous"] != Num(1000) {
		t.Errorf("changelog entries %+v, want one latency entry 1000 -> 1234", entries)
	}
}

// Until two whole days exist (or when a day lacks countries) the 24h change
// is null with the calculator's reason; a snapshot from before it has none
// to publish.
func TestANullDayLatencyChangeSaysWhy(t *testing.T) {
	t.Parallel()
	for _, c := range []struct{ delta, want string }{
		{`{"pct":null,"reason":"needs_two_days","days":[{"p50":null},{"p50":900}]}`, NANeedsTwoDays},
		{`{"pct":null,"reason":"day_not_covered","days":[{"p50":800},{"p50":900}]}`, NADayNotCovered},
		{`{"pct":null,"reason":"something new"}`, NAInsufficient},
		{`{"pct":5,"reason":null,"days":[]}`, NAInsufficient},
		{"", NANotPublished},
	} {
		d, edges, why := latencyChange(latencyDaysProduct(t, c.delta), "24h")
		if d.OK() || edges[0].OK() || edges[1].OK() || why != c.want {
			t.Errorf("%s: %v %v %q, want null with %s", c.delta, d, edges, why, c.want)
		}
	}
}

// The reason reaches the ranking row (and so the home row, which takes its
// facts) and the network answer's stats.24h (naReasons), and /v1/codes
// lists it.
func TestTheDayLatencyReasonIsServed(t *testing.T) {
	t.Parallel()
	row := RankingRow{Requests: 10, Source: model.SourceMeasured, Breakdown: model.NewBreakdown(), LatencyDelta: nan,
		latencyWhy: NANeedsTwoDays}
	row.WindowFigures = noFigures()
	if err := row.NAList.list(&row, nil, row.facts().code); err != nil {
		t.Fatal(err)
	}
	if got := row.ReasonOf("latencyDeltaPct"); got != NANeedsTwoDays {
		t.Errorf("ranking row latencyDeltaPct reason %q, want %s", got, NANeedsTwoDays)
	}
	if got := (naFacts{measured: true, latencyWhy: NADayNotCovered}).code("latencyDeltaPct"); got != NANoRun {
		t.Errorf("without a run: %q, want %s", got, NANoRun)
	}
	stats := &RangeStats{WindowStats: &WindowStats{Requests: 10, LatencyDeltaPct: nan, latencyWhy: NADayNotCovered}}
	a := &NetworkAnswer{Kind: AllNetworks, Stats: map[string]*RangeStats{"24h": stats}}
	if err := a.listNA(nil); err != nil {
		t.Fatal(err)
	}
	if got := stats.ReasonOf("latencyDeltaPct"); got != NADayNotCovered {
		t.Errorf("stats.24h latencyDeltaPct reason %q, want %s", got, NADayNotCovered)
	}
	listed := map[string]bool{}
	for _, code := range codeSets["na_reason"].Values {
		listed[code.Code] = true
	}
	if !listed[NANeedsTwoDays] || !listed[NADayNotCovered] {
		t.Errorf("/v1/codes na_reason lacks %s or %s", NANeedsTwoDays, NADayNotCovered)
	}
}
