package build

import (
	"github.com/Zeus-Development/atlas/api/internal/jsnum"
	"github.com/Zeus-Development/atlas/api/internal/snap"
)

// The reasons of a null 24h latencyDeltaPct (LATENCY-DELTA-24H-1), the
// calculator's own codes (calculated/latency_days.py).
const (
	// NANeedsTwoDays: the network has no two whole days of daily tests yet.
	NANeedsTwoDays = "needs_two_days"
	// NADayNotCovered: a day compared has too few tested countries or successes.
	NADayNotCovered = "day_not_covered"
)

// latencyChange is a measured range's latencyDeltaPct, the two latencies it
// compares (the changelog's values) and the reason code of its null ("" for
// the figure's general one): the 24h range's change of two whole days
// (dayLatencyDelta), a longer range's closing fifth against its opening
// fifth (measuredTrend).
func latencyChange(sp *snap.Product, rangeID string) (Num, trendEdges, string) {
	if rangeID == "24h" {
		return dayLatencyDelta(sp.Window(rangeID))
	}
	d, edges := measuredTrend(sp, rangeID, true)
	return d, edges, ""
}

// dayLatencyDelta is the calculator's latency change of two whole days
// (snap.LatencyDays) at 1 dp, the two days' headline p50s, and its reason when
// null: the calculator's, or not_published in a snapshot from before it.
func dayLatencyDelta(w snap.Window) (Num, trendEdges, string) {
	none := trendEdges{nan, nan}
	d := w.LatencyDays
	switch {
	case d == nil:
		return nan, none, NANotPublished
	case !d.Pct.OK() || len(d.Days) != 2:
		if d.Reason != nil && (*d.Reason == NANeedsTwoDays || *d.Reason == NADayNotCovered) {
			return nan, none, *d.Reason
		}
		return nan, none, NAInsufficient
	}
	return Num(jsnum.FixedNum(d.Pct.F(), 1)), trendEdges{d.Days[0].P50, d.Days[1].P50}, ""
}
