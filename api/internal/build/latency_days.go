package build

import (
	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// The reasons of a null latencyDeltaPct (LATENCY-DELTA-24H-1,
// LATENCY-DELTA-PERIOD-1), the calculator's own codes
// (calculated/latency_days.py).
const (
	// NANeedsTwoDays: the network has no two whole days of daily tests yet.
	NANeedsTwoDays = "needs_two_days"
	// NANeedsTwoPeriods: the same for the 7d and 30d ranges' two periods of
	// 7 or 30 whole days.
	NANeedsTwoPeriods = "needs_two_periods"
	// NADayNotCovered: a day or period compared has too few tested countries
	// or successes.
	NADayNotCovered = "day_not_covered"
)

// latencyChange is a measured range's latencyDeltaPct, the two latencies it
// compares (the changelog's values) and the reason code of its null ("" for
// the figure's general one): the change of the last 1, 7 or 30 whole UTC days
// against as many before them (dayLatencyDelta).
func latencyChange(sp *snap.Product, rangeID string) (Num, trendEdges, string) {
	return dayLatencyDelta(sp.Window(rangeID))
}

// dayLatencyDelta is the calculator's latency change of two periods of whole
// days (snap.LatencyDays) at 1 dp, the two periods' headline p50s, and its
// reason when null: the calculator's, or not_published in a snapshot from
// before it.
func dayLatencyDelta(w snap.Window) (Num, trendEdges, string) {
	none := trendEdges{nan, nan}
	d := w.LatencyDays
	switch {
	case d == nil:
		return nan, none, NANotPublished
	case !d.Pct.OK() || len(d.Days) != 2:
		if d.Reason != nil {
			switch *d.Reason {
			case NANeedsTwoDays, NANeedsTwoPeriods, NADayNotCovered:
				return nan, none, *d.Reason
			}
		}
		return nan, none, NAInsufficient
	}
	return Num(jsnum.FixedNum(d.Pct.F(), 1)), trendEdges{d.Days[0].P50, d.Days[1].P50}, ""
}
