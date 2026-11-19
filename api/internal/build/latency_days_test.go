package build

import (
	"math"
	"slices"
	"testing"

	"github.com/shifter-io/atlas/api/internal/model"
	"github.com/shifter-io/atlas/api/internal/snap"
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

// latencyDaysJSON is a latency_day_delta of +23.4567% whose last period runs
// from start to 16 Nov.
func latencyDaysJSON(start string) string {
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
		p := latencyDaysProduct(t, c.rangeID, latencyDaysJSON(c.start))
		ld := latencyChange(p, c.rangeID)
		d, edges, basis, why := ld.pct, ld.edges, ld.basis, ld.why
		if d != 23.5 || edges != (trendEdges{1000, 1234.4}) || model.SV(basis) != c.want || why != "" ||
			model.SV(ld.asOf) != "2026-11-15" {
			t.Errorf("%s %s: %v %v %q %q, want 23.5 [1000 1234.4] %s and no reason", c.rangeID, c.start, d, edges, model.SV(basis), why, c.want)
		}
		// The change is dated at the newer period's end, its entry too
		// (CHANGELOG-REAL-1, #1314).
		if ld.at != snap.Ms("2026-11-16T00:00:00Z") {
			t.Errorf("%s %s: dated %v, want the newer period's end 2026-11-16T00:00Z", c.rangeID, c.start, ld.at)
		}
		ws := &WindowStats{LatencyDeltaPct: d, latencyEdges: edges, LatencyDeltaBasis: basis, latencyAt: ld.at}
		dated := math.NaN()
		entries := performanceEntries(func(kind, id string, at float64) ChangeEntry {
			dated = at
			return ChangeEntry{Kind: kind, ID: id}
		}, ws)
		if len(entries) != 1 || entries[0].Values["value"] != Num(1234) || entries[0].Values["previous"] != Num(1000) ||
			entries[0].Values["basis"] != c.want || entries[0].NAReasons != nil || dated != ld.at {
			t.Errorf("%s changelog entries %+v dated %v, want one latency entry 1000 -> 1234 on %s at %v", c.rangeID, entries, dated, c.want, ld.at)
		}
	}
	// Periods of another length are no basis the API knows.
	ld := latencyChange(latencyDaysProduct(t, "7d", latencyDaysJSON("2026-11-13T00:00:00Z")), "7d")
	d, basis, why := ld.pct, ld.basis, ld.why
	if d.OK() || basis != nil || why != NAInsufficient {
		t.Errorf("3-day periods: %v %v %q, want null with %s", d, basis, why, NAInsufficient)
	}
	// A success entry has no basis: null, not applicable.
	ws := &WindowStats{SuccessDeltaPts: 3, LatencyDeltaPct: nan, successEdges: trendEdges{0.5, 0.53}}
	entries := performanceEntries(func(kind, id string, _ float64) ChangeEntry { return ChangeEntry{Kind: kind, ID: id} }, ws)
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
		ld := latencyChange(latencyDaysProduct(t, c.rangeID, c.delta), c.rangeID)
		d, edges, basis, why := ld.pct, ld.edges, ld.basis, ld.why
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
	for _, path := range []string{"latencyDeltaPct", "latencyDeltaBasis", "latencyDeltaAsOf"} {
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
	for _, path := range []string{"latencyDeltaPct", "latencyDeltaBasis", "latencyDeltaAsOf"} {
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

// #1285 on staging (a network, 17 Nov 03:05): 7d and 30d fell back to the day,
// which had too few countries (159 tested on 15 Nov, 205 on 16 Nov), so
// latencyDeltaPct and latencyDeltaBasis were null with day_not_covered. The
// two paths made it the row's most common code, served as naReason, and
// naReasons.latencyDeltaPct, which the website reads, was missing. A
// latency change's reason is always in naReasons.
func TestAFallbackDayNotCoveredIsInNaReasons(t *testing.T) {
	t.Parallel()
	delta := `{"pct":null,"reason":"day_not_covered","basis":"normal","computed_at":"2026-11-17T03:00:00Z",
"days":[{"start":"2026-11-15T00:00:00Z","end":"2026-11-16T00:00:00Z","p50":980,"countries":159},
 {"start":"2026-11-16T00:00:00Z","end":"2026-11-17T00:00:00Z","p50":702,"countries":205}]}`
	stats := map[string]*RangeStats{}
	for _, rangeID := range []string{"7d", "30d"} {
		ld := latencyChange(latencyDaysProduct(t, rangeID, delta), rangeID)
		d, basis, why := ld.pct, ld.basis, ld.why
		if d.OK() || basis != nil || ld.asOf != nil || why != NADayNotCovered {
			t.Fatalf("%s: %v %v %q, want null, no basis, %s", rangeID, d, basis, why, NADayNotCovered)
		}
		ws := &WindowStats{Requests: 10, LatencyDeltaPct: d, LatencyDeltaBasis: basis, latencyWhy: why}
		stats[rangeID] = &RangeStats{WindowStats: ws}
		row := RankingRow{Requests: 10, Source: model.SourceMeasured, Breakdown: model.NewBreakdown(), LatencyDelta: d,
			LatencyBasis: basis, latencyWhy: why}
		row.WindowFigures = noFigures()
		if err := row.NAList.list(&row, nil, row.facts().code); err != nil {
			t.Fatal(err)
		}
		for _, path := range []string{"latencyDeltaPct", "latencyDeltaBasis", "latencyDeltaAsOf"} {
			if got := row.Reasons[path]; got != NADayNotCovered || row.Reason == NADayNotCovered {
				t.Errorf("%s ranking row naReasons.%s %q (naReason %q), want %s", rangeID, path, got, row.Reason, NADayNotCovered)
			}
		}
	}
	a := &NetworkAnswer{Kind: AllNetworks, Stats: stats}
	if err := a.listNA(nil); err != nil {
		t.Fatal(err)
	}
	for rangeID, s := range stats {
		for _, path := range []string{"latencyDeltaPct", "latencyDeltaBasis", "latencyDeltaAsOf"} {
			if got := s.Reasons[path]; got != NADayNotCovered || s.Reason == NADayNotCovered {
				t.Errorf("stats.%s naReasons.%s %q (naReason %q), want %s", rangeID, path, got, s.Reason, NADayNotCovered)
			}
		}
	}
	// That network's 7d row: the two latency paths against two insufficient_data
	// ones, a tie the code's name decided.
	var l NAList
	l.set([]string{"atlasScoreSpark.values[]", "blacklistHitRate", "kycVerified", "latencyDeltaBasis", "latencyDeltaPct",
		"stickinessHoldRate", "successScoreSpark.values[]"},
		[]string{NAInsufficient, NANotMeasured, NACatalog, NADayNotCovered, NADayNotCovered, NANotMeasured, NAInsufficient})
	if l.Reasons["latencyDeltaPct"] != NADayNotCovered || l.Reasons["latencyDeltaBasis"] != NADayNotCovered ||
		l.Reason == NADayNotCovered || l.ReasonOf("kycVerified") != NACatalog || l.ReasonOf("blacklistHitRate") != NANotMeasured {
		t.Errorf("the 7d row: naReason %q, naReasons %v", l.Reason, l.Reasons)
	}
	// Only latency nulls: no naReason, both in naReasons.
	l.set([]string{"latencyDeltaBasis", "latencyDeltaPct"}, []string{NANeedsTwoDays, NANeedsTwoDays})
	if l.Reason != "" || len(l.Reasons) != 2 || l.ReasonOf("latencyDeltaPct") != NANeedsTwoDays {
		t.Errorf("only latency nulls: naReason %q, naReasons %v", l.Reason, l.Reasons)
	}
}

// LATENCY-ALWAYS-1: a change the calculator carried on from an earlier build
// is served with its own basis and the last day its periods cover.
func TestACarriedLatencyChangeSaysItsDay(t *testing.T) {
	t.Parallel()
	carried := `{"pct":-12.34,"reason":null,"basis":"normal","scope":"common","carried":true,"computed_at":"2026-11-15T03:00:00Z",
"days":[{"start":"2026-11-13T00:00:00Z","end":"2026-11-14T00:00:00Z","p50":800,"countries":150},
 {"start":"2026-11-14T00:00:00Z","end":"2026-11-15T00:00:00Z","p50":701.3,"countries":150}]}`
	for _, rangeID := range []string{"24h", "7d", "30d"} {
		ld := latencyChange(latencyDaysProduct(t, rangeID, carried), rangeID)
		if ld.pct != -12.3 || model.SV(ld.basis) != DeltaBasisDay || model.SV(ld.asOf) != "2026-11-14" || ld.why != "" || !ld.carried {
			t.Errorf("%s: %+v, want -12.3 on day, as of 2026-11-14, carried", rangeID, ld)
		}
		// Its move is not new: no changelog entry, build after build.
		ws := &WindowStats{LatencyDeltaPct: ld.pct, latencyEdges: ld.edges, LatencyDeltaBasis: ld.basis, latencyCarried: ld.carried,
			SuccessDeltaPts: nan}
		if entries := performanceEntries(func(kind, id string, _ float64) ChangeEntry { return ChangeEntry{Kind: kind, ID: id} }, ws); len(entries) != 0 {
			t.Errorf("%s: a carried change makes changelog entries %+v", rangeID, entries)
		}
	}
}

// LATENCY-ALWAYS-1: the network answer's profile.performance.latencyDelta24h
// (and the dataset file's providers[].performance) is stats.24h's change, by
// the same rule: a network's day on its common countries, or a value kept from
// an earlier day with its day; it was always null for a measured network.
func TestTheProfileLatencyDelta24hIsTheDayChange(t *testing.T) {
	t.Parallel()
	shifter := `{"pct":5.172413793103448,"reason":null,"basis":"normal","scope":"common","computed_at":"2026-11-17T03:00:00Z",
"days":[{"start":"2026-11-15T00:00:00Z","end":"2026-11-16T00:00:00Z","p50":464,"countries":159},
 {"start":"2026-11-16T00:00:00Z","end":"2026-11-17T00:00:00Z","p50":488,"countries":159}]}`
	carried := `{"pct":-12.34,"reason":null,"basis":"normal","carried":true,"computed_at":"2026-11-15T03:00:00Z",
"days":[{"start":"2026-11-13T00:00:00Z","end":"2026-11-14T00:00:00Z","p50":800,"countries":150},
 {"start":"2026-11-14T00:00:00Z","end":"2026-11-15T00:00:00Z","p50":701.3,"countries":150}]}`
	for _, c := range []struct {
		delta string
		pct   Num
		asOf  string
	}{{shifter, 5.2, "2026-11-16"}, {carried, -12.3, "2026-11-14"}} {
		p := latencyDaysProduct(t, "24h", c.delta)
		perf := performanceStats(p.Window("30d"), p.Window("24h"))
		if perf.LatencyDelta24h != c.pct || model.SV(perf.LatencyDelta24hAsOf) != c.asOf {
			t.Errorf("latencyDelta24h %v as of %q, want %v as of %s", perf.LatencyDelta24h, model.SV(perf.LatencyDelta24hAsOf), c.pct, c.asOf)
		}
	}
	// Never computed: null, with the day rule's reason in naReasons.
	none := performanceStats(snap.Window{}, latencyDaysProduct(t, "24h",
		`{"pct":null,"reason":"day_not_covered","days":[{"p50":800},{"p50":900}]}`).Window("24h"))
	if none.LatencyDelta24h.OK() || none.LatencyDelta24hAsOf != nil {
		t.Errorf("a null day change gives %v as of %v", none.LatencyDelta24h, none.LatencyDelta24hAsOf)
	}
	stats := &RangeStats{WindowStats: &WindowStats{Requests: 10, LatencyDeltaPct: nan, latencyWhy: NADayNotCovered}}
	ranges := map[string]*RangeTotals{"24h": {Requests: 10}, "30d": {Requests: 10}}
	a := &NetworkAnswer{Kind: AllNetworks, Ranges: ranges, Stats: map[string]*RangeStats{"24h": stats}, Profile: &NetworkProfile{Founded: nan}}
	a.Profile.Performance = none
	if err := a.listNA(nil); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"performance.latencyDelta24h", "performance.latencyDelta24hAsOf"} {
		if got := a.Profile.Reasons[path]; got != NADayNotCovered {
			t.Errorf("profile naReasons.%s %q (naReason %q), want %s", path, got, a.Profile.Reason, NADayNotCovered)
		}
	}
}
