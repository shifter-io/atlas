package build

import (
	"slices"
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

// latencyDelta is a latency_day_delta of +23.4567% whose last period runs
// from start to 16 Nov.
func latencyDelta(start string) string {
	return `{"pct":23.4567,"reason":null,"basis":"normal","computed_at":"2026-11-16T03:00:00Z",
"days":[{"p50":1000,"countries":40},{"start":"` + start + `","end":"2026-11-16T00:00:00Z","p50":1234.4,"countries":40}]}`
}

// LATENCY-DELTA-24H-1, LATENCY-DELTA-PERIOD-1: the 24h, 7d and 30d
// latencyDeltaPct are the calculator's change of the last period of whole
// days against the one before, at 1 dp (not the series' closing fifth
// against its opening fifth), and the changelog compares those two periods'
// p50s. LATENCY-DELTA-FALLBACK-1: latencyDeltaBasis and the changelog's
// basis name the periods compared, the longest the network's data allows.
func TestTheDayLatencyChangeIsTheCalculators(t *testing.T) {
	t.Parallel()
	for _, c := range []struct{ rangeID, start, want string }{
		{"24h", "2026-11-15T00:00:00Z", DeltaBasisDay},
		{"7d", "2026-11-09T00:00:00Z", DeltaBasisWeek},
		{"7d", "2026-11-15T00:00:00Z", DeltaBasisDay},
		{"30d", "2026-10-17T00:00:00Z", DeltaBasisMonth},
		{"30d", "2026-11-09T00:00:00Z", DeltaBasisWeek},
		{"30d", "2026-11-15T00:00:00Z", DeltaBasisDay},
	} {
		p := latencyDaysProduct(t, c.rangeID, latencyDelta(c.start))
		d, edges, basis, why := latencyChange(p, c.rangeID)
		if d != 23.5 || edges != (trendEdges{1000, 1234.4}) || model.SV(basis) != c.want || why != "" {
			t.Errorf("%s %s: %v %v %q %q, want 23.5 [1000 1234.4] %s and no reason", c.rangeID, c.start, d, edges, model.SV(basis), why, c.want)
		}
		ws := &WindowStats{LatencyDeltaPct: d, latencyEdges: edges, LatencyDeltaBasis: basis}
		entries := performanceEntries(func(kind, id string) ChangeEntry { return ChangeEntry{Kind: kind, ID: id} }, ws)
		if len(entries) != 1 || entries[0].Values["value"] != Num(1234) || entries[0].Values["previous"] != Num(1000) ||
			entries[0].Values["basis"] != c.want || entries[0].NAReasons != nil {
			t.Errorf("%s changelog entries %+v, want one latency entry 1000 -> 1234 on %s", c.rangeID, entries, c.want)
		}
	}
	// Periods of another length are no basis the API knows.
	d, _, basis, why := latencyChange(latencyDaysProduct(t, "7d", latencyDelta("2026-11-13T00:00:00Z")), "7d")
	if d.OK() || basis != nil || why != NAInsufficient {
		t.Errorf("3-day periods: %v %v %q, want null with %s", d, basis, why, NAInsufficient)
	}
	// A success entry has no basis: null, not applicable.
	ws := &WindowStats{SuccessDeltaPts: 3, LatencyDeltaPct: nan, successEdges: trendEdges{0.5, 0.53}}
	entries := performanceEntries(func(kind, id string) ChangeEntry { return ChangeEntry{Kind: kind, ID: id} }, ws)
	if len(entries) != 1 || entries[0].Values["basis"] != nil || entries[0].NAReasons["values.basis"] != NANotApplicable {
		t.Errorf("success entries %+v, want a null basis, not applicable", entries)
	}
}

// Until two days exist (or when the day lacks countries) the change is null
// with the calculator's reason and no basis (a 7d or 30d snapshot from
// before the fallback: needs_two_periods); a snapshot from before it has
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
		{"30d", `{"pct":null,"reason":"needs_two_days","days":[{"p50":null},{"p50":900}]}`, NANeedsTwoDays},
		{"30d", "", NANotPublished},
	} {
		d, edges, basis, why := latencyChange(latencyDaysProduct(t, c.rangeID, c.delta), c.rangeID)
		if d.OK() || edges[0].OK() || edges[1].OK() || basis != nil || why != c.want {
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
	for _, path := range []string{"latencyDeltaPct", "latencyDeltaBasis"} {
		if got := row.ReasonOf(path); got != NANeedsTwoDays {
			t.Errorf("ranking row %s reason %q, want %s", path, got, NANeedsTwoDays)
		}
	}
	if got := (naFacts{measured: true, latencyWhy: NADayNotCovered}).code("latencyDeltaPct"); got != NANoRun {
		t.Errorf("without a run: %q, want %s", got, NANoRun)
	}
	stats := &RangeStats{WindowStats: &WindowStats{Requests: 10, LatencyDeltaPct: nan, latencyWhy: NADayNotCovered}}
	a := &NetworkAnswer{Kind: AllNetworks, Stats: map[string]*RangeStats{"24h": stats}}
	if err := a.listNA(nil); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"latencyDeltaPct", "latencyDeltaBasis"} {
		if got := stats.ReasonOf(path); got != NADayNotCovered {
			t.Errorf("stats.24h %s reason %q, want %s", path, got, NADayNotCovered)
		}
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
	var bases []string
	for _, code := range codeSets["latency_delta_basis"].Values {
		bases = append(bases, code.Code)
	}
	if !slices.Equal(bases, []string{DeltaBasisDay, DeltaBasisWeek, DeltaBasisMonth}) {
		t.Errorf("/v1/codes latency_delta_basis %v, want day, week, month", bases)
	}
}
