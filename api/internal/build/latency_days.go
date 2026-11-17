package build

import (
	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/model"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// The reasons of a null latencyDeltaPct (LATENCY-DELTA-24H-1,
// LATENCY-DELTA-PERIOD-1), the calculator's own codes
// (calculated/latency_days.py). Since LATENCY-DELTA-FALLBACK-1 every range
// falls back to the day rule, so a new calculation gives the day rule's.
const (
	// NANeedsTwoDays: the network has no two whole days of daily tests yet.
	NANeedsTwoDays = "needs_two_days"
	// NANeedsTwoPeriods: the same for the 7d and 30d ranges' two periods of
	// 7 or 30 whole days, only in a snapshot from before the fallback.
	NANeedsTwoPeriods = "needs_two_periods"
	// NADayNotCovered: a day or period compared has too few tested countries
	// or successes.
	NADayNotCovered = "day_not_covered"
)

// The latencyDeltaBasis codes (LATENCY-DELTA-FALLBACK-1): the periods a
// latencyDeltaPct compares, by their length in whole days.
const (
	DeltaBasisDay   = "day"
	DeltaBasisWeek  = "week"
	DeltaBasisMonth = "month"
)

var deltaBases = map[float64]string{1: DeltaBasisDay, 7: DeltaBasisWeek, 30: DeltaBasisMonth}

const dayMs = 24 * 60 * 60 * 1000

// latencyChange is a measured range's latencyDeltaPct, the two latencies it
// compares (the changelog's values), its basis and the reason code of its
// null ("" for the figure's general one): the change of the last 30, 7 or 1
// whole UTC days against as many before them, the longest the network's data
// allows up to the range (dayLatencyDelta).
func latencyChange(sp *snap.Product, rangeID string) (Num, trendEdges, model.Str, string) {
	return dayLatencyDelta(sp.Window(rangeID))
}

// dayLatencyDelta is the calculator's latency change of two periods of whole
// days (snap.LatencyDays) at 1 dp, the two periods' headline p50s, the
// periods' basis code, and its reason when null: the calculator's, or
// not_published in a snapshot from before it.
func dayLatencyDelta(w snap.Window) (Num, trendEdges, model.Str, string) {
	none := trendEdges{nan, nan}
	d := w.LatencyDays
	if d == nil {
		return nan, none, nil, NANotPublished
	}
	if !d.Pct.OK() || len(d.Days) != 2 {
		if d.Reason != nil {
			switch *d.Reason {
			case NANeedsTwoDays, NANeedsTwoPeriods, NADayNotCovered:
				return nan, none, nil, *d.Reason
			}
		}
		return nan, none, nil, NAInsufficient
	}
	last := d.Days[1]
	basis, ok := deltaBases[(snap.Ms(last.End)-snap.Ms(last.Start))/dayMs]
	if !ok {
		return nan, none, nil, NAInsufficient
	}
	return Num(jsnum.FixedNum(d.Pct.F(), 1)), trendEdges{d.Days[0].P50, last.P50}, model.S(basis), ""
}
