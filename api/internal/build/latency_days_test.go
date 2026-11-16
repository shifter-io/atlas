package build

import (
	"testing"

	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// latencyDaysProduct is a measured product whose rangeID window carries the
// calculator's latency change of two periods of whole days (delta: its JSON,
// "" for a snapshot from before it) and whose series' latency rose from 300
// to 600, which the change no longer reads.
func latencyDaysProduct(t *testing.T, rangeID, delta string) *snap.Product {
	t.Helper()
	w := `{"period":{"start":"2026-10-17T12:00:00Z","end":"2026-11-16T12:00:00Z"},
 "series":[{"at":"2026-10-18T00:00:00Z","latency_ms":{"p50":300}},{"at":"2026-11-15T00:00:00Z","latency_ms":{"p50":600}}]`
	if delta != "" {
		w += `,"latency_day_delta":` + delta
	}
	return decodeProduct(t, `{"id":"p","windows":{"`+rangeID+`":`+w+`}}}`)
}

// LATENCY-DELTA-24H-1, LATENCY-DELTA-PERIOD-1: the 24h, 7d and 30d
// latencyDeltaPct are the calculator's change of the last period of whole
// days against the one before, at 1 dp (not the series' closing fifth
// against its opening fifth), and the changelog compares those two periods'
// p50s.
func TestTheDayLatencyChangeIsTheCalculators(t *testing.T) {
	t.Parallel()
	for _, rangeID := range []string{"24h", "7d", "30d"} {
		p := latencyDaysProduct(t, rangeID, `{"pct":23.4567,"reason":null,"basis":"normal","computed_at":"2026-11-16T03:00:00Z",
"days":[{"start":"2026-11-02T00:00:00Z","end":"2026-11-09T00:00:00Z","p50":1000,"countries":40},
 {"start":"2026-11-09T00:00:00Z","end":"2026-11-16T00:00:00Z","p50":1234.4,"countries":40}]}`)
		d, edges, why := latencyChange(p, rangeID)
		if d != 23.5 || edges != (trendEdges{1000, 1234.4}) || why != "" {
			t.Errorf("%s: %v %v %q, want 23.5 [1000 1234.4] and no reason", rangeID, d, edges, why)
		}
		ws := &WindowStats{LatencyDeltaPct: d, latencyEdges: edges}
		entries := performanceEntries(func(kind, id string) ChangeEntry { return ChangeEntry{Kind: kind, ID: id} }, ws)
		if len(entries) != 1 || entries[0].Values["value"] != Num(1234) || entries[0].Values["previous"] != Num(1000) {
			t.Errorf("%s changelog entries %+v, want one latency entry 1000 -> 1234", rangeID, entries)
		}
	}
}

// Until two periods exist (or when a period lacks countries) the change is
// null with the calculator's reason; a snapshot from before it has
// none to publish.
func TestANullDayLatencyChangeSaysWhy(t *testing.T) {
	t.Parallel()
	for _, c := range []struct{ rangeID, delta, want string }{
		{"24h", `{"pct":null,"reason":"needs_two_days","days":[{"p50":null},{"p50":900}]}`, NANeedsTwoDays},
		{"7d", `{"pct":null,"reason":"needs_two_periods","days":[{"p50":null},{"p50":900}]}`, NANeedsTwoPeriods},
		{"24h", `{"pct":null,"reason":"day_not_covered","days":[{"p50":800},{"p50":900}]}`, NADayNotCovered},
		{"7d", `{"pct":null,"reason":"day_not_covered","days":[{"p50":800},{"p50":900}]}`, NADayNotCovered},
		{"24h", `{"pct":null,"reason":"something new"}`, NAInsufficient},
		{"7d", `{"pct":5,"reason":null,"days":[]}`, NAInsufficient},
		{"24h", "", NANotPublished},
		{"7d", "", NANotPublished},
		{"30d", `{"pct":null,"reason":"needs_two_periods","days":[{"p50":null},{"p50":900}]}`, NANeedsTwoPeriods},
		{"30d", "", NANotPublished},
	} {
		d, edges, why := latencyChange(latencyDaysProduct(t, c.rangeID, c.delta), c.rangeID)
		if d.OK() || edges[0].OK() || edges[1].OK() || why != c.want {
			t.Errorf("%s %s: %v %v %q, want null with %s", c.rangeID, c.delta, d, edges, why, c.want)
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
	for _, code := range []string{NANeedsTwoDays, NANeedsTwoPeriods, NADayNotCovered} {
		if !listed[code] {
			t.Errorf("/v1/codes na_reason lacks %s", code)
		}
	}
}
